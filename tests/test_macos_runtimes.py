"""Apple Silicon speech runtimes: Qwen, Moonshine and NeMo-Speech.cpp on Metal."""
from __future__ import annotations

import io
import shutil
import sys
import tarfile
import threading
import zipfile
from pathlib import Path

import pytest

from services import components
from services.local_asr.catalog import (
    NVIDIA_METAL_RUNTIME,
    resolve_runtime,
    runtime_catalog,
    runtime_id,
)
from transcriber.optional_backend import LocalSpeechBackend


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(components, "current_platform_tag", lambda: "darwin_arm64")
    installed = set()
    monkeypatch.setattr(components, "is_installed", lambda key: key in installed)
    return installed


def test_every_engine_has_a_pinned_mac_runtime():
    catalog = runtime_catalog()
    mac = {key for key, runtime in catalog.items() if "darwin_arm64" in runtime["platforms"]}
    assert mac == {
        "asr-nvidia-cpu", NVIDIA_METAL_RUNTIME, "asr-parakeet-mlx", "asr-qwen", "asr-moonshine",
    }
    intel = {key for key, runtime in catalog.items() if "darwin_x86_64" in runtime["platforms"]}
    assert intel == {"asr-nvidia-cpu"}
    (metal,) = catalog[NVIDIA_METAL_RUNTIME]["platforms"]["darwin_arm64"]["archives"]
    assert metal["extract"] == "nemo-tar"
    assert metal["url"].endswith("/v0.1.0/nemo-speech-0.1.0-macos-aarch64-metal.tar.gz")
    for key, package, minimum in (
        ("asr-qwen", "qwen_asr-0.0.6-", "14.0"),
        ("asr-moonshine", "moonshine_voice-0.1.5-", "15.0"),
    ):
        entry = catalog[key]["platforms"]["darwin_arm64"]
        assert entry["python_abi"] == "cp312" and entry["macos_min"] == minimum
        assert any(a["name"].startswith(package) for a in entry["archives"])
        assert all(a["extract"] == "python-wheel" for a in entry["archives"])
        # Only wheels this Mac can load: arm64, universal2 or pure Python.
        for archive in entry["archives"]:
            name = archive["name"]
            assert name.endswith(("-none-any.whl", "_arm64.whl", "_universal2.whl")), name


def test_qwen_auto_uses_the_apple_gpu(mac):
    assert resolve_runtime("qwen_asr", "auto") == ("asr-qwen", "mps")
    assert resolve_runtime("qwen_asr", "cpu") == ("asr-qwen", "cpu")
    # A "cuda" saved on another computer means this Mac's GPU, never an error.
    assert resolve_runtime("qwen_asr", "cuda") == ("asr-qwen", "mps")


@pytest.mark.parametrize("backend", ["parakeet", "nemotron"])
def test_nvidia_speech_auto_prefers_metal_but_keeps_an_installed_cpu_runtime(mac, backend):
    # Nothing installed: Auto suggests the Apple GPU runtime.
    assert resolve_runtime(backend, "auto") == (NVIDIA_METAL_RUNTIME, "metal")
    # An existing CPU install keeps working until Metal is added.
    mac.add("asr-nvidia-cpu")
    assert resolve_runtime(backend, "auto") == ("asr-nvidia-cpu", "cpu")
    mac.add(NVIDIA_METAL_RUNTIME)
    assert resolve_runtime(backend, "auto") == (NVIDIA_METAL_RUNTIME, "metal")
    assert resolve_runtime(backend, "cpu") == ("asr-nvidia-cpu", "cpu")
    assert resolve_runtime(backend, "cuda") == (NVIDIA_METAL_RUNTIME, "metal")
    assert runtime_id(backend, "auto") == NVIDIA_METAL_RUNTIME
    assert runtime_id(backend, "cpu") == "asr-nvidia-cpu"


