"""One action from "NVIDIA GPU, no CUDA libraries" to Local Whisper on the GPU.

Getting there used to take three steps nothing connected: install GPU
Acceleration from Downloads, move the device setting back from the "cpu" a
fallback had saved, and consent to the model download. On a fresh install the
last one came first and stopped at "waiting for download consent" without a
word about the GPU being unusable too. ``plan_gpu_setup`` works out all three
for one offer ("Use this GPU"); the application controller carries them out.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from services.gpu_info import (
    NvidiaGpu, best_cuda_compute_type, estimate_vram_mib, fits, nvidia_gpu, plan_cuda,
)


@dataclass(frozen=True)
class GpuSetupPlan:
    """What "Use this GPU" will do on this computer."""

    gpu: Optional[NvidiaGpu]
    #: The Whisper model that will load on the GPU.
    model: str
    #: What the model setting becomes: "auto" when switching, else unchanged.
    model_setting: str
    #: The model setting before, e.g. "base" or "auto".
    previous_model: str
    compute_type: str
    supports_float16: bool
    estimate_mib: Optional[int]
    install_component: bool
    component_download_bytes: int
    component_install_bytes: int
    #: 0 when the model is already downloaded.
    model_download_bytes: int

    @property
    def switches_model(self) -> bool:
        return self.model_setting != self.previous_model


def plan_gpu_setup(settings: dict, supported: Iterable[str]) -> Optional[GpuSetupPlan]:
    """Plan GPU Local Whisper here; None where GPU Acceleration isn't offered.

    ``supported`` is what CTranslate2 reports for "cuda". A model the user
    chose is kept when it fits the card at the best compute type it
    supports; an "auto" model, or one too large for the card, becomes
    "auto", which resolves to the model ``plan_cuda`` picks for this card.
    """
    from config import config
    from services.components import (
        ComponentId, available_component_ids, catalog_entry_for_platform, gpu_runtime_available,
    )
    from services.hf_access import MODEL_DOWNLOAD_SIZE_MB, is_model_cached
    from services.settings import SettingsKey

    if ComponentId.GPU_ACCEL not in available_component_ids():
        return None
    supported = set(supported) or {"float32"}
    gpu = nvidia_gpu()
    total = gpu.total_mib if gpu is not None else None
    selected = settings.get(SettingsKey.WHISPER_MODEL, config.DEFAULT_WHISPER_MODEL) or "auto"
    compute_setting = settings.get(
        SettingsKey.WHISPER_COMPUTE_TYPE, config.FASTER_WHISPER_COMPUTE_TYPE
    ) or "auto"

    keep = selected != "auto" and fits(selected, best_cuda_compute_type(supported), total)
    model_setting = selected if keep else "auto"
    model, compute = plan_cuda(model_setting, compute_setting, supported, total)

    install = not gpu_runtime_available()
    entry = catalog_entry_for_platform(ComponentId.GPU_ACCEL) or {}
    download_bytes = sum(int(a.get("size_bytes", 0)) for a in entry.get("archives", []))
    return GpuSetupPlan(
        gpu=gpu,
        model=model,
        model_setting=model_setting,
        previous_model=selected,
        compute_type=compute,
        supports_float16="float16" in supported,
        estimate_mib=estimate_vram_mib(model, compute),
        install_component=install,
        component_download_bytes=download_bytes if install else 0,
        component_install_bytes=int(entry.get("install_bytes", 0)) if install else 0,
        model_download_bytes=(
            0 if is_model_cached(model) else MODEL_DOWNLOAD_SIZE_MB.get(model, 0) * 1_000_000
        ),
    )
