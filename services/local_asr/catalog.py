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
        SpeechModel("parakeet-v3-mlx", "parakeet_mlx", "Parakeet TDT 0.6B v3 (MLX)", "Apple Silicon dictation and files", "CC-BY-4.0", "25 European languages", meeting=True),
    )
}
BACKENDS = {
    "parakeet": "Parakeet",
    "qwen_asr": "Qwen3-ASR",
    "nemotron": "Nemotron Streaming",
    "moonshine": "Moonshine",
    "parakeet_mlx": "Parakeet MLX",
}
#: Backend id of the built-in faster-whisper family, which is not in MODELS.
WHISPER_BACKEND = "local_whisper"
DEFAULT_MODELS = {key: next(m.key for m in MODELS.values() if m.backend == key) for key in BACKENDS}
MLX_RUNTIME = "asr-parakeet-mlx"
#: NeMo-Speech.cpp's Metal release: Parakeet and Nemotron on the Apple GPU.
NVIDIA_METAL_RUNTIME = "asr-nvidia-metal"
RUNTIME_IDS = ("asr-nvidia-cpu", "asr-nvidia-cuda", "asr-nvidia-vulkan", NVIDIA_METAL_RUNTIME, "asr-qwen", "asr-moonshine", MLX_RUNTIME)
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
    if backend == "parakeet_mlx":
        return device if device in ("auto", "cpu") else "auto"
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


def apple_silicon() -> bool:
    """True on an Apple Silicon Mac, where Auto means the Apple GPU, never CUDA."""
    from services.components import current_platform_tag

    return current_platform_tag() == "darwin_arm64"


def runtime_id(backend: str, device: str) -> str:
    if backend == "parakeet_mlx":
        return MLX_RUNTIME
    if backend == "qwen_asr":
        return "asr-qwen"
    if backend == "moonshine":
        return "asr-moonshine"
    if device != "cpu" and apple_silicon():
        return NVIDIA_METAL_RUNTIME
    return nvidia_gpu_runtime() if device == "cuda" else "asr-nvidia-cpu"


def resolve_runtime(backend: str, requested: str) -> tuple[str, str]:
    from services.components import is_installed

    if backend == "parakeet_mlx":
        if requested not in ("auto", "cpu"):
            raise ValueError("Parakeet MLX supports Auto (Apple GPU) or CPU, not CUDA.")
        return MLX_RUNTIME, "cpu" if requested == "cpu" else "metal"
    if backend in ("qwen_asr", "parakeet", "nemotron") and apple_silicon():
        # A Mac's GPU is the Apple GPU: Metal for NeMo-Speech.cpp, MPS for
        # PyTorch. A saved "cuda" from another computer means that GPU too.
        if requested == "cpu":
            return runtime_id(backend, "cpu"), "cpu"
        if backend == "qwen_asr":
            return "asr-qwen", "mps"
        # Auto keeps an installed CPU runtime until the Metal one is added.
        if (requested == "auto" and is_installed("asr-nvidia-cpu")
                and not is_installed(NVIDIA_METAL_RUNTIME)):
            return "asr-nvidia-cpu", "cpu"
        return NVIDIA_METAL_RUNTIME, "metal"
    device = _auto_device(backend) if requested == "auto" else requested
    component = runtime_id(backend, device)
    # Explicit CUDA must never silently fall back to CPU.
    if requested == "auto" and not is_installed(component) and backend in ("parakeet", "nemotron"):
        component, device = runtime_id(backend, "cpu"), "cpu"
    return component, device


def _auto_device(backend: str) -> str:
    """The device Auto runs on once its runtime is installed (not Apple Silicon)."""
    try:
        import ctranslate2
        device = "cuda" if ctranslate2.get_cuda_device_count() else "cpu"
    except Exception:
        device = "cpu"
    # The Vulkan runtime needs no CUDA libraries, only the card, which
    # CTranslate2 can't count without them.
    if device == "cpu" and backend in ("parakeet", "nemotron") and runtime_id(backend, "cuda") == NVIDIA_VULKAN_RUNTIME:
        device = "cuda"
    return device


def _vulkan_loader_present() -> bool:
    import ctypes

    try:
        ctypes.CDLL("libvulkan.so.1")
    except OSError:
        return False
    return True


