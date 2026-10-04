"""MLX integration checks that run without Apple hardware or model downloads."""

import array
import shutil
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest

from services.local_asr.catalog import (
    artifacts,
    resolve_runtime,
    runtime_catalog,
    selected_device,
)
from transcriber.optional_backend import LocalSpeechBackend


def test_mlx_downloads_and_runtime_are_separate_from_gguf():
    mlx = artifacts("parakeet-v3-mlx")
    assert mlx["repo"] == "mlx-community/parakeet-tdt-0.6b-v3"
    assert {f["name"] for f in mlx["files"]} == {"config.json", "model.safetensors"}
    assert all(mlx["revision"] in f["url"] for f in mlx["files"])
    assert artifacts("parakeet-v3")["repo"] == "nvidia/parakeet-tdt-0.6b-v3"
    runtime = runtime_catalog()["asr-parakeet-mlx"]
    assert set(runtime["platforms"]) == {"darwin_arm64"}
    assert any(
        f["name"].startswith("mlx_metal-")
        for f in runtime["platforms"]["darwin_arm64"]["archives"]
    )


def test_mlx_auto_uses_metal_without_probing_cuda(monkeypatch):
    monkeypatch.setattr(
        "ctranslate2.get_cuda_device_count",
        Mock(side_effect=AssertionError("must not probe CUDA")),
    )
    assert resolve_runtime("parakeet_mlx", "auto") == ("asr-parakeet-mlx", "metal")
    assert resolve_runtime("parakeet_mlx", "cpu") == ("asr-parakeet-mlx", "cpu")
    assert (
        selected_device("parakeet_mlx", {"local_asr_devices": {"parakeet_mlx": "cuda"}})
        == "auto"
    )
    with pytest.raises(ValueError, match="not CUDA"):
        resolve_runtime("parakeet_mlx", "cuda")


@pytest.mark.parametrize("tag", ["win_amd64", "linux_x86_64", None])
def test_mlx_unavailable_runtime_has_no_download_prompt(monkeypatch, tag):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr("services.components.current_platform_tag", lambda: tag)
    monkeypatch.setattr("services.components.is_installed", lambda _: False)
    backend = LocalSpeechBackend("parakeet_mlx")
    backend.reload_model()
    assert backend.runtime_component is None
    assert "not available on this platform" in backend.last_error
    assert "Downloads" not in backend.last_error


def test_mlx_backend_loads_local_weights_in_its_worker(monkeypatch, tmp_path):
    from services.local_asr import cache

    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr("services.components.is_installed", lambda _: True)
    monkeypatch.setattr(
        "services.components.component_dir", lambda _: str(tmp_path / "runtime")
    )
    monkeypatch.setattr(cache, "is_cached", lambda _: True)
    monkeypatch.setattr(cache, "load_path", lambda _: str(tmp_path / "weights"))
    process = Mock(process=SimpleNamespace(poll=lambda: None))
    process.request.return_value = {"device": "metal"}
    factory = Mock(return_value=process)
    monkeypatch.setattr("services.local_asr.process.SpeechProcess", factory)
    backend = LocalSpeechBackend("parakeet_mlx")
    backend.reload_model()
    request = process.request.call_args.kwargs
    assert request["backend"] == "parakeet_mlx"
    assert request["device"] == "metal"
    assert request["model_path"] == str(tmp_path / "weights")
    assert backend.is_available() and backend.device == "metal"
    backend.cancel_transcription()
    process.close.assert_called_once()


@pytest.mark.skipif(
    sys.platform == "darwin", reason="Checks rejection on a non-Mac host"
)
def test_real_worker_routes_mlx_requests_to_the_platform_guard(tmp_path):
    from services.local_asr.process import SpeechProcess

    process = SpeechProcess(sys.executable)
    try:
        with pytest.raises(RuntimeError, match="requires an Apple Silicon Mac"):
            process.request(
                "load",
                backend="parakeet_mlx",
                model="parakeet-v3-mlx",
                runtime=str(tmp_path),
                model_path=str(tmp_path),
                device="metal",
                timeout=10,
            )
    finally:
        process.close()


