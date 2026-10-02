"""What this computer's NVIDIA GPU is, and how Local Whisper will run on it.

Local Whisper used to announce float16 and turbo for every CUDA device, then
fall back to whatever the card really supports, and the Downloads page quoted
OpenAI's float16 reference table (turbo 6 GB, medium 5 GB). On a GTX 1050 Ti
that read as "turbo doesn't fit, pick medium", when the card runs turbo at
int8_float32 in 1.4 GB, and medium, with six times turbo's decoder layers,
needs more memory than turbo does.

This module answers the questions in the order the user should hear them:
which card (NVML, shipped with the NVIDIA driver on Windows and Linux),
which compute types it supports (CTranslate2's own rule, from the compute
capability), and about how much GPU memory each model needs at that compute
type. ``plan_cuda`` turns those into the model and compute type the "auto"
settings choose. Nothing here imports ctranslate2, so the Settings window can
ask without paying for loading it.
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import sys
import threading
from dataclasses import dataclass
from typing import Iterable, Optional, Tuple

logger = logging.getLogger(__name__)

#: Best first. float16 is the quality/speed default; int8_float16 halves the
#: weights on the same cards; int8_float32 is what Pascal cards run (they
#: have int8 but no float16 math); float32 doubles every weight and is only
#: chosen when nothing else is supported.
CUDA_COMPUTE_PREFERENCE: Tuple[str, ...] = (
    "float16", "int8_float16", "int8_float32", "int8", "float32",
)

#: The model "auto" picks on a GPU, then smaller ones for cards that can't
#: hold it. Turbo first: it is both the most accurate of these and, with four
#: decoder layers, lighter at runtime than medium.
AUTO_GPU_MODELS: Tuple[str, ...] = ("turbo", "small", "base", "tiny")

#: Share of the card the estimate may use; the rest covers the desktop,
#: other programs, and the estimate being an estimate.
VRAM_BUDGET_FRACTION = 0.85


@dataclass(frozen=True)
class NvidiaGpu:
    """The first NVIDIA GPU, which is the one CTranslate2 uses by default."""

    name: str
    total_mib: int
    #: (major, minor), e.g. (6, 1) for Pascal's GTX 10 series.
    compute_capability: Optional[Tuple[int, int]] = None

    @property
    def total_gb(self) -> float:
        return self.total_mib / 1024

    @property
    def short_name(self) -> str:
        """``GTX 1050 Ti`` rather than ``NVIDIA GeForce GTX 1050 Ti``."""
        name = self.name
        for prefix in ("NVIDIA ", "GeForce ", "Quadro "):
            if name.startswith(prefix):
                name = name[len(prefix):]
        return name or self.name


# ---- NVML ----

class _NvmlMemory(ctypes.Structure):
    _fields_ = [
        ("total", ctypes.c_ulonglong),
        ("free", ctypes.c_ulonglong),
        ("used", ctypes.c_ulonglong),
    ]


def _nvml_candidates() -> Tuple[str, ...]:
    if sys.platform == "win32":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        return (
            "nvml.dll",  # System32 on drivers since 2019
            os.path.join(program_files, "NVIDIA Corporation", "NVSMI", "nvml.dll"),
        )
    if sys.platform.startswith("linux"):
        return ("libnvidia-ml.so.1", "libnvidia-ml.so")
    return ()


def _load_nvml():
    for name in _nvml_candidates():
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    return None


def _query_nvml() -> Optional[NvidiaGpu]:
    nvml = _load_nvml()
    if nvml is None:
        return None
    init = getattr(nvml, "nvmlInit_v2", None) or nvml.nvmlInit
    if init() != 0:
        return None
    try:
        count = ctypes.c_uint(0)
        get_count = getattr(nvml, "nvmlDeviceGetCount_v2", None) or nvml.nvmlDeviceGetCount
        if get_count(ctypes.byref(count)) != 0 or count.value == 0:
            return None
        handle = ctypes.c_void_p()
        get_handle = (getattr(nvml, "nvmlDeviceGetHandleByIndex_v2", None)
                      or nvml.nvmlDeviceGetHandleByIndex)
        if get_handle(0, ctypes.byref(handle)) != 0:
            return None
        name = ctypes.create_string_buffer(96)
        if nvml.nvmlDeviceGetName(handle, name, ctypes.c_uint(len(name))) != 0:
            return None
        memory = _NvmlMemory()
        if nvml.nvmlDeviceGetMemoryInfo(handle, ctypes.byref(memory)) != 0:
            return None
        capability = None
        major, minor = ctypes.c_int(0), ctypes.c_int(0)
        get_capability = getattr(nvml, "nvmlDeviceGetCudaComputeCapability", None)
        if get_capability is not None and get_capability(
            handle, ctypes.byref(major), ctypes.byref(minor)
        ) == 0:
            capability = (major.value, minor.value)
        return NvidiaGpu(
            name=name.value.decode("utf-8", "replace").strip(),
            total_mib=int(memory.total // (1024 * 1024)),
            compute_capability=capability,
        )
    finally:
        try:
            nvml.nvmlShutdown()
        except Exception:
            pass


_gpu_lock = threading.Lock()
_gpu_cache: list = []


def nvidia_gpu() -> Optional[NvidiaGpu]:
    """The first NVIDIA GPU, or None without one (or without its driver).

    Asked once per run (a few milliseconds); the hardware doesn't change
    while the app is open. Never raises.
    """
    with _gpu_lock:
        if not _gpu_cache:
            try:
                _gpu_cache.append(_query_nvml())
            except Exception:
                logger.debug("NVML query failed", exc_info=True)
                _gpu_cache.append(None)
        return _gpu_cache[0]


def ctranslate2_cuda_types(compute_capability: Optional[Tuple[int, int]]) -> frozenset:
    """The CUDA compute types CTranslate2 accepts on a card of this capability.

    Mirrors ``get_supported_compute_types("cuda")`` in CTranslate2 4.x
    (src/cuda/utils.cc): int8 needs DP4A (compute 6.1, or 7.0 and newer),
    float16 needs tensor cores (7.0+), bfloat16 needs 8.0+. The backend still
    asks CTranslate2 itself before loading; this is for describing a card
    without importing it.
    """
    types = {"float32"}
    if compute_capability is None:
        return frozenset(types)
    major, minor = compute_capability
    if major > 6 or (major, minor) == (6, 1):
        types |= {"int8", "int8_float32"}
    if major >= 7:
        types |= {"float16", "int8_float16"}
    if major >= 8:
        types |= {"bfloat16", "int8_bfloat16"}
    return frozenset(types)


def best_cuda_compute_type(supported: Iterable[str]) -> str:
    supported = set(supported)
    return next((c for c in CUDA_COMPUTE_PREFERENCE if c in supported), "float32")


# ---- memory ----

#: (encoder layers, decoder layers, d_model, parameters). Decoder depth is
#: what separates turbo (4) from medium (24) and large (32) at runtime.
_ARCHITECTURES = {
    "tiny": (4, 4, 384, 39e6),
    "base": (6, 6, 512, 74e6),
    "small": (12, 12, 768, 244e6),
    "medium": (24, 24, 1024, 769e6),
    "large": (32, 32, 1280, 1550e6),
    "turbo": (32, 4, 1280, 809e6),
    "distil-small.en": (12, 4, 768, 166e6),
    "distil-medium.en": (24, 2, 1024, 394e6),
    "distil-large-v2": (32, 2, 1280, 756e6),
    "distil-large-v3": (32, 2, 1280, 756e6),
}

_WEIGHT_BYTES = {
    "float32": 4, "float16": 2, "bfloat16": 2,
    "int8": 1, "int8_float32": 1, "int8_float16": 1, "int8_bfloat16": 1,
}

_ENCODER_FRAMES = 1500   # one 30 s window
_MAX_TOKENS = 448        # Whisper's decoder context


def _overhead_mib() -> int:
    """CUDA context, cuBLAS workspace and allocator slack.

    What a measured peak leaves after the weights and decoder caches, for
    turbo at beam 5 on one 30 s window (2026-09-26). Linux, GTX 1050 Ti:
    int8_float32 peaked at 1377 MiB, and float32 ran the 4 GB card out of
    memory. Windows keeps a larger CUDA context under WDDM; RTX 2060:
    float16 2365 MiB, int8_float16 1520, int8_float32 1567.
    """
    return 450 if sys.platform == "win32" else 250


def _architecture(model: str):
    # Repository owners and parent directories do not describe the weights.
    name = model.lower().replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if name in _ARCHITECTURES:
        return _ARCHITECTURES[name]
    tokens = set(re.split(r"[-_.]", name))
    if "distil" in tokens:
        match = re.search(r"(?:^|[-_])distil(?:[-_]whisper)?[-_]"
                          r"(small\.en|medium\.en|large[-_]v[23])(?=$|[-_])", name)
        return _ARCHITECTURES.get("distil-" + match[1].replace("_", "-")) if match else None
    sizes = tokens.intersection({"tiny", "base", "small", "medium", "large"})
    if "turbo" in tokens:
        return _ARCHITECTURES["turbo"] if sizes <= {"large"} else None
    if len(sizes) == 1:
        return _ARCHITECTURES[sizes.pop()]
    return None


def estimate_vram_mib(model: str, compute_type: str, beam_size: Optional[int] = None) -> Optional[int]:
    """Peak GPU memory for faster-whisper decoding one window, in MiB.

    Weights at the compute type's width, plus the decoder's per-beam caches:
    cross-attention over the 1500 encoder frames and self-attention over up
    to 448 tokens, both in the activation precision. None for a model this
    doesn't know.
    """
    arch = _architecture(model)
    if arch is None:
        return None
    if beam_size is None:
        from config import config
        beam_size = max(1, int(config.FASTER_WHISPER_BEAM_SIZE))
    _encoder_layers, decoder_layers, width, parameters = arch
    weight_bytes = _WEIGHT_BYTES.get(compute_type, 4)
    activation_bytes = 2 if compute_type.endswith(("float16", "bfloat16")) else 4
    caches = decoder_layers * 2 * (_ENCODER_FRAMES + _MAX_TOKENS) * width * activation_bytes * beam_size
    total = parameters * weight_bytes + caches
    return int(round(total / (1024 * 1024))) + _overhead_mib()


def format_gb(mib: int) -> str:
    return f"{mib / 1024:.1f} GB"


def fits(model: str, compute_type: str, total_mib: Optional[int]) -> bool:
    """Whether the estimate leaves the card its headroom; True when unknown."""
    if not total_mib:
        return True
    estimate = estimate_vram_mib(model, compute_type)
    return estimate is None or estimate <= total_mib * VRAM_BUDGET_FRACTION


def plan_cuda(model: str, compute_type: str, supported: Iterable[str],
              total_mib: Optional[int]) -> Tuple[str, str]:
    """Resolve an "auto" model and/or compute type for a CUDA device.

    A compute type the user chose is kept (the backend's fallback handles
    one the card lacks). "auto" takes the best supported type whose estimate
    fits the card, so a large model on a small card lands on an int8 type
    rather than running out of memory. An "auto" model is the first of
    ``AUTO_GPU_MODELS`` that fits at the best type that fits it.
    """
    supported = set(supported) or {"float32"}
    computes = ([compute_type] if compute_type != "auto"
                else [c for c in CUDA_COMPUTE_PREFERENCE if c in supported] or ["float32"])
    models = [model] if model != "auto" else list(AUTO_GPU_MODELS)
    for candidate in models:
        for compute in computes:
            if fits(candidate, compute, total_mib):
                return candidate, compute
    # Nothing fits: the lightest of the choices, and let a failed load say so.
    candidate = models[-1]
    compute = min(computes, key=lambda c: estimate_vram_mib(candidate, c) or 0)
    return candidate, compute


def describe_plan(model: str, compute_type: str, gpu: Optional[NvidiaGpu],
                  supported: Iterable[str]) -> str:
    """One log line that says what will happen, before it happens."""
    card = (f"{gpu.name} ({format_gb(gpu.total_mib)})" if gpu is not None
            else "a CUDA device")
    line = f"Using {card} for Local Whisper: {model} at {compute_type}"
    estimate = estimate_vram_mib(model, compute_type)
    if estimate:
        line += f", about {format_gb(estimate)} of GPU memory"
    if "float16" not in set(supported):
        line += " (this GPU has no float16)"
    return line


def memory_guidance(model: str, gpu: Optional[NvidiaGpu] = None) -> Optional[str]:
    """What the Downloads inspector says about a Whisper model's GPU memory.

    With an NVIDIA GPU, the compute type this card will actually use and the
    estimate at it; without one, the float16 and int8 estimates. None for a
    model this doesn't know.
    """
    if _architecture(model) is None:
        return None
    if gpu is not None and gpu.compute_capability is not None:
        supported = ctranslate2_cuda_types(gpu.compute_capability)
        _model, compute = plan_cuda(model, "auto", supported, gpu.total_mib)
        estimate = estimate_vram_mib(model, compute)
        verdict = ("fits" if fits(model, compute, gpu.total_mib)
                   else "is more than it can spare")
        return (
            f"About {format_gb(estimate)} on this {gpu.short_name} "
            f"({format_gb(gpu.total_mib)}) at {compute}, the precision it runs; "
            f"that {verdict}. Estimated for beam size 5."
        )
    half = estimate_vram_mib(model, "float16")
    quarter = estimate_vram_mib(model, "int8_float32")
    return (
        f"About {format_gb(half)} of GPU memory at float16, or {format_gb(quarter)} "
        "at int8 on cards without float16 (estimates for beam size 5). "
        "On the CPU it uses system memory instead."
    )
