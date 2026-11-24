from __future__ import annotations

import functools
import json
from collections.abc import Iterator, Mapping
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
    #: User-facing model family (e.g. "parakeet", "qwen", "nemotron", "moonshine").
    family: str = ""
    #: Explicit platform tags this model supports; empty means any platform
    #: where its backend's runtime is published.
    platforms: tuple[str, ...] = ()
    #: Language codes accepted by this model; empty falls back to backend defaults.
    language_codes: tuple[str, ...] = ()
    streaming: bool = False
    meeting: bool = False
    #: Whether a finished native stream's text is as good as decoding the
    #: saved recording, so a long dictation may paste it instead of waiting
    #: for the file decode (config.STREAM_FINAL_MIN_SEC).
    stream_final: bool = False
    #: Whether windows are checked with Silero VAD before decoding (for
    #: autoregressive models like Qwen3-ASR / Fourier that hallucinate on silence).
    vad_gate: bool = False
    #: Whether a throwaway decode runs after loading to warm the worker.
    warmup: bool = False
    #: Whether long dictations decode completed 30 s windows while recording.
    incremental: bool = False
    #: Whether the loaded dictation worker can decode live preview windows.
    preview: bool = False
    #: Whether live preview uses the worker's native stream_audio session.
    native_stream_preview: bool = False
    #: Moonshine's model size, "small" or "medium"; "" for other engines.
    arch: str = ""
    #: True for a model the person added (services/local_asr/custom.py).
    custom: bool = False
    #: A custom model's file or folder on this computer.
    path: str = ""
    #: Optional language-scope override for custom models (None uses backend/languages default).
    multilingual: bool | None = None


def is_english_only_model(model: SpeechModel) -> bool:
    """Whether ``model`` only accepts English audio."""
    if model.multilingual is not None:
        return not model.multilingual
    return model.languages == "English"


_EUROPEAN_LANGUAGES = ("en", "ru", "es", "fr", "pt", "auto")
_ALL_LANGUAGES = ("en", "ru", "es", "fr", "pt", "zh", "auto")

BUILTIN_MODELS = {
    m.key: m for m in (
        SpeechModel(
            "parakeet-v3", "parakeet", "Parakeet TDT 0.6B v3", "Fast dictation and files",
            "CC-BY-4.0", "25 European languages", family="parakeet",
            language_codes=_EUROPEAN_LANGUAGES, meeting=True,
            warmup=True, incremental=True, preview=True,
        ),
        SpeechModel(
            "orukeet-v0.1", "parakeet", "Orukeet TDT 0.6B",
            "Community adaptation of Parakeet for multilingual transcription",
            "CC-BY-SA-4.0", "25 European languages, including Russian and English",
            family="parakeet", language_codes=_EUROPEAN_LANGUAGES, meeting=True,
            warmup=True, incremental=True, preview=True,
        ),
        SpeechModel(
            "qwen-0.6b", "qwen_asr", "Qwen3-ASR 0.6B", "Efficient multilingual transcription",
            "Apache-2.0", "30 languages and 22 Chinese dialects", family="qwen",
            language_codes=_ALL_LANGUAGES, vad_gate=True,
        ),
        SpeechModel(
            "qwen-1.7b", "qwen_asr", "Qwen3-ASR 1.7B", "Accuracy-focused transcription",
            "Apache-2.0", "30 languages and 22 Chinese dialects", family="qwen",
            language_codes=_ALL_LANGUAGES, vad_gate=True,
        ),
        SpeechModel(
            "nemotron-3.5", "nemotron", "Nemotron 3.5 ASR 0.6B", "Live speech recognition",
            "OpenMDW-1.1", "Multilingual; coverage varies by locale", family="nemotron",
            language_codes=_ALL_LANGUAGES, streaming=True, meeting=True,
            warmup=True, incremental=True, preview=True, native_stream_preview=True,
        ),
        SpeechModel(
            "moonshine-small", "moonshine", "Moonshine Streaming Small", "Fast CPU transcription",
            "MIT", "English", family="moonshine", language_codes=("en",),
            streaming=True, meeting=True, arch="small",
        ),
        SpeechModel(
            "moonshine-medium", "moonshine", "Moonshine Streaming Medium",
            "CPU transcription with a larger model", "MIT", "English",
            family="moonshine", language_codes=("en",),
            streaming=True, meeting=True, arch="medium",
        ),
        SpeechModel(
            "parakeet-v3-mlx", "parakeet_mlx", "Parakeet TDT 0.6B v3 (MLX)",
            "Apple Silicon dictation and files", "CC-BY-4.0", "25 European languages",
            family="parakeet", platforms=("darwin_arm64",), language_codes=("auto",),
            meeting=True, warmup=True, incremental=True, preview=True,
        ),
        SpeechModel(
            "parakeet-unified-en", "parakeet_unified", "Parakeet Unified EN 0.6B",
            "English dictation with readable live text", "NVIDIA Open Model License", "English",
            family="parakeet", platforms=("win_amd64", "linux_x86_64"),
            language_codes=("en",), streaming=True, stream_final=True,
            warmup=True, incremental=True, preview=True, native_stream_preview=True,
        ),
        SpeechModel(
            "parakeet-110m-en", "parakeet_unified", "Parakeet TDT-CTC 110M",
            "Compact English dictation and meetings", "CC-BY-4.0", "English",
            family="parakeet", platforms=("win_amd64", "linux_x86_64"),
            language_codes=("en",), meeting=True,
            warmup=True, incremental=True, preview=True,
        ),
    )
}


