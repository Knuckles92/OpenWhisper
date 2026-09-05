from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SpeechModel:
    key: str
    backend: str
    label: str
    purpose: str
    license: str
    languages: str
    streaming: bool = False
    meeting: bool = False


MODELS = {
    m.key: m for m in (
        SpeechModel("parakeet-v3", "parakeet", "Parakeet TDT 0.6B v3", "Fast dictation and files", "CC-BY-4.0", "25 European languages", meeting=True),
        SpeechModel("qwen-0.6b", "qwen_asr", "Qwen3-ASR 0.6B", "Efficient multilingual transcription", "Apache-2.0", "30 languages and 22 Chinese dialects"),
        SpeechModel("qwen-1.7b", "qwen_asr", "Qwen3-ASR 1.7B", "Accuracy-focused transcription", "Apache-2.0", "30 languages and 22 Chinese dialects"),
        SpeechModel("nemotron-3.5", "nemotron", "Nemotron 3.5 ASR 0.6B", "Live speech recognition", "OpenMDW-1.1", "Multilingual; coverage varies by locale", streaming=True, meeting=True),
        SpeechModel("moonshine-small", "moonshine", "Moonshine Streaming Small", "Fast CPU transcription", "MIT", "English", streaming=True, meeting=True),
        SpeechModel("moonshine-medium", "moonshine", "Moonshine Streaming Medium", "CPU transcription with a larger model", "MIT", "English", streaming=True, meeting=True),
    )
}
BACKENDS = {
    "parakeet": "Parakeet",
    "qwen_asr": "Qwen3-ASR",
    "nemotron": "Nemotron Streaming",
    "moonshine": "Moonshine",
}
#: Backend id of the built-in faster-whisper family, which is not in MODELS.
WHISPER_BACKEND = "local_whisper"
DEFAULT_MODELS = {key: next(m.key for m in MODELS.values() if m.backend == key) for key in BACKENDS}
RUNTIME_IDS = ("asr-nvidia-cpu", "asr-nvidia-cuda", "asr-qwen", "asr-moonshine")


def backend_of(model_name: str) -> str:
    """Return the backend id that owns a catalog model name (Whisper names included)."""
    return MODELS[model_name].backend if model_name in MODELS else WHISPER_BACKEND


def artifacts(key: str) -> dict:
    with Path(__file__).with_name("models.json").open(encoding="utf-8-sig") as stream:
        return json.load(stream)[key]


def selected_model(backend: str, settings: dict) -> str:
    key = settings.get("local_asr_models", {}).get(backend) if isinstance(settings.get("local_asr_models"), dict) else None
    return key if key in MODELS and MODELS[key].backend == backend else DEFAULT_MODELS[backend]


def selected_device(backend: str, settings: dict) -> str:
    devices = settings.get("local_asr_devices", {})
    device = devices.get(backend) if isinstance(devices, dict) else None
    return device if device in ("auto", "cpu", "cuda") and backend != "moonshine" else ("cpu" if backend == "moonshine" else "auto")


def runtime_id(backend: str, device: str) -> str:
    if backend == "qwen_asr":
        return "asr-qwen"
    if backend == "moonshine":
        return "asr-moonshine"
    return "asr-nvidia-cuda" if device == "cuda" else "asr-nvidia-cpu"


def runtime_catalog() -> dict:
    entries = {}
    for key, filename in (("asr-qwen", "qwen_runtime.json"), ("asr-moonshine", "moonshine_runtime.json"), ("asr-nvidia-cpu", "nvidia_cpu_runtime.json"), ("asr-nvidia-cuda", "nvidia_cuda_runtime.json")):
        with Path(__file__).with_name(filename).open(encoding="utf-8-sig") as stream:
            entries[key] = {"platforms": {"win_amd64": json.load(stream)}}
    return entries

