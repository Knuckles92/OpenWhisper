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
        SpeechModel("orukeet-v0.1", "parakeet", "Orukeet TDT 0.6B", "Community adaptation of Parakeet for multilingual transcription", "CC-BY-SA-4.0", "25 European languages, including Russian and English", meeting=True),
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
RUNTIME_IDS = ("asr-nvidia-cpu", "asr-nvidia-cuda", "asr-nvidia-vulkan", "asr-qwen", "asr-moonshine")
#: NVIDIA's CUDA release of NeMo-Speech.cpp runs on Turing and newer. Older
#: NVIDIA GPUs, such as the GTX 10 series, run its Vulkan release instead.
CUDA_MIN_COMPUTE_CAPABILITY = (7, 5)
NVIDIA_VULKAN_RUNTIME = "asr-nvidia-vulkan"


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


def nvidia_gpu_runtime() -> str:
    """The NVIDIA Speech GPU runtime for this computer's card.

    The "cuda" device means the NVIDIA GPU. It runs on the CUDA release,
    except on a card older than Turing where this platform has the Vulkan
    release. A card of unknown compute capability keeps CUDA.
    """
    from services.components import component_is_published
    from services.gpu_info import nvidia_gpu

    gpu = nvidia_gpu()
    capability = gpu.compute_capability if gpu is not None else None
    if (capability is not None and capability < CUDA_MIN_COMPUTE_CAPABILITY
            and component_is_published(NVIDIA_VULKAN_RUNTIME)):
        return NVIDIA_VULKAN_RUNTIME
    return "asr-nvidia-cuda"


def runtime_id(backend: str, device: str) -> str:
    if backend == "qwen_asr":
        return "asr-qwen"
    if backend == "moonshine":
        return "asr-moonshine"
    return nvidia_gpu_runtime() if device == "cuda" else "asr-nvidia-cpu"


def resolve_runtime(backend: str, requested: str) -> tuple[str, str]:
    from services.components import is_installed

    device = requested
    if device == "auto":
        try:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
        except Exception:
            device = "cpu"
        # The Vulkan runtime needs no CUDA libraries, only the card, which
        # CTranslate2 can't count without them.
        if device == "cpu" and backend in ("parakeet", "nemotron") and runtime_id(backend, "cuda") == NVIDIA_VULKAN_RUNTIME:
            device = "cuda"
    component = runtime_id(backend, device)
    # Explicit CUDA must never silently fall back to CPU.
    if requested == "auto" and not is_installed(component) and backend in ("parakeet", "nemotron"):
        component, device = runtime_id(backend, "cpu"), "cpu"
    return component, device


def missing_runtime(model_name: str, settings: dict) -> str | None:
    from services.components import component_is_published, is_installed

    model = MODELS.get(model_name)
    if model is None:
        return None
    component, _device = resolve_runtime(model.backend, selected_device(model.backend, settings))
    return component if component_is_published(component) and not is_installed(component) else None


def runtime_catalog() -> dict:
    entries = {}
    for key, filename in (("asr-qwen", "qwen_runtime.json"), ("asr-moonshine", "moonshine_runtime.json"), ("asr-nvidia-cpu", "nvidia_cpu_runtime.json"), ("asr-nvidia-cuda", "nvidia_cuda_runtime.json")):
        with Path(__file__).with_name(filename).open(encoding="utf-8-sig") as stream:
            entries[key] = {"platforms": {"win_amd64": json.load(stream)}}
    # macOS and Linux runtimes are only NeMo-Speech.cpp's native libraries;
    # the app's own interpreter runs the worker there.
    for key, platform, filename in (
        ("asr-nvidia-cpu", "darwin_arm64", "nvidia_macos_runtime.json"),
        ("asr-nvidia-cpu", "linux_x86_64", "nvidia_linux_cpu_runtime.json"),
        ("asr-nvidia-cuda", "linux_x86_64", "nvidia_linux_cuda_runtime.json"),
        (NVIDIA_VULKAN_RUNTIME, "linux_x86_64", "nvidia_linux_vulkan_runtime.json"),
    ):
        with Path(__file__).with_name(filename).open(encoding="utf-8") as stream:
            entries.setdefault(key, {"platforms": {}})["platforms"][platform] = json.load(stream)
    return entries
