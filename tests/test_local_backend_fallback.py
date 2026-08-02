"""Tests for the local backend's CPU fallback when GPU libraries are absent.

CTranslate2 reports a CUDA device from the driver alone and resolves cuBLAS
lazily, on the first encoder pass rather than at construction. A machine with a
driver but no GPU component (Windows) or no ``requirements-gpu.txt`` (Linux)
therefore loads a GPU model successfully and then throws on every transcription.
These tests pin the pre-flight probe that prevents it, plus the construction-time
fallback behind it.
"""

import pytest

from transcriber import local_backend as module

CUBLAS_ERROR = "Library cublas64_12.dll is not found or cannot be loaded"


@pytest.fixture
def stub_backend(monkeypatch):
    """Build backends against fake models, recording every construction."""
    def install(
        model_factory=lambda device: object(),
        detected=("cuda", "float16", "turbo"),
        gpu_libraries=True,
    ):
        built = []
        probes = []

        def fake_whisper_model(name, device=None, compute_type=None, **_kwargs):
            # Record before constructing, so a factory that raises still leaves
            # evidence that this device was attempted.
            attempt = [name, device, compute_type, None]
            built.append(attempt)
            attempt[3] = model_factory(device)
            return attempt[3]

        def fake_probe():
            probes.append(True)
            return gpu_libraries

        monkeypatch.setattr(module, "WhisperModel", fake_whisper_model)
        monkeypatch.setattr(
            "services.components.gpu_runtime_available", fake_probe
        )
        monkeypatch.setattr(
            module.LocalWhisperBackend, "_detect_hardware", lambda self: detected
        )
        monkeypatch.setattr(
            module.LocalWhisperBackend,
            "_get_supported_compute_types",
            lambda self, device: {"int8", "float32", "float16"},
        )
        monkeypatch.setattr("services.hf_access.is_model_cached", lambda name: True)
        return built, probes

    return install


def test_missing_cuda_libraries_select_cpu_up_front(stub_backend):
    """No GPU model is loaded at all when its libraries cannot be loaded.

    Loading one would succeed and then fail on the user's first transcription,
    because CTranslate2 does not touch cuBLAS until the first encoder pass.
    """
    built, probes = stub_backend(gpu_libraries=False)

    backend = module.LocalWhisperBackend(model_name="turbo")

    assert probes, "the CUDA libraries were never probed"
    assert [device for _, device, _, _ in built] == ["cpu"]
    assert backend.is_available()
    assert backend._device == "cpu"
    assert backend._compute_type == "int8"
    assert "cuBLAS" in backend.gpu_fallback_reason


def test_working_cuda_libraries_stay_on_gpu(stub_backend):
    """The probe must not disturb a machine where CUDA actually works."""
    built, _probes = stub_backend(gpu_libraries=True)

    backend = module.LocalWhisperBackend(model_name="turbo")

    assert [device for _, device, _, _ in built] == ["cuda"]
    assert backend._device == "cuda"
    assert backend._compute_type == "float16"
    assert backend.gpu_fallback_reason is None


def test_cpu_selection_skips_the_probe(stub_backend):
    """Nothing to validate when the CPU was already chosen."""
    _built, probes = stub_backend(detected=("cpu", "int8", "base"))

    module.LocalWhisperBackend(model_name="base")

    assert probes == []


def test_construction_failure_falls_back_to_cpu(stub_backend):
    """A GPU that passes the probe but fails to load must still recover.

    Covers driver/hardware faults the library probe cannot see.
    """
    def factory(device):
        if device == "cuda":
            raise RuntimeError(CUBLAS_ERROR)
        return object()

    built, _probes = stub_backend(factory)

    backend = module.LocalWhisperBackend(model_name="turbo")

    assert [device for _, device, _, _ in built] == ["cuda", "cpu"]
    assert backend.is_available()
    assert backend._device == "cpu"
    assert "cublas" in backend.gpu_fallback_reason


def test_cpu_fallback_keeps_the_selected_model(stub_backend):
    """Downgrading turbo to base would change output and may need a download."""
    built, _probes = stub_backend(gpu_libraries=False)

    backend = module.LocalWhisperBackend(model_name="turbo")

    assert backend.model_name == "turbo"
    assert all(name == "turbo" for name, _, _, _ in built)


def test_cpu_fallback_is_visible_in_device_info(stub_backend):
    """Without this the UI looks identical to a machine that has no GPU."""
    stub_backend(gpu_libraries=False)

    backend = module.LocalWhisperBackend(model_name="turbo")

    assert "GPU unavailable" in backend.device_info
    assert "cpu" in backend.device_info


def test_non_gpu_errors_do_not_switch_devices(stub_backend):
    """A corrupt model fails the same way on CPU; do not blame the GPU."""
    def factory(device):
        raise RuntimeError("invalid model file")

    built, _probes = stub_backend(factory)

    backend = module.LocalWhisperBackend(model_name="turbo")

    assert backend.is_available() is False
    assert backend.gpu_fallback_reason is None
    assert len(built) == 1


def test_cpu_load_failure_still_reports_unavailable(stub_backend):
    """The fallback must not mask a genuine failure as a working backend."""
    def factory(device):
        raise RuntimeError("no backend at all")

    stub_backend(factory, detected=("cpu", "int8", "base"))

    backend = module.LocalWhisperBackend(model_name="base")

    assert backend.is_available() is False
    assert backend.gpu_fallback_reason is None