class _SpeechModels(Mapping):
    """Every speech model on this computer: the built-in ones, then the person's own.

    The custom part follows the saved settings, so a model added or removed
    in Settings is seen everywhere without a restart. Code that must offer
    only the published catalog, such as what a host tells paired computers,
    uses ``BUILTIN_MODELS``.
    """

    def __getitem__(self, key: str) -> SpeechModel:
        model = BUILTIN_MODELS.get(key)
        if model is not None:
            return model
        from services.local_asr.custom import CUSTOM_PREFIX, registered
        if not (isinstance(key, str) and key.startswith(CUSTOM_PREFIX)):
            raise KeyError(key)
        return registered()[key]

    def __contains__(self, key: object) -> bool:
        try:
            self[key]
        except KeyError:
            return False
        return True

    def __iter__(self) -> Iterator[str]:
        from services.local_asr.custom import registered
        yield from BUILTIN_MODELS
        yield from registered()

    def __len__(self) -> int:
        from services.local_asr.custom import registered
        return len(BUILTIN_MODELS) + len(registered())


MODELS: Mapping[str, SpeechModel] = _SpeechModels()
BACKENDS = {
    "parakeet": "Parakeet",
    "qwen_asr": "Qwen3-ASR",
    "nemotron": "Nemotron Streaming",
    "moonshine": "Moonshine",
    "parakeet_mlx": "Parakeet MLX",
    "parakeet_unified": "Parakeet Unified",
}
#: Backend id of the built-in faster-whisper family, which is not in MODELS.
WHISPER_BACKEND = "local_whisper"
#: User-facing local model families (grouping runtimes that serve the same family).
WHISPER_FAMILY = "whisper"
MODEL_FAMILIES: Mapping[str, str] = {
    "parakeet": "Parakeet",
    WHISPER_FAMILY: "Whisper",
    "qwen": "Qwen3-ASR",
    "nemotron": "Nemotron",
    "moonshine": "Moonshine",
}
BACKEND_FAMILIES: Mapping[str, str] = {
    WHISPER_BACKEND: WHISPER_FAMILY,
    "parakeet": "parakeet",
    "parakeet_unified": "parakeet",
    "parakeet_mlx": "parakeet",
    "qwen_asr": "qwen",
    "nemotron": "nemotron",
    "moonshine": "moonshine",
}
DEFAULT_MODELS = {key: next(m.key for m in BUILTIN_MODELS.values() if m.backend == key) for key in BACKENDS}
#: Compact model bundled in the all-in-one installer so dictation works offline
#: out of the box without any model download.
ALL_IN_ONE_MODEL = "parakeet-110m-en"
MLX_RUNTIME = "asr-parakeet-mlx"
#: NeMo-Speech.cpp's Metal release: Parakeet and Nemotron on the Apple GPU.
NVIDIA_METAL_RUNTIME = "asr-nvidia-metal"
#: Shared CPU and Vulkan runtime for Parakeet Unified and Qwen on x64.
TRANSCRIBE_RUNTIME = "asr-transcribe"
RUNTIME_IDS = (
    "asr-nvidia-cpu", "asr-nvidia-cuda", "asr-nvidia-vulkan", NVIDIA_METAL_RUNTIME,
    "asr-qwen", "asr-moonshine", MLX_RUNTIME, TRANSCRIBE_RUNTIME,
)
#: NVIDIA's CUDA release of NeMo-Speech.cpp runs on Turing and newer. Older
#: NVIDIA GPUs, such as the GTX 10 series, run its Vulkan release instead.
CUDA_MIN_COMPUTE_CAPABILITY = (7, 5)
NVIDIA_VULKAN_RUNTIME = "asr-nvidia-vulkan"