@pytest.fixture
def fake_mlx(monkeypatch):
    from services.local_asr import mlx

    monkeypatch.setattr(mlx.sys, "platform", "darwin")
    monkeypatch.setattr(mlx.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(sys, "path", list(sys.path))
    result = SimpleNamespace(
        text="Hello world.",
        sentences=[SimpleNamespace(text="Hello world.", start=0.1, end=0.8)],
    )
    model = Mock(preprocessor_config=SimpleNamespace(sample_rate=16000, hop_length=160, n_fft=512))
    model.generate.return_value = [result]
    core = SimpleNamespace(
        cpu="cpu",
        gpu="gpu",
        bfloat16="bfloat16",
        float32="float32",
        metal=SimpleNamespace(is_available=lambda: True),
        set_default_device=Mock(),
        array=Mock(
            side_effect=lambda samples, **_: np.asarray(samples, dtype=np.float32)
        ),
        pad=np.pad,
        clear_cache=Mock(),
    )
    loader = Mock(return_value=model)
    mel = Mock()
    mel.astype.side_effect = lambda dtype: f"mel-{dtype}"
    logmel = Mock(return_value=mel)
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=core))
    monkeypatch.setitem(sys.modules, "mlx.core", core)
    monkeypatch.setitem(
        sys.modules, "parakeet_mlx", SimpleNamespace(from_pretrained=loader)
    )
    monkeypatch.setitem(
        sys.modules, "parakeet_mlx.audio", SimpleNamespace(get_logmel=logmel)
    )
    return SimpleNamespace(core=core, model=model, loader=loader, logmel=logmel)


@pytest.mark.parametrize("device,dtype", [("metal", "bfloat16"), ("cpu", "float32")])
def test_mlx_decodes_pcm_and_preserves_meeting_timestamps(
    fake_mlx, tmp_path, device, dtype
):
    from services.local_asr.mlx import MlxRecognizer

    engine = MlxRecognizer(str(tmp_path), str(tmp_path / "weights"), device)
    fake_mlx.loader.assert_called_once_with(
        str((tmp_path / "weights").resolve()), dtype=dtype
    )
    fake_mlx.core.set_default_device.assert_called_once_with(
        "gpu" if device == "metal" else "cpu"
    )
    result = engine.transcribe(array.array("f", [0.01] * 16000), "auto")
    assert result == {
        "text": "Hello world.",
        "segments": [{"text": "Hello world.", "start": 0.1, "end": 0.8}],
    }
    assert len(fake_mlx.logmel.call_args.args[0]) == 16000
    assert fake_mlx.core.array.call_args.kwargs == {"dtype": "float32"}
    np.testing.assert_allclose(fake_mlx.core.array.call_args.args[0], [0.01] * 16000)
    fake_mlx.logmel.return_value.astype.assert_called_once_with(dtype)
    fake_mlx.model.generate.assert_called_once_with(f"mel-{dtype}")
    engine.close()
    fake_mlx.core.clear_cache.assert_called_once()


def test_mlx_handles_empty_and_very_short_audio(fake_mlx, tmp_path):
    from services.local_asr.mlx import MlxRecognizer

    engine = MlxRecognizer(str(tmp_path), str(tmp_path), "metal")
    assert engine.transcribe(array.array("f")) == {"text": "", "segments": []}
    fake_mlx.model.generate.assert_not_called()
    result = engine.transcribe(array.array("f", [0.01] * 80))
    assert len(fake_mlx.logmel.call_args.args[0]) == 512
    assert result["segments"][0]["end"] == 80 / 16000


def test_mlx_missing_metal_does_not_silently_switch_devices(fake_mlx, tmp_path):
    from services.local_asr.mlx import MlxRecognizer

    fake_mlx.core.metal.is_available = lambda: False
    with pytest.raises(RuntimeError, match="Select CPU"):
        MlxRecognizer(str(tmp_path), str(tmp_path), "metal")
    fake_mlx.loader.assert_not_called()


