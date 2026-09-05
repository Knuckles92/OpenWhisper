"""Optional local speech engines running in isolated, persistent processes."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace

import numpy as np

from transcriber.base import TranscriptionBackend
from services.local_asr.catalog import BACKENDS, MODELS, selected_model, selected_device, runtime_id
from services.local_asr import cache


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

    def reload_model(self, model_name=None):
        with self._state_lock:
            self.cleanup()
            self.reset_cancel_flag()
            generation = self._generation
        settings = self._settings()
        self.model_name = model_name or self._model_override or selected_model(self.backend_id, settings)
        if self.model_name not in MODELS or MODELS[self.model_name].backend != self.backend_id:
            raise ValueError("Model does not belong to this backend")
        device = self._device_override or selected_device(self.backend_id, settings)
        if device == "auto":
            try:
                import ctranslate2
                device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
            except Exception:
                device = "cpu"
        from services.components import component_dir, is_installed
        component = runtime_id(self.backend_id, device)
        # Auto can use an installed CPU runtime; explicit CUDA never silently falls back.
        requested = self._device_override or selected_device(self.backend_id, settings)
        if requested == "auto" and not is_installed(component) and self.backend_id in ("parakeet", "nemotron"):
            component, device = runtime_id(self.backend_id, "cpu"), "cpu"
        self.runtime_component = component
        if not is_installed(component):
            self.last_error = f"Install {self.name}'s {'GPU' if device == 'cuda' else 'CPU'} runtime in Downloads."
            return
        if self.is_model_missing:
            self.last_error = f"Download {MODELS[self.model_name].label} in Downloads."
            return
        from services.local_asr.process import SpeechProcess
        with self._state_lock:
            if generation != self._generation:
                return
            process = SpeechProcess(str(Path(component_dir(component)) / "python.exe"))
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

    def stream_audio(self, session: str, audio: np.ndarray, language=None, *, finish=False):
        with self._decode_lock:
            return self._request_audio("stream", audio, language, session=session, finish=finish)["events"]

    def cancel_stream(self, session: str):
        with self._decode_lock:
            if self._process:
                self._process.request("cancel_stream", session=session, timeout=10)

    def _recognize(self, audio: np.ndarray, language=None) -> dict:
        return self._request_audio("transcribe", audio, language)

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
            settings = self._settings()
            language = language or settings.get("local_asr_language", "en")
            result = process.request(op, audio_path=path, language=language, timeout=300, **options)
        if self.should_cancel:
            raise RuntimeError("Transcription canceled")
        return result

    def transcribe(self, audio_path: str) -> str:
        from services.local_asr.audio import windows
        with self._decode_lock:
            self.is_transcribing = True
            try:
                if self.should_cancel:
                    raise RuntimeError("Transcription canceled")
                if not self.is_available():
                    raise RuntimeError(self.device_info)
                return " ".join(self._transcribe_audio(audio)["text"]
                                for _offset, audio in windows(audio_path)).strip()
            finally:
                self.is_transcribing = False

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

