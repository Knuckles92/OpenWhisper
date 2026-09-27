"""What a host serves: a thin view of its selected transcription engine.

``host_engine_for`` wraps the backend the host has selected. The optional
engines (Parakeet, Nemotron, Qwen3-ASR, Moonshine) already take window
decodes and stream pushes, so they pass straight through. Local Whisper has
no worker; its loaded faster-whisper model decodes the window here and
returns the same ``{text, segments}`` shape the worker does. ``host_models``
lists the models a paired computer may switch the host to.
"""
from __future__ import annotations

import threading
from typing import Optional

import numpy as np

from config import config


class HostEngine:
    """The interface ``SpeechHost`` calls. Methods run on connection threads."""

    #: Changes when the host switches engine or model; clients reconnect.
    identity: tuple = ()

    def describe(self) -> dict:
        raise NotImplementedError

    def transcribe(self, audio: np.ndarray, language: Optional[str]) -> dict:
        raise NotImplementedError

    def stream(self, session: str, audio: np.ndarray, language: Optional[str], finish: bool) -> dict:
        raise RuntimeError("The host's engine has no live stream")

    def cancel_stream(self, session: str) -> None:
        return None


class UnavailableEngine(HostEngine):
    def __init__(self, reason: str):
        self.reason = reason
        self.identity = ("unavailable", reason)

    def describe(self) -> dict:
        return {"family": "", "model": "", "label": "", "device": "",
                "streaming": False, "available": False, "status": self.reason}

    def transcribe(self, audio, language):
        raise RuntimeError(self.reason)


class SpeechWorkerEngine(HostEngine):
    """An optional engine's worker, shared with the host's own dictation.

    Requests queue on the backend's decode lock, so a client's decode and the
    host's own never run on the worker at once.
    """

    def __init__(self, backend):
        self.backend = backend

    @property
    def identity(self) -> tuple:
        from services.local_asr.catalog import selected_device
        from services.settings import SettingsKey, settings_manager

        settings = settings_manager.load_all_settings()
        return ("worker", self.backend.backend_id, self.backend.model_name, self.backend.device,
                selected_device(self.backend.backend_id, settings),
                settings.get(SettingsKey.LOCAL_ASR_LANGUAGE, "en"))

    def describe(self) -> dict:
        from services.local_asr.catalog import MODELS

        backend = self.backend
        model = MODELS.get(backend.model_name)
        return {
            "family": backend.backend_id,
            "model": backend.model_name,
            "label": model.label if model else backend.name,
            "device": backend.device,
            "streaming": bool(model and model.streaming),
            "available": backend.is_available(),
            "status": backend.device_info,
        }

    def transcribe(self, audio, language):
        return self.backend.recognize(audio, language)

    def stream(self, session, audio, language, finish):
        return {"events": self.backend.stream_audio(session, audio, language, finish=finish)}

    def cancel_stream(self, session):
        self.backend.cancel_stream(session)


class WhisperEngine(HostEngine):
    """Local Whisper's loaded model, decoding one window per request."""

    def __init__(self, backend):
        self.backend = backend
        self._lock = threading.Lock()

    @property
    def identity(self) -> tuple:
        # Device and compute type count: turbo moving from the CPU to the GPU
        # is a different engine to a client, as much as another model is.
        from services.settings import SettingsKey, settings_manager

        settings = settings_manager.load_all_settings()
        return ("whisper", self._model_name(), self.backend.device,
                getattr(self.backend, "compute_type", None),
                settings.get(SettingsKey.WHISPER_DEVICE, "auto"),
                settings.get(SettingsKey.WHISPER_COMPUTE_TYPE, "auto"))

    def _model_name(self) -> str:
        return self.backend.last_loaded_model or getattr(self.backend, "model_name", "") or ""

    def describe(self) -> dict:
        backend = self.backend
        name = self._model_name()
        return {
            "family": "local_whisper",
            "model": name,
            "label": f"Whisper {name}".strip(),
            "device": backend.device or "",
            "compute_type": getattr(backend, "compute_type", None) or "",
            "streaming": False,
            "available": backend.is_available(),
            "status": backend.device_info,
        }

    def transcribe(self, audio, language):
        model = self.backend.model
        if model is None:
            raise RuntimeError("Whisper is not loaded on the host")
        options = {}
        if config.FASTER_WHISPER_VAD_ENABLED:
            options["vad_parameters"] = dict(
                min_silence_duration_ms=config.FASTER_WHISPER_VAD_MIN_SILENCE_MS
            )
        with self._lock:
            segments, _info = model.transcribe(
                np.asarray(audio, dtype=np.float32),
                language=whisper_language(language),
                beam_size=config.FASTER_WHISPER_BEAM_SIZE,
                vad_filter=config.FASTER_WHISPER_VAD_ENABLED,
                **options,
            )
            # faster-whisper decodes while the generator is consumed.
            parts = [
                {"text": segment.text.strip(), "start": float(segment.start), "end": float(segment.end)}
                for segment in segments
            ]
        text = " ".join(part["text"] for part in parts if part["text"]).strip()
        return {"text": text, "segments": parts}


def whisper_language(language: Optional[str]) -> Optional[str]:
    """Whisper takes ISO 639-1 codes; ``auto`` or nothing means detect."""
    if not language or language.lower() == "auto":
        return None
    return language.split("-", 1)[0].lower() or None


def host_models() -> list:
    """The models a paired computer may switch this one to.

    Only models that load without asking anything of the person at this
    computer: downloaded, with the runtime installed for the device set here
    for that engine. Each entry is ``{family, model, label}``, with labels
    as ``describe()`` gives them, grouped by engine with Whisper last.
    """
    from services.components import is_installed
    from services.hf_access import resolve_model_repo, scan_cached_models
    from services.local_asr import cache
    from services.local_asr.catalog import MODELS, WHISPER_BACKEND, resolve_runtime, selected_device
    from services.settings import settings_manager

    settings = settings_manager.load_all_settings()
    runtime_ready: dict = {}
    models = []
    for key, model in MODELS.items():
        if model.backend not in runtime_ready:
            component, _device = resolve_runtime(
                model.backend, selected_device(model.backend, settings)
            )
            runtime_ready[model.backend] = is_installed(component)
        if runtime_ready[model.backend] and cache.is_cached(key):
            models.append({"family": model.backend, "model": key, "label": model.label})
    try:
        cached = scan_cached_models(max_age_seconds=30.0)
    except Exception:
        cached = {}
    for name in config.WHISPER_MODEL_CHOICES:
        if name != "auto" and resolve_model_repo(name) in cached:
            models.append({"family": WHISPER_BACKEND, "model": name, "label": f"Whisper {name}"})
    return models


def host_engine_for(backend) -> HostEngine:
    """Wrap the host's selected backend for serving.

    Callers keep the result for as long as the same backend stays selected:
    ``WhisperEngine`` serializes decodes on its own lock.
    """
    if backend is None:
        return UnavailableEngine("No transcription engine is selected on the host")
    if getattr(backend, "is_remote", False):
        return UnavailableEngine(
            "The host is itself using a remote engine. Select a local engine there."
        )
    from transcriber.optional_backend import LocalSpeechBackend

    if isinstance(backend, LocalSpeechBackend):
        return SpeechWorkerEngine(backend)
    from transcriber.local_backend import LocalWhisperBackend

    if isinstance(backend, LocalWhisperBackend):
        return WhisperEngine(backend)
    return UnavailableEngine(
        "The host's selected engine runs in the cloud and can't be shared. "
        "Select a local engine there."
    )