def test_mlx_installer_keeps_native_libraries_and_license_files(monkeypatch, tmp_path):
    from services import components

    monkeypatch.setattr(components, "current_platform_tag", lambda: "darwin_arm64")
    monkeypatch.setattr(
        components, "components_root", lambda: str(tmp_path / "components")
    )
    wheel = tmp_path / "runtime.whl"
    files = (
        "parakeet_mlx/__init__.py",
        "mlx/nn/__init__.py",
        "mlx/core.cpython-312-darwin.so",
        "mlx/lib/libmlx.dylib",
        "mlx/lib/mlx.metallib",
        "librosa/__init__.py",
        "dacite/__init__.py",
        "parakeet_mlx-0.5.3.dist-info/licenses/LICENSE",
        "librosa/core/convert.pyi",
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in files:
            archive.writestr(name, b"payload")
    monkeypatch.setattr(
        components,
        "_download_verified",
        lambda url, sha, size, target, *args, **kwargs: shutil.copyfile(wheel, target),
    )
    entry = dict(
        platform="darwin_arm64",
        component_api=1,
        version="test",
        install_bytes=4096,
        archives=[
            dict(
                name="runtime.whl",
                url="https://example.invalid/runtime.whl",
                sha256="0" * 64,
                size_bytes=wheel.stat().st_size,
                extract="python-wheel",
            )
        ],
    )
    components.install_component(
        "asr-parakeet-mlx", entry, lambda *args: None, threading.Event()
    )
    assert components.is_installed("asr-parakeet-mlx")
    root = Path(components.component_dir("asr-parakeet-mlx"))
    assert all(
        (root / "site-packages" / name).read_bytes() == b"payload" for name in files
    )
    (root / "site-packages/mlx/lib/mlx.metallib").unlink()
    with pytest.raises(components.ComponentError, match="missing required files"):
        components._validate_component_payload("asr-parakeet-mlx", str(root))


def test_mlx_remote_host_exposes_auto_and_cpu_without_nvidia(monkeypatch):
    from services.remote_asr.dependencies import dependency_options
    from services.remote_asr.runtime import runtime_state

    monkeypatch.setattr(
        "services.components.current_platform_tag", lambda: "darwin_arm64"
    )
    monkeypatch.setattr(
        "services.components.is_installed", lambda key: key == "asr-parakeet-mlx"
    )
    monkeypatch.setattr("services.gpu_info.nvidia_gpu", lambda: None)
    options = dependency_options("parakeet_mlx")
    assert [option["device"] for option in options] == ["auto", "cpu"]
    assert all(option["ready"] for option in options)
    state = runtime_state(
        {"family": "parakeet_mlx", "model": "parakeet-v3-mlx", "device": "metal"}
    )
    assert state["devices"] == ["auto", "cpu"]
    assert state["languages"] == ["auto"]


def test_mlx_controls_expose_auto_and_cpu_and_automatic_language():
    from ui_qt.widgets.local_engine_controls import LocalEngineControls

    controls = LocalEngineControls()
    controls.set_backend("parakeet_mlx")
    assert [
        controls.device_combo.itemText(i) for i in range(controls.device_combo.count())
    ] == ["auto", "cpu"]
    assert controls.language_combo.currentText() == "Auto"
    assert not controls.language_combo.isEnabled()
    assert controls.model_combo.currentData() == "parakeet-v3-mlx"
    controls.close()


@pytest.mark.parametrize("tab_name", ["quick_record_tab", "upload_file_tab"])
def test_mlx_can_be_selected_in_transcription_tabs(tab_name):
    from importlib import import_module

    module = import_module(f"ui_qt.widgets.{tab_name}")
    tab_type = module.QuickRecordTab if tab_name == "quick_record_tab" else module.UploadFileTab
    tab = tab_type()
    tab.set_model_selection("parakeet_mlx")
    assert tab.current_backend() == "Parakeet MLX"
    assert tab.local_engine.model_combo.currentData() == "parakeet-v3-mlx"
    assert tab.local_engine.language_combo.currentText() == "Auto"
    tab.close()


def test_mlx_meeting_starts_window_preview(monkeypatch):
    from meeting.asr.engine import MeetingAsrEngine

    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    backend = LocalSpeechBackend("parakeet_mlx")
    engine = MeetingAsrEngine("parakeet-v3-mlx", "mlx-test", Mock(), defer_load=True)
    engine._backend = backend
    factory = Mock()
    monkeypatch.setattr("meeting.asr.preview.WindowSpeechPreview", factory)
    callback = Mock()
    engine.start_preview(callback)
    assert engine._preview is factory.return_value
    assert factory.call_args.args[:2] == (backend, callback)
    engine.stop()