def backend_of(model_name: str) -> str:
    """Return the backend id that owns a catalog model name (Whisper names included)."""
    return MODELS[model_name].backend if model_name in MODELS else WHISPER_BACKEND


def family_of(model_or_backend: str) -> str:
    """Return the user-facing model family id for a model key or backend id."""
    if model_or_backend in MODEL_FAMILIES:
        return model_or_backend
    if model_or_backend in BACKEND_FAMILIES:
        return BACKEND_FAMILIES[model_or_backend]
    model = MODELS.get(model_or_backend)
    if model is not None:
        return model.family or BACKEND_FAMILIES.get(model.backend, WHISPER_FAMILY)
    return WHISPER_FAMILY


def backends_in_family(family: str) -> tuple[str, ...]:
    """Every backend id grouped under ``family``, in platform-preferred order."""
    from config import parakeet_mlx_supported

    if family == "parakeet":
        return (
            ("parakeet_mlx", "parakeet", "parakeet_unified")
            if parakeet_mlx_supported()
            else ("parakeet_unified", "parakeet", "parakeet_mlx")
        )
    return tuple(backend for backend, fam in BACKEND_FAMILIES.items() if fam == family)


def backend_supported_here(backend: str, platform_tag: str | None = None) -> bool:
    """Whether ``backend`` has a runnable runtime on ``platform_tag`` (default: this host)."""
    if backend in (WHISPER_BACKEND, "api", "remote"):
        return True
    if backend not in BACKENDS:
        return False
    from services.components import current_platform_tag

    tag = platform_tag or current_platform_tag()
    component = _runtime_id_for_platform(backend, tag)
    platforms = runtime_catalog().get(component, {}).get("platforms", {})
    return tag in platforms


def _runtime_id_for_platform(backend: str, platform_tag: str) -> str:
    if backend == "parakeet_mlx":
        return MLX_RUNTIME
    if backend == "parakeet_unified":
        return TRANSCRIBE_RUNTIME
    if backend == "qwen_asr":
        return TRANSCRIBE_RUNTIME if platform_tag in ("win_amd64", "linux_x86_64") else "asr-qwen"
    if backend == "moonshine":
        return "asr-moonshine"
    return "asr-nvidia-cpu"


def model_supported_here(key: str, platform_tag: str | None = None) -> bool:
    """Whether ``key`` can run on ``platform_tag`` (default: this host)."""
    model = MODELS.get(key)
    if model is None:
        return True
    from services.components import current_platform_tag

    tag = platform_tag or current_platform_tag()
    if model.platforms and tag not in model.platforms:
        return False
    return backend_supported_here(model.backend, tag)


def supported_families(platform_tag: str | None = None) -> tuple[str, ...]:
    """Model families that have at least one runnable backend on this computer."""
    from config import parakeet_mlx_supported

    order = (
        ("parakeet", WHISPER_FAMILY, "qwen", "nemotron", "moonshine")
        if parakeet_mlx_supported()
        else ("parakeet", WHISPER_FAMILY, "qwen", "nemotron", "moonshine")
    )
    return tuple(
        family for family in order
        if any(backend_supported_here(b, platform_tag) for b in backends_in_family(family))
    )