def gpu_runtime_offer(model_name: str, settings: dict) -> str | None:
    """The GPU runtime to offer alongside Auto's CPU fallback on first use.

    Auto runs Parakeet and Nemotron on an installed GPU runtime, else on the
    CPU one, so ``missing_runtime`` asks a fresh install for the CPU runtime
    even where the NVIDIA GPU would run the model several times faster. This
    names the runtime Auto would pick once installed (CUDA, or Vulkan for an
    older card on Linux) while no NVIDIA Speech runtime is installed and this
    computer can load it. None otherwise, and for an explicit CPU or CUDA
    choice, which ``missing_runtime`` already answers.
    """
    from services.components import (
        catalog_entry_for_platform, check_compatibility, component_is_published, is_installed,
    )
    from services.gpu_info import nvidia_gpu

    model = MODELS.get(model_name)
    if (model is None or model.backend not in ("parakeet", "nemotron") or apple_silicon()
            or selected_device(model.backend, settings) != "auto"
            or is_installed(runtime_id(model.backend, "cpu"))):
        return None
    gpu = nvidia_gpu()
    if gpu is None or _auto_device(model.backend) != "cuda":
        return None
    component = runtime_id(model.backend, "cuda")
    if component == NVIDIA_VULKAN_RUNTIME:
        if not _vulkan_loader_present():
            return None
    # NVIDIA's CUDA release runs on Turing and newer; a card of unknown
    # capability isn't offered a download it may not run.
    elif gpu.compute_capability is None or gpu.compute_capability < CUDA_MIN_COMPUTE_CAPABILITY:
        return None
    if (is_installed(component) or not component_is_published(component)
            or check_compatibility(catalog_entry_for_platform(component) or {})):
        return None
    return component


def missing_runtime(model_name: str, settings: dict) -> str | None:
    from services.components import (
        catalog_entry_for_platform, check_compatibility, component_is_published, is_installed,
    )

    model = MODELS.get(model_name)
    if model is None:
        return None
    component, _device = resolve_runtime(model.backend, selected_device(model.backend, settings))
    if not component_is_published(component) or is_installed(component):
        return None
    # Never offer a runtime this computer cannot load, such as Moonshine's
    # macOS 15 build on macOS 14.
    return None if check_compatibility(catalog_entry_for_platform(component) or {}) else component


def runtime_catalog() -> dict:
    entries = {}
    for key, filename in (("asr-qwen", "qwen_runtime.json"), ("asr-moonshine", "moonshine_runtime.json"), ("asr-nvidia-cpu", "nvidia_cpu_runtime.json"), ("asr-nvidia-cuda", "nvidia_cuda_runtime.json")):
        with Path(__file__).with_name(filename).open(encoding="utf-8-sig") as stream:
            entries[key] = {"platforms": {"win_amd64": json.load(stream)}}
    # The app's interpreter runs workers on macOS/Linux, using downloaded
    # native libraries or a macOS wheel tree (MLX, Qwen, Moonshine).
    for key, platform, filename in (
        (MLX_RUNTIME, "darwin_arm64", "mlx_runtime.json"),
        ("asr-nvidia-cpu", "darwin_arm64", "nvidia_macos_runtime.json"),
        ("asr-nvidia-cpu", "darwin_x86_64", "nvidia_macos_intel_runtime.json"),
        (NVIDIA_METAL_RUNTIME, "darwin_arm64", "nvidia_macos_metal_runtime.json"),
        ("asr-qwen", "darwin_arm64", "qwen_macos_runtime.json"),
        ("asr-moonshine", "darwin_arm64", "moonshine_macos_runtime.json"),
        ("asr-nvidia-cpu", "linux_x86_64", "nvidia_linux_cpu_runtime.json"),
        ("asr-nvidia-cuda", "linux_x86_64", "nvidia_linux_cuda_runtime.json"),
        (NVIDIA_VULKAN_RUNTIME, "linux_x86_64", "nvidia_linux_vulkan_runtime.json"),
    ):
        with Path(__file__).with_name(filename).open(encoding="utf-8") as stream:
            entries.setdefault(key, {"platforms": {}})["platforms"][platform] = json.load(stream)
    return entries
