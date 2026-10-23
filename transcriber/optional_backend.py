"""Optional local speech engines running in isolated, persistent processes."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace

import numpy as np

from transcriber.base import TranscriptionBackend
from services.local_asr.catalog import BACKENDS, MODELS, selected_model, selected_device, resolve_runtime
from services.local_asr import cache

logger = logging.getLogger(__name__)

# warmup() input: one second of seeded noise at about -40 dBFS. Quiet enough
# that Parakeet and Nemotron return no text for it, but well above the
# 0.00025 peak below which _transcribe_audio skips a window, so unlike zeros
# it reaches the engine. Longer inputs measured no better (see
# config.SPEECH_WARMUP_BACKENDS).
_WARMUP_SAMPLES = 16000
_WARMUP_LEVEL = 0.01


def _is_set(event) -> bool:
    return event is not None and event.is_set()


def _phrase_options(phrases) -> dict:
    # A decode without a vocabulary is called exactly as before there was one.
    return {"phrases": tuple(phrases)} if phrases else {}


class LocalSpeechBackend(TranscriptionBackend):
    supports_recognition = True
    #: True for RemoteSpeechBackend, whose worker runs on another computer.
    is_remote = False

    def __init__(self, backend: str, model_name: str | None = None, device: str | None = None):
        super().__init__()
        self.backend_id = backend
        self._model_override = model_name
        self._device_override = device
        self._process = None
        self._generation = 0
        self._state_lock = threading.RLock()
        self._decode_lock = threading.Lock()
        self.model = None
        self.last_error = ""
        # Why an Auto device runs on the CPU although a GPU is present.
        self.cpu_fallback_note = ""
        self.runtime_component = None
        self.device = "cpu"
        self.model_name = model_name or selected_model(backend, self._settings())

    @staticmethod
    def _settings():
        from services.settings import settings_manager
        return settings_manager.load_all_settings()

    @property
    def name(self):
        return BACKENDS[self.backend_id]

    @property
    def is_model_missing(self):
        return not cache.is_cached(self.model_name)

    @property
    def last_loaded_model(self):
        return self.model_name if self.is_available() else None

    @property
    def device_info(self):
        if self.is_available():
            return f"{MODELS[self.model_name].label} | {self.device}"
        return self.last_error or f"{self.name} is not loaded"

    def is_available(self):
        return self._process is not None and self.model is not None and self._process.process.poll() is None

    @property
    def generation(self) -> int:
        """Advances on every cleanup, so a changed value means a different worker.

        Incremental dictation compares it across the windows it decodes early:
        text from before a reload or cancel is never joined with text after.
        """
        with self._state_lock:
            return self._generation

    def reload_model(self, model_name=None, *, cancel_event=None):
        with self._state_lock:
            if cancel_event is not None and cancel_event.is_set():
                return
            self.cleanup()
            self.reset_cancel_flag()
            generation = self._generation
        self.cpu_fallback_note = ""
        settings = self._settings()
        self.model_name = model_name or self._model_override or selected_model(self.backend_id, settings)
        if self.model_name not in MODELS or MODELS[self.model_name].backend != self.backend_id:
            raise ValueError("Model does not belong to this backend")
        from services.components import is_installed
        requested = self._device_override or selected_device(self.backend_id, settings)
        component, device = resolve_runtime(self.backend_id, requested)
        self.runtime_component = component
        if not is_installed(component):
            self._explain_missing_runtime(component, device)
            return
        if self.is_model_missing:
            self.last_error = f"Download {MODELS[self.model_name].label} in Downloads."
            return
        loaded = self._start_worker(component, requested, device, generation, cancel_event)
        if loaded is None:
            return
        process, result = loaded
        with self._state_lock:
            if generation != self._generation or _is_set(cancel_event):
                process.close()
                return
            self.device = result["device"]
            self.model = SpeechDecoder(self)
            self.last_error = ""
        if result.get("gpu"):
            logger.info("%s is running on %s through Vulkan", self.name, result["gpu"])

    def _explain_missing_runtime(self, component, device):
        from services.components import catalog_entry_for_platform, check_compatibility
        entry = catalog_entry_for_platform(component)
        reason = check_compatibility(entry) if entry is not None else None
        if entry is None:
            self.runtime_component = None
            self.last_error = f"{self.name}'s {device.upper()} runtime is not available on this platform."
        elif reason:
            self.runtime_component = None
            self.last_error = f"{self.name} is not available on this computer. {reason}."
        else:
            self.last_error = f"Install {self.name}'s {self._runtime_kind(device)} runtime in Downloads."

    def _runtime_kind(self, device) -> str:
        if self.backend_id == "parakeet_mlx":
            return "MLX"
        return "GPU" if device in ("cuda", "mps", "metal") else "CPU"

    def _start_worker(self, component, requested, device, generation, cancel_event):
        """A worker with the model loaded and its load reply, or None once superseded.

        Auto falls back to the CPU when the runtime has no code for the GPU.
        """
        from services.components import component_dir
        from services.local_asr.process import SpeechProcess
        # Windows runtimes carry an embedded Python; the macOS and Linux
        # ones use the app's interpreter with downloaded native libraries
        # or, for MLX, a separate wheel tree.
        if sys.platform == "win32":
            python = str(Path(component_dir(component)) / "python.exe")
        else:
            python = sys.executable
        while True:
            with self._state_lock:
                if generation != self._generation or _is_set(cancel_event):
                    return None
                process = SpeechProcess(python)
                self._process = process
            try:
                result = process.request("load", backend=self.backend_id, model=self.model_name,
                                         model_path=cache.load_path(self.model_name),
                                         runtime=component_dir(component), device=device, timeout=300,
                                         cancel=cancel_event)
            except Exception as exc:
                process.close()
                if (getattr(exc, "code", None) == "unsupported_gpu" and requested == "auto"
                        and device != "cpu"):
                    # Auto picked a GPU the runtime has no code for (an older
                    # PyTorch on a newer card); the CPU still transcribes.
                    logger.warning("%s cannot use this GPU; loading on the CPU: %s", self.name, exc)
                    self.cpu_fallback_note = f"{self.name} is using the CPU. {exc}"
                    device = "cpu"
                    continue
                self._load_failed(process, exc, device, generation, cancel_event)
                raise
            return process, result

    def _load_failed(self, process, exc, device, generation, cancel_event):
        with self._state_lock:
            current = generation == self._generation
            if current:
                self._process = None
                self.last_error = str(exc)
        if current and not _is_set(cancel_event):
            # The worker's traceback (and any PyTorch warning) is the
            # only record of why a native runtime failed to load.
            output = getattr(process, "recent_errors", lambda: "")()
            logger.error("%s failed to load on %s: %s%s", self.name, device, exc,
                         f"\nWorker output:\n{output}" if output else "")

    def download_and_load(self, progress_callback=None):
        cache.download(self.model_name, progress_callback)
        self.reload_model(self.model_name)

    def cleanup(self):
        with self._state_lock:
            self._generation += 1
            process, self._process = self._process, None
            self.model = None
        if process:
            process.close()

    def cancel_transcription(self):
        super().cancel_transcription()
        self.cleanup()

    def warmup(self) -> bool:
        """Run one throwaway decode so the first real one after a load is warm.

        The first "transcribe" request in a fresh worker pays a one-time
        cost: on an RTX 2060 Parakeet decoded a 10.6 s dictation in 314 ms
        cold against 85 ms warm. This decode takes about 0.3 s and leaves the
        next one at 88 ms (config.SPEECH_WARMUP_BACKENDS has the other
        engines).

        Best-effort and non-fatal. It skips when another decode already holds
        the worker (that decode warms it), and a cleanup or reload closing
        the worker mid-request just ends it. Deliberately leaves
        ``is_transcribing`` alone so a cancel press cannot tear the engine
        down over it. Returns True when the decode ran.
        """
        with self._state_lock:
            generation = self._generation
            if self._process is None or self.model is None:
                return False
        if not self._decode_lock.acquire(blocking=False):
            return False
        started = time.perf_counter()
        try:
            with self._state_lock:
                if generation != self._generation:
                    return False
            noise = np.random.default_rng(0).standard_normal(_WARMUP_SAMPLES) * _WARMUP_LEVEL
            self._recognize(noise.astype(np.float32))
        except Exception as exc:
            with self._state_lock:
                superseded = generation != self._generation or self.should_cancel
            if superseded:
                logger.info("%s warmup stopped by a reload or cancel", self.name)
            else:
                logger.warning("%s warmup failed (non-fatal): %s", self.name, exc)
            return False
        finally:
            self._decode_lock.release()
        logger.info("%s warmed up in %.0f ms", self.name, (time.perf_counter() - started) * 1000)
        return True

    def stream_audio(self, session: str, audio: np.ndarray, language=None, *, finish=False):
        with self._decode_lock:
            return self._request_audio("stream", audio, language, session=session, finish=finish)["events"]

    def preview_audio(self, audio: np.ndarray, language=None, *, busy=lambda: False):
        """Best-effort short decode; never queue behind durable transcription."""
        if busy() or not self._decode_lock.acquire(blocking=False):
            return None
        try:
            if busy():
                return None
            return self._transcribe_audio(audio, language)
        finally:
            self._decode_lock.release()

    def cancel_stream(self, session: str):
        with self._decode_lock:
            if self._process:
                self._process.request("cancel_stream", session=session, timeout=10)

    def _recognize(self, audio: np.ndarray, language=None, phrases=()) -> dict:
        options = {"phrases": list(phrases)} if phrases else {}
        return self._request_audio("transcribe", audio, language, **options)

    def recognize(self, audio: np.ndarray, language=None, phrases=()) -> dict:
        """One window's raw worker result, ``{text, segments}``.

        What a remote engine host serves for a client's window decode. It
        queues on the decode lock with this computer's own dictation and
        preview, so the worker only ever sees one request at a time.
        ``phrases`` are dropped unless this engine boosts them.
        """
        phrases = tuple(phrases) if self._phrases_reach_model() else ()
        with self._decode_lock:
            return self._recognize(
                np.asarray(audio, dtype=np.float32), language, **_phrase_options(phrases)
            )

    def request_language(self) -> str:
        """The language a request made without one asks the worker for."""
        from services.local_asr.languages import selected_language
        return selected_language(self.backend_id, self._settings().get("local_asr_language", "en"))

    def _phrases_reach_model(self) -> bool:
        from services.recognition_context import SUPPORT_MODEL, engine_support

        return engine_support(self.backend_id) == SUPPORT_MODEL

    @property
    def recognition_support(self) -> str:
        from services.recognition_context import SUPPORT_AFTER, SUPPORT_MODEL

        return SUPPORT_MODEL if self._phrases_reach_model() else SUPPORT_AFTER

    def recognition_inputs(self, recognition) -> tuple[str, tuple[str, ...]]:
        """The language and phrases a decode for ``recognition`` sends.

        Without a language of its own the context leaves the engine's
        setting in charge; a language the engine doesn't list becomes auto.
        Phrases go only to an engine that boosts them.
        """
        code = recognition.language if recognition else ""
        if not code:
            language = self.request_language()
        else:
            from services.local_asr.languages import language_choices, selected_language

            language = (
                selected_language(self.backend_id, code)
                if language_choices(self.backend_id) else code
            )
        phrases = tuple(recognition.phrases) if recognition and self._phrases_reach_model() else ()
        return language, phrases

    def _request_audio(self, op, audio, language=None, **options) -> dict:
        if self.should_cancel:
            raise RuntimeError("Transcription canceled")
        with self._state_lock:
            process = self._process
        if process is None:
            raise RuntimeError(self.last_error or "Speech engine is not loaded")
        if self.backend_id == "qwen_asr" and op == "transcribe" and not self._qwen_has_speech(audio):
            return dict(text="", segments=[])
        with tempfile.TemporaryDirectory(prefix="openwhisper-asr-") as directory:
            path = os.path.join(directory, "audio.f32")
            np.asarray(audio, dtype=np.float32).tofile(path)
            language = language or self.request_language()
            result = process.request(op, audio_path=path, language=language, timeout=300, **options)
        if self.should_cancel:
            raise RuntimeError("Transcription canceled")
        return result

    def _qwen_has_speech(self, audio) -> bool:
        from services.local_asr.qwen_speech import has_speech

        speech = True
        try:
            speech = has_speech(audio)
        except Exception:
            # A detector failure must not discard a user's recording.
            logger.warning("Qwen speech detection failed; decoding the original audio", exc_info=True)
        if self.should_cancel:
            raise RuntimeError("Transcription canceled")
        return speech

    def _remote_whisper(self) -> bool:
        """Whether this backend should send Whisper a long clip, not 30 s windows."""
        return self.is_remote and self.backend_id == "local_whisper"

    def transcribe(self, audio_path: str, recognition=None) -> str:
        from services.local_asr.audio import joined_windows, windows
        source = windows(audio_path)
        if self._remote_whisper():
            # One clip so the host's faster-whisper can VAD and overlap its
            # own 30 s windows, matching a local file decode. Independent
            # 30 s requests dropped and repeated whole sentences.
            source = joined_windows(source)
        if not recognition:
            return self.join_texts(self.transcribe_windows(source))
        language, phrases = self.recognition_inputs(recognition)
        return self.join_texts(self.transcribe_windows(
            source, language, **_phrase_options(phrases)
        ))

    def transcribe_windows(self, windows, language=None, *, phrases=()) -> list[str]:
        """Decode ``(offset, audio)`` windows as one durable transcription.

        ``transcribe`` passes a file's ``windows()``; incremental dictation
        passes the windows a recording had left at stop. Both mark the engine
        busy for the cancel flow and refuse a canceled or unloaded engine the
        same way. Returns each window's text, in order.
        """
        with self._decode_lock:
            self.is_transcribing = True
            try:
                if self.should_cancel:
                    raise RuntimeError("Transcription canceled")
                if not self.is_available():
                    raise RuntimeError(self.device_info)
                if self.backend_id == "qwen_asr" and self.device == "cuda" and not self.is_remote:
                    return self._transcribe_batched(windows, language)
                return [self._transcribe_audio(audio, language, phrases)["text"]
                        for _offset, audio in windows]
            finally:
                self.is_transcribing = False

    def _transcribe_batched(self, windows, language) -> list[str]:
        """Qwen on this computer's GPU: several windows per worker request.

        Windows are read only as far as the next request needs, so a long
        file is never held in memory, and those without speech are skipped
        before they take a place in a batch. Every request goes to the
        worker this transcription started on: once a reload or cancel
        closes it, the next one fails instead of joining two models' text.
        """
        from services.local_asr.qwen_batch import MAX_BATCH_SIZE

        with self._state_lock:
            process = self._process
        language = language or self.request_language()
        texts, group, limit = [], [], MAX_BATCH_SIZE
        for _offset, audio in windows:
            if self.should_cancel:
                raise RuntimeError("Transcription canceled")
            texts.append("")
            if self._audible(audio) and self._qwen_has_speech(audio):
                group.append((len(texts) - 1, audio))
            if len(group) >= limit:
                limit = self._decode_batch(process, group, language, limit, texts)
                group = []
        if group:
            self._decode_batch(process, group, language, limit, texts)
        return texts

    def _decode_batch(self, process, group, language, limit, texts) -> int:
        """Fill ``texts`` for ``group``'s ``(index, audio)`` windows in one request.

        Returns the limit for the rest of the file, lower once the GPU has run
        out of memory (services/local_asr/qwen_batch.py).
        """
        with tempfile.TemporaryDirectory(prefix="openwhisper-asr-") as directory:
            paths = [os.path.join(directory, f"audio-{number}.f32") for number in range(len(group))]
            for path, (_index, audio) in zip(paths, group):
                np.asarray(audio, dtype=np.float32).tofile(path)
            # A batch gets as long as its windows had one request each.
            result = process.request("transcribe_batch", audio_paths=paths, language=language,
                                     batch_size=limit, timeout=300 * len(group))
        if self.should_cancel:
            raise RuntimeError("Transcription canceled")
        for (index, _audio), text in zip(group, result["texts"], strict=True):
            texts[index] = text
        return result["batch_size"]

    def decode_window(self, audio: np.ndarray, language=None, *, phrases=()) -> str:
        """One window's text, decoded exactly as ``transcribe_windows`` would.

        For windows decoded while a dictation is still being recorded. It
        queues with the preview on the decode lock but leaves
        ``is_transcribing`` alone: a recording is not yet a job, so a cancel
        press must not tear the engine down over it.
        """
        with self._decode_lock:
            return self._transcribe_audio(audio, language, phrases)["text"]

    @staticmethod
    def join_texts(texts) -> str:
        """A transcript from its windows' texts; both decode paths join here.

        A window without speech has no text and leaves no extra space.
        """
        return " ".join(filter(None, (text.strip() for text in texts)))

    @staticmethod
    def _audible(audio) -> bool:
        """Whether a window rises above digital silence; quieter ones are never decoded."""
        return bool(audio.size) and np.max(np.abs(audio)) > .00025

    def _transcribe_audio(self, audio, language=None, phrases=()):
        if self._remote_whisper():
            return self._transcribe_remote_whisper(audio, language, phrases)
        texts, segments = [], []
        start = 0
        # Bound attention memory and output lengths. Prefer a quiet boundary
        # near 25 seconds so ordinary speech isn't cut in the middle of a word.
        while start < len(audio):
            if self.should_cancel:
                raise RuntimeError("Transcription canceled")
            from services.local_asr.audio import split_point
            end = start + split_point(audio[start:])
            window = audio[start:end]
            if self._audible(window):
                result = self._recognize(window, language, **_phrase_options(phrases))
                texts.append(result["text"])
                for segment in result.get("segments", []):
                    segments.append(dict(segment, start=segment["start"]+start/16000,
                                         end=segment["end"]+start/16000))
            start = end
        return dict(text=self.join_texts(texts), segments=segments)

    def _transcribe_remote_whisper(self, audio, language=None, phrases=()):
        """Send Whisper as much audio as one request holds, overlapping the rest.

        A local file decode is one faster-whisper call over the whole
        recording. Remote dictation used to cut that into independent 30 s
        windows and join the texts with a space, which dropped and repeated
        sentences in the middle of a ~4-minute clip. One long request matches
        the local path. Clips longer than the frame limit keep a short overlap
        so a sentence on the seam is in both requests; the later one owns it.
        """
        from services.local_asr.audio import SAMPLE_RATE, split_point
        from services.remote_asr.protocol import MAX_AUDIO_SECONDS, REMOTE_WHISPER_OVERLAP_S

        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        limit = MAX_AUDIO_SECONDS * SAMPLE_RATE
        overlap = int(REMOTE_WHISPER_OVERLAP_S * SAMPLE_RATE)
        options = _phrase_options(phrases)
        collected = []
        start = 0
        keep_from = 0.0
        while start < len(audio):
            if self.should_cancel:
                raise RuntimeError("Transcription canceled")
            end = start + split_point(audio[start:], max_samples=limit)
            window = audio[start:end]
            origin = start / SAMPLE_RATE
            if self._audible(window):
                result = self._recognize(window, language, **options)
                kept = []
                for segment in result.get("segments", []):
                    abs_start = float(segment.get("start") or 0.0) + origin
                    abs_end = float(segment.get("end") or 0.0) + origin
                    text = str(segment.get("text") or "").strip()
                    if text and abs_end > keep_from:
                        kept.append(dict(segment, text=text, start=abs_start, end=abs_end))
                if not kept:
                    text = str(result.get("text") or "").strip()
                    duration = len(window) / SAMPLE_RATE
                    if text and origin + duration > keep_from:
                        kept.append({"text": text, "start": origin, "end": origin + duration})
                if kept:
                    collected = [
                        seg for seg in collected if float(seg["start"]) < origin - 1e-6
                    ]
                    collected.extend(kept)
            if end >= len(audio):
                break
            nxt = end - overlap
            if nxt <= start:
                nxt = end
            keep_from = end / SAMPLE_RATE
            start = nxt
        return dict(text=self.join_texts(seg["text"] for seg in collected), segments=collected)


class SpeechDecoder:
    """Compatibility boundary for the meeting pipeline's timestamped decoder."""

    def __init__(self, backend):
        self.backend = backend

    def transcribe(self, audio, *, language=None, **_whisper_options):
        with self.backend._decode_lock:
            self.backend.is_transcribing = True
            try:
                result = self.backend._transcribe_audio(np.asarray(audio, dtype=np.float32), language or "auto")
            finally:
                self.backend.is_transcribing = False
        segments = [SimpleNamespace(**s, avg_logprob=0., no_speech_prob=0., words=None) for s in result["segments"]]
        return iter(segments), SimpleNamespace(language=language or "en")
