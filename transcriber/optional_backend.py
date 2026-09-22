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


class LocalSpeechBackend(TranscriptionBackend):
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
    def requires_file_splitting(self):
        return False

    @property
    def is_model_missing(self):
        return not cache.is_cached(self.model_name)

    @property
    def last_loaded_model(self):
        return self.model_name if self.is_available() else None

    @property
    def device_info(self):
        return f"{MODELS[self.model_name].label} | {self.device}" if self.is_available() else self.last_error or f"{self.name} is not loaded"

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

    def reload_model(self, model_name=None):
        with self._state_lock:
            self.cleanup()
            self.reset_cancel_flag()
            generation = self._generation
        settings = self._settings()
        self.model_name = model_name or self._model_override or selected_model(self.backend_id, settings)
        if self.model_name not in MODELS or MODELS[self.model_name].backend != self.backend_id:
            raise ValueError("Model does not belong to this backend")
        from services.components import component_dir, is_installed
        requested = self._device_override or selected_device(self.backend_id, settings)
        component, device = resolve_runtime(self.backend_id, requested)
        self.runtime_component = component
        if not is_installed(component):
            from services.components import catalog_entry_for_platform
            if catalog_entry_for_platform(component) is None:
                self.runtime_component = None
                self.last_error = f"{self.name}'s {device.upper()} runtime is not available on this platform."
            else:
                self.last_error = f"Install {self.name}'s {'GPU' if device == 'cuda' else 'CPU'} runtime in Downloads."
            return
        if self.is_model_missing:
            self.last_error = f"Download {MODELS[self.model_name].label} in Downloads."
            return
        from services.local_asr.process import SpeechProcess
        with self._state_lock:
            if generation != self._generation:
                return
            python = sys.executable if sys.platform == "darwin" else str(Path(component_dir(component)) / "python.exe")
            process = SpeechProcess(python)
            self._process = process
        try:
            result = process.request("load", backend=self.backend_id, model=self.model_name,
                                     model_path=cache.load_path(self.model_name),
                                     runtime=component_dir(component), device=device, timeout=300)
            with self._state_lock:
                if generation != self._generation:
                    process.close()
                    return
                self.device = result["device"]
                self.model = SpeechDecoder(self)
                self.last_error = ""
        except Exception as exc:
            process.close()
            with self._state_lock:
                if generation == self._generation:
                    self._process = None
                    self.last_error = str(exc)
            raise

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

    def _recognize(self, audio: np.ndarray, language=None) -> dict:
        return self._request_audio("transcribe", audio, language)

    def request_language(self) -> str:
        """The language a request made without one asks the worker for."""
        return self._settings().get("local_asr_language", "en")

    def _request_audio(self, op, audio, language=None, **options) -> dict:
        if self.should_cancel:
            raise RuntimeError("Transcription canceled")
        with self._state_lock:
            process = self._process
        if process is None:
            raise RuntimeError(self.last_error or "Speech engine is not loaded")
        with tempfile.TemporaryDirectory(prefix="openwhisper-asr-") as directory:
            path = os.path.join(directory, "audio.f32")
            np.asarray(audio, dtype=np.float32).tofile(path)
            language = language or self.request_language()
            result = process.request(op, audio_path=path, language=language, timeout=300, **options)
        if self.should_cancel:
            raise RuntimeError("Transcription canceled")
        return result

    def transcribe(self, audio_path: str) -> str:
        from services.local_asr.audio import windows
        return self.join_texts(self.transcribe_windows(windows(audio_path)))

    def transcribe_windows(self, windows, language=None) -> list[str]:
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
                return [self._transcribe_audio(audio, language)["text"]
                        for _offset, audio in windows]
            finally:
                self.is_transcribing = False

    def decode_window(self, audio: np.ndarray, language=None) -> str:
        """One window's text, decoded exactly as ``transcribe_windows`` would.

        For windows decoded while a dictation is still being recorded. It
        queues with the preview on the decode lock but leaves
        ``is_transcribing`` alone: a recording is not yet a job, so a cancel
        press must not tear the engine down over it.
        """
        with self._decode_lock:
            return self._transcribe_audio(audio, language)["text"]

    @staticmethod
    def join_texts(texts) -> str:
        """A transcript from its windows' texts; both decode paths join here."""
        return " ".join(texts).strip()

    def _transcribe_audio(self, audio, language=None):
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
            if window.size and np.max(np.abs(window)) > .00025:
                result = self._recognize(window, language)
                texts.append(result["text"])
                for segment in result.get("segments", []):
                    segments.append(dict(segment, start=segment["start"]+start/16000,
                                         end=segment["end"]+start/16000))
            start = end
        return dict(text=" ".join(texts).strip(), segments=segments)


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
