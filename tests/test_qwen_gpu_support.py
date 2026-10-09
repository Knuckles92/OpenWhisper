"""Qwen3-ASR on a GPU its PyTorch build has no code for (issue #39).

torch 2.6.0+cu124 reports CUDA available on an RTX 50-series card (sm_120),
then fails on the first kernel. The worker checks the build's architectures
first, Auto falls back to the CPU, and an explicit CUDA choice says why.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.local_asr import cache
from services.local_asr.worker import cuda_arch_supported
from tests.test_local_asr_languages import run_worker
from transcriber.optional_backend import LocalSpeechBackend

TORCH_26_CU124 = ["sm_50", "sm_60", "sm_61", "sm_70", "sm_75", "sm_80", "sm_86", "sm_90"]
TORCH_271_CU128 = [*TORCH_26_CU124, "sm_100", "sm_120"]


@pytest.mark.parametrize("capability, arch_list, supported", [
    ((12, 0), TORCH_26_CU124, False),  # RTX 5080 on the old runtime
    ((12, 0), TORCH_271_CU128, True),
    ((6, 1), TORCH_271_CU128, True),  # GTX 1050 Ti
    ((7, 5), TORCH_271_CU128, True),  # RTX 2060
    ((8, 9), ["sm_80", "sm_86"], True),  # same major, newer minor
    ((8, 6), ["sm_89"], False),  # code for a newer minor never runs on an older one
    ((6, 1), ["sm_70", "sm_75"], False),
    ((12, 0), ["sm_80", "compute_90"], True),  # PTX compiles for newer GPUs
    ((8, 6), ["compute_90"], False),
    ((9, 0), ["sm_90a"], False),  # arch-specific code, ignored
])
def test_cuda_arch_supported(capability, arch_list, supported):
    assert cuda_arch_supported(capability, arch_list) is supported


_FAKE_RUNTIME = """
import runpy, sys, types
loaded = []
class Qwen:
    def __init__(self):
        self.model = types.SimpleNamespace(generate=lambda *a, **k: None)
    @classmethod
    def from_pretrained(cls, *a, **k):
        loaded.append(k['device_map'])
        return cls()
sys.modules['qwen_asr'] = types.SimpleNamespace(Qwen3ASRModel=Qwen)
sys.modules['torch'] = types.SimpleNamespace(
    __version__='2.6.0+cu124', float16='f16', float32='f32',
    cuda=types.SimpleNamespace(
        is_available=lambda: True,
        get_device_capability=lambda index: (12, 0),
        get_device_name=lambda index: 'NVIDIA GeForce RTX 5080',
        get_arch_list=lambda: ARCHES))
runpy.run_path('services/local_asr/worker.py', run_name='__main__')
"""


def test_worker_refuses_a_gpu_its_torch_has_no_code_for():
    bootstrap = _FAKE_RUNTIME.replace("ARCHES", json.dumps(TORCH_26_CU124))
    responses = run_worker(bootstrap, [
        dict(id=1, op='load', backend='qwen_asr', device='cuda', model_path='fixture'),
        dict(id=2, op='load', backend='qwen_asr', device='cpu', model_path='fixture'),
        dict(id=3, op='shutdown')])

    assert responses[1]['code'] == 'unsupported_gpu'
    assert 'RTX 5080' in responses[1]['error']
    assert 'compute capability 12.0' in responses[1]['error']
    assert responses[2]['result'] == {'device': 'cpu'}


def test_worker_loads_on_a_gpu_its_torch_supports():
    bootstrap = _FAKE_RUNTIME.replace("ARCHES", json.dumps(TORCH_271_CU128))
    responses = run_worker(bootstrap, [
        dict(id=1, op='load', backend='qwen_asr', device='cuda', model_path='fixture'),
        dict(id=2, op='shutdown')])

    assert responses[1]['result'] == {'device': 'cuda'}


def _unsupported_gpu():
    error = RuntimeError("Qwen's runtime (PyTorch 2.6.0+cu124) has no code for the RTX 5080")
    error.code = "unsupported_gpu"
    return error


@pytest.fixture
def qwen(monkeypatch, tmp_path):
    """A Qwen backend whose worker refuses CUDA; returns (make_backend, requested devices)."""
    monkeypatch.setattr("services.components.is_installed", lambda _: True)
    monkeypatch.setattr("services.components.component_dir", lambda key: str(tmp_path / key))
    monkeypatch.setattr(cache, "is_cached", lambda key: True)
    monkeypatch.setattr(cache, "load_path", lambda key: str(tmp_path / key))
    monkeypatch.setattr(
        "transcriber.optional_backend.resolve_runtime", lambda backend, requested: (
            "asr-qwen", "cpu" if requested == "cpu" else "cuda")
    )
    devices = []

    class Process:
        def __init__(self, _python):
            self.process = Mock(poll=Mock(return_value=None))

        def request(self, op, **kwargs):
            devices.append(kwargs["device"])
            if kwargs["device"] == "cuda":
                raise _unsupported_gpu()
            return {"device": kwargs["device"]}

        def recent_errors(self):
            return "UserWarning: sm_120 is not compatible with the current PyTorch installation."

        def close(self):
            pass

    monkeypatch.setattr("services.local_asr.process.SpeechProcess", Process)

    def make(requested):
        monkeypatch.setattr(
            LocalSpeechBackend, "_settings",
            staticmethod(lambda: {"local_asr_devices": {"qwen_asr": requested}}),
        )
        return LocalSpeechBackend("qwen_asr")

    return make, devices


def test_auto_falls_back_to_the_cpu_and_says_why(qwen):
    make, devices = qwen
    backend = make("auto")
    backend.reload_model()

    assert devices == ["cuda", "cpu"]
    assert backend.is_available() and backend.device == "cpu"
    assert backend.cpu_fallback_note.startswith("Qwen3-ASR is using the CPU. ")
    assert "RTX 5080" in backend.cpu_fallback_note


def test_explicit_cuda_fails_and_logs_the_worker_output(qwen, caplog):
    make, devices = qwen
    backend = make("cuda")
    with caplog.at_level(logging.ERROR, logger="transcriber.optional_backend"):
        with pytest.raises(RuntimeError, match="RTX 5080"):
            backend.reload_model()

    assert devices == ["cuda"]
    assert not backend.is_available()
    assert "RTX 5080" in backend.last_error
    assert backend.cpu_fallback_note == ""
    assert "sm_120 is not compatible" in caplog.text


def test_a_reload_forgets_an_earlier_fallback(qwen):
    make, devices = qwen
    backend = make("auto")
    backend.reload_model()
    backend._settings = staticmethod(lambda: {"local_asr_devices": {"qwen_asr": "cpu"}})
    backend.reload_model()

    assert devices == ["cuda", "cpu", "cpu"]
    assert backend.cpu_fallback_note == ""


def test_other_load_errors_are_not_retried_on_the_cpu(qwen, monkeypatch):
    make, devices = qwen
    backend = make("auto")
    failing = SimpleNamespace(calls=[])

    class Broken:
        def __init__(self, _python):
            self.process = Mock(poll=Mock(return_value=None))

        def request(self, op, **kwargs):
            failing.calls.append(kwargs["device"])
            raise RuntimeError("model.safetensors is corrupt")

        def close(self):
            pass

    monkeypatch.setattr("services.local_asr.process.SpeechProcess", Broken)
    with pytest.raises(RuntimeError, match="corrupt"):
        backend.reload_model()

    assert failing.calls == ["cuda"]