def test_missing_mac_runtimes_point_to_downloads(mac, monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr(components, "_macos_version", lambda: (15, 0))
    for backend, expected, kind in (
        ("qwen_asr", "asr-qwen", "GPU"),
        ("nemotron", NVIDIA_METAL_RUNTIME, "GPU"),
        ("moonshine", "asr-moonshine", "CPU"),
    ):
        engine = LocalSpeechBackend(backend)
        engine.reload_model()
        assert engine.runtime_component == expected
        assert f"{kind} runtime in Downloads" in engine.last_error


def test_moonshine_explains_its_macos_15_requirement(mac, monkeypatch):
    monkeypatch.setattr(LocalSpeechBackend, "_settings", staticmethod(lambda: {}))
    monkeypatch.setattr(components, "_macos_version", lambda: (14, 6))
    entry = components.catalog_entry_for_platform("asr-moonshine")
    assert components.check_compatibility(entry) == "Requires macOS 15.0 or later"
    with pytest.raises(components.ComponentError, match="macOS 15.0"):
        components.install_component("asr-moonshine", entry, lambda *a: None, threading.Event())
    engine = LocalSpeechBackend("moonshine")
    engine.reload_model()
    assert engine.runtime_component is None
    assert "Requires macOS 15.0" in engine.last_error
    assert "Downloads" not in engine.last_error


def test_download_prompt_skips_a_runtime_this_mac_cannot_load(mac, monkeypatch):
    from services.local_asr.catalog import missing_runtime

    monkeypatch.setattr(components, "_macos_version", lambda: (14, 6))
    assert missing_runtime("moonshine-small", {}) is None
    assert missing_runtime("qwen-0.6b", {}) == "asr-qwen"
    monkeypatch.setattr(components, "_macos_version", lambda: (15, 1))
    assert missing_runtime("moonshine-small", {}) == "asr-moonshine"


def test_macos_version_gate_ignores_other_platforms(monkeypatch):
    monkeypatch.setattr(components, "_macos_version", lambda: None)
    assert components._below_macos_minimum({"macos_min": "15.0"}) is None
    monkeypatch.setattr(components, "_macos_version", lambda: (26, 6, 2))
    assert components._below_macos_minimum({"macos_min": "15.0"}) is None
    assert components._below_macos_minimum({}) is None


def _wheel(path: Path, files) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in files:
            archive.writestr(name, b"payload")
    return path


@pytest.mark.parametrize("component_id, files", [
    ("asr-moonshine", (
        "moonshine_voice/__init__.py",
        "_sounddevice_data/portaudio-binaries/libportaudio.dylib",
    )),
    ("asr-qwen", (
        "qwen_asr/__init__.py",
        "torch/__init__.py",
        "torch/lib/libtorch_cpu.dylib",
        "transformers/__init__.py",
    )),
])
def test_mac_wheel_runtimes_install_into_site_packages(monkeypatch, tmp_path, component_id, files):
    monkeypatch.setattr(components, "current_platform_tag", lambda: "darwin_arm64")
    monkeypatch.setattr(components, "components_root", lambda: str(tmp_path / "components"))
    monkeypatch.setattr(components, "_macos_version", lambda: (15, 0))
    wheel = _wheel(tmp_path / "runtime.whl", files)
    monkeypatch.setattr(
        components, "_download_verified",
        lambda url, sha, size, target, *args, **kwargs: shutil.copyfile(wheel, target),
    )
    entry = dict(
        platform="darwin_arm64", component_api=1, version="test", install_bytes=4096,
        python_abi=f"cp{sys.version_info.major}{sys.version_info.minor}", macos_min="15.0",
        archives=[dict(name="runtime.whl", url="https://example.invalid/runtime.whl",
                       sha256="0" * 64, size_bytes=wheel.stat().st_size, extract="python-wheel")],
    )
    components.install_component(component_id, entry, lambda *args: None, threading.Event())
    root = Path(components.component_dir(component_id))
    assert all((root / "site-packages" / name).is_file() for name in files)
    (root / "site-packages" / files[-1]).unlink()
    with pytest.raises(components.ComponentError, match="missing required files"):
        components._validate_component_payload(component_id, str(root))


def _nemo_tar(path: Path, libraries) -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name in libraries:
            info = tarfile.TarInfo(f"nemo-speech/lib/{name}")
            info.size = 7
            archive.addfile(info, io.BytesIO(b"payload"))
    return path


def test_metal_runtime_requires_the_metal_backend(monkeypatch, tmp_path):
    monkeypatch.setattr(components, "current_platform_tag", lambda: "darwin_arm64")
    stage = tmp_path / "stage"
    for libraries, ok in (
        (("libnemo_speech_asr_c.dylib", "libggml-metal.dylib"), True),
        (("libnemo_speech_asr_c.dylib",), False),
    ):
        shutil.rmtree(stage, ignore_errors=True)
        archive = _nemo_tar(tmp_path / "metal.tar.gz", libraries)
        components._safe_extract_nemo_tar(str(archive), str(stage), lambda *a: None, threading.Event())
        if ok:
            components._validate_component_payload(NVIDIA_METAL_RUNTIME, str(stage))
        else:
            with pytest.raises(components.ComponentError, match="missing required files"):
                components._validate_component_payload(NVIDIA_METAL_RUNTIME, str(stage))


def test_mac_nemo_adapter_refuses_cuda_and_a_cpu_only_runtime(monkeypatch, tmp_path):
    from services.local_asr import nvidia

    monkeypatch.setattr(nvidia.sys, "platform", "darwin")
    lib = tmp_path / "nemo-speech" / "lib"
    lib.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="Apple GPU or CPU"):
        nvidia.NvidiaRecognizer(str(tmp_path), "model.gguf", "cuda")
    with pytest.raises(RuntimeError, match="Metal"):
        nvidia.NvidiaRecognizer(str(tmp_path), "model.gguf", "metal")


