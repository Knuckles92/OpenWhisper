"""What an NVIDIA GPU runs Local Whisper at, and how much memory that takes.

Pinned to measurements (2026-09-26, turbo, beam 5, one 30 s window): a
GTX 1050 Ti on Linux peaked at 1377 MiB at int8_float32 and ran out of its
4 GB at float32; an RTX 2060 on Windows peaked at 2365 MiB at float16.
"""
import re

import pytest

from services import gpu_info
from services.gpu_info import NvidiaGpu

PASCAL = gpu_info.ctranslate2_cuda_types((6, 1))
TURING = gpu_info.ctranslate2_cuda_types((7, 5))
GTX_1050_TI = NvidiaGpu("NVIDIA GeForce GTX 1050 Ti", 4096, (6, 1))


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(gpu_info.sys, "platform", "linux")


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(gpu_info.sys, "platform", "win32")


def test_compute_types_follow_ctranslate2s_capability_rule():
    # What CTranslate2 reported on the GTX 1050 Ti.
    assert PASCAL == {"float32", "int8", "int8_float32"}
    assert {"float16", "int8_float16", "int8_float32"} <= TURING
    assert "bfloat16" not in TURING
    assert "bfloat16" in gpu_info.ctranslate2_cuda_types((8, 6))
    # Maxwell has no DP4A: float32 only.
    assert gpu_info.ctranslate2_cuda_types((5, 2)) == {"float32"}


def test_turbo_estimate_matches_the_linux_measurement(linux):
    assert gpu_info.estimate_vram_mib("turbo", "int8_float32") == pytest.approx(1377, rel=0.05)
    # float32 is beyond what a 4 GB card can spare, as observed.
    assert not gpu_info.fits("turbo", "float32", 4096)


def test_turbo_estimates_track_the_windows_measurements(windows):
    measured = {"float16": 2365, "int8_float16": 1520, "int8_float32": 1567}
    for compute, peak in measured.items():
        assert gpu_info.estimate_vram_mib("turbo", compute) == pytest.approx(peak, rel=0.10), compute


def test_medium_needs_more_memory_than_turbo():
    """The upstream table said the opposite (medium 5 GB, turbo 6 GB)."""
    for compute in ("float16", "int8_float32"):
        assert gpu_info.estimate_vram_mib("medium", compute) > gpu_info.estimate_vram_mib("turbo", compute)


def test_every_whisper_model_has_an_estimate():
    from config import config

    for model in config.WHISPER_MODEL_CHOICES:
        if model != "auto":
            assert gpu_info.estimate_vram_mib(model, "float16"), model
            assert gpu_info.memory_guidance(model), model


def test_auto_on_a_4gb_pascal_card_is_turbo_at_int8_float32(linux):
    assert gpu_info.plan_cuda("auto", "auto", PASCAL, 4096) == ("turbo", "int8_float32")


def test_auto_prefers_float16_where_it_fits():
    assert gpu_info.plan_cuda("auto", "auto", TURING, 6144) == ("turbo", "float16")


def test_a_chosen_model_too_big_for_float16_lands_on_int8_float16():
    assert gpu_info.plan_cuda("large-v3", "auto", TURING, 4096) == ("large-v3", "int8_float16")


def test_a_chosen_compute_type_is_kept():
    assert gpu_info.plan_cuda("auto", "float32", PASCAL, 8192) == ("turbo", "float32")


def test_auto_steps_down_on_a_small_card(linux):
    model, compute = gpu_info.plan_cuda("auto", "auto", PASCAL, 1024)
    assert model in ("small", "base", "tiny") and compute == "int8_float32"
    assert gpu_info.fits(model, compute, 1024)


def test_unknown_memory_keeps_turbo():
    assert gpu_info.plan_cuda("auto", "auto", PASCAL, None) == ("turbo", "int8_float32")


def test_plan_line_says_the_compute_type_before_loading(linux):
    line = gpu_info.describe_plan("turbo", "int8_float32", GTX_1050_TI, PASCAL)
    assert "turbo at int8_float32" in line
    assert "1.4 GB" in line
    assert "no float16" in line
    assert "float16 and turbo" not in line


def test_memory_guidance_names_this_gpu_and_its_compute_type(linux):
    text = gpu_info.memory_guidance("turbo", GTX_1050_TI)
    assert "GTX 1050 Ti" in text and "int8_float32" in text and "1.4 GB" in text
    assert not re.search(r"(?<![\d.])6 GB", text)


def test_memory_guidance_without_a_gpu_gives_both_precisions():
    text = gpu_info.memory_guidance("turbo")
    assert "float16" in text and "int8" in text


def test_no_nvml_means_no_gpu(monkeypatch):
    monkeypatch.setattr(gpu_info, "_load_nvml", lambda: None)
    monkeypatch.setattr(gpu_info, "_gpu_cache", [])
    assert gpu_info.nvidia_gpu() is None


def test_catalog_no_longer_quotes_the_upstream_table():
    from services.model_catalog import get_model_details

    guidance = get_model_details("turbo").memory_guidance
    assert "upstream reference table" not in guidance
    assert not re.search(r"(?<![\d.])6 GB", guidance)