def model_runtime_label(model_name: str, backend: str = "") -> str:
    """Short human-readable name of the runtime executing ``model_name`` here."""
    owner = backend or backend_of(model_name)
    if owner == WHISPER_BACKEND:
        return "faster-whisper"
    if owner == "parakeet_unified":
        return "transcribe.cpp"
    if owner == "qwen_asr":
        return "transcribe.cpp" if uses_transcribe_cpp("qwen_asr") else "PyTorch"
    if owner in ("parakeet", "nemotron"):
        return "NeMo-Speech"
    if owner == "parakeet_mlx":
        return "MLX"
    if owner == "moonshine":
        return "Moonshine Voice"
    return owner


def model_supports(model_name: str | None, backend_id: str, capability: str) -> bool:
    """Read a boolean capability from ``SpeechModel``, falling back to ``backend_id``'s default."""
    model = MODELS.get(model_name) if model_name else None
    if model is None and backend_id in DEFAULT_MODELS:
        model = BUILTIN_MODELS.get(DEFAULT_MODELS[backend_id])
    return bool(getattr(model, capability, False)) if model is not None else False


@functools.cache
def _artifacts(filename: str) -> dict:
    # Shipped with the app, so it is read once; callers only read the specs.
    with Path(__file__).with_name(filename).open(encoding="utf-8-sig") as stream:
        return json.load(stream)


def artifacts(key: str) -> dict:
    """A built-in model's pinned download: repository, revision and checked files."""
    if BUILTIN_MODELS[key].backend == "qwen_asr":
        gguf_specs = _artifacts("qwen_gguf_models.json")
        if uses_transcribe_cpp("qwen_asr") and key in gguf_specs:
            return gguf_specs[key]
        models_specs = _artifacts("models.json")
        if key in models_specs:
            return models_specs[key]
        if key in gguf_specs:
            return gguf_specs[key]
    return _artifacts("models.json")[key]


def selected_model(backend: str, settings: dict) -> str:
    from services.local_asr.cache import is_bundled
    from services.settings import SettingsKey

    key = SettingsKey.LOCAL_ASR_MODELS.read(settings).get(backend)
    if key in MODELS and MODELS[key].backend == backend:
        return key
    # An all-in-one install bundles a ready-to-use model inside the app tree;
    # prefer it when the user has not chosen a specific model yet.
    for model in BUILTIN_MODELS.values():
        if model.backend == backend and is_bundled(model.key):
            return model.key
    return DEFAULT_MODELS[backend]


def selected_device(backend: str, settings: dict) -> str:
    from services.settings import SettingsKey

    device = SettingsKey.LOCAL_ASR_DEVICES.read(settings).get(backend)
    if backend == "parakeet_mlx":
        return device if device in ("auto", "cpu") else "auto"
    if backend == "moonshine":
        return "cpu"
    return device if device in ("auto", "cpu", "cuda") else "auto"


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


def uses_transcribe_cpp(backend: str) -> bool:
    if backend == "parakeet_unified":
        return True
    if backend != "qwen_asr":
        return False

    from services.components import current_platform_tag

    return current_platform_tag() in ("win_amd64", "linux_x86_64")


def runtime_id(backend: str, device: str) -> str:
    if backend == "parakeet_mlx":
        return MLX_RUNTIME
    if uses_transcribe_cpp(backend):
        return TRANSCRIBE_RUNTIME
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
    if uses_transcribe_cpp(backend):
        # The worker asks transcribe.cpp for a GPU; Auto falls back to the
        # CPU there when none is listed.
        return TRANSCRIBE_RUNTIME, requested
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
    for key, filename in (
        ("asr-qwen", "qwen_runtime.json"),
        ("asr-moonshine", "moonshine_runtime.json"),
        ("asr-nvidia-cpu", "nvidia_cpu_runtime.json"),
        ("asr-nvidia-cuda", "nvidia_cuda_runtime.json"),
    ):
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
        (TRANSCRIBE_RUNTIME, "win_amd64", "transcribe_windows_runtime.json"),
        (TRANSCRIBE_RUNTIME, "linux_x86_64", "transcribe_linux_runtime.json"),
    ):
        with Path(__file__).with_name(filename).open(encoding="utf-8") as stream:
            entries.setdefault(key, {"platforms": {}})["platforms"][platform] = json.load(stream)
    return entries