def test_worker_puts_a_mac_wheel_tree_first(monkeypatch, tmp_path):
    from services.local_asr import worker

    monkeypatch.setattr(sys, "path", list(sys.path))
    worker.use_runtime_packages(str(tmp_path))  # Windows: an embedded Python.
    assert str(tmp_path / "site-packages") not in sys.path
    (tmp_path / "site-packages").mkdir()
    worker.use_runtime_packages(str(tmp_path))
    worker.use_runtime_packages(str(tmp_path))
    assert sys.path[0] == str(tmp_path / "site-packages")
    assert sys.path.count(str(tmp_path / "site-packages")) == 1


@pytest.mark.parametrize("family, runtime", [
    ("parakeet", NVIDIA_METAL_RUNTIME), ("nemotron", NVIDIA_METAL_RUNTIME), ("qwen_asr", "asr-qwen"),
])
def test_mac_remote_host_offers_auto_and_cpu(mac, monkeypatch, family, runtime):
    from services.remote_asr.dependencies import dependency_options
    from services.remote_asr.runtime import runtime_state

    monkeypatch.setattr("services.gpu_info.nvidia_gpu", lambda: None)
    monkeypatch.setattr(components, "available_component_ids", lambda *a: (
        "asr-nvidia-cpu", NVIDIA_METAL_RUNTIME, "asr-qwen", "asr-moonshine",
    ))
    mac.add(runtime)
    options = {option["device"]: option for option in dependency_options(family)}
    assert list(options) == ["auto", "cpu"]
    assert options["auto"]["component"] == runtime and options["auto"]["ready"]
    assert runtime_state({"family": family, "model": "", "device": "metal"})["devices"][0] == "auto"


def test_intel_macs_run_parakeet_and_nemotron_on_the_cpu_runtime(monkeypatch):
    monkeypatch.setattr(components, "current_platform_tag", lambda: "darwin_x86_64")
    monkeypatch.setattr(components, "is_installed", lambda key: False)
    monkeypatch.setattr("ctranslate2.get_cuda_device_count", lambda: 0)
    assert resolve_runtime("nemotron", "auto") == ("asr-nvidia-cpu", "cpu")
    assert runtime_id("parakeet", "cpu") == "asr-nvidia-cpu"
    (archive,) = components.catalog_entry_for_platform("asr-nvidia-cpu")["archives"]
    assert archive["name"] == "nemo-speech-0.1.0-macos-x86_64-cpu.tar.gz"
    assert components.catalog_entry_for_platform("asr-qwen") is None
