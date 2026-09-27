"""GPU Acceleration on Linux x86_64: the same Downloads component as Windows.

It used to be Windows-only, so a Linux machine with an NVIDIA GPU logged
"install requirements-gpu.txt" and ran on the CPU. The component pins the
same CUDA 12.9 wheels' manylinux builds, keeps their shared objects in one
``lib`` folder, and loads them globally before CTranslate2 asks by name.
"""
import threading
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from services import component_runtime, components
from services.components import ComponentError, ComponentId


@pytest.fixture
def component_root(tmp_path, monkeypatch):
    root = tmp_path / "components"
    root.mkdir()
    monkeypatch.setattr(components, "components_root", lambda: str(root))
    return root


@pytest.fixture
def on_linux(monkeypatch):
    monkeypatch.setattr(components.sys, "platform", "linux")
    monkeypatch.setattr(components.platform_module, "machine", lambda: "x86_64")
    monkeypatch.setattr(component_runtime.sys, "platform", "linux")


def _wheel(path: Path, members: dict) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_linux_x86_64_offers_gpu_acceleration(on_linux):
    assert ComponentId.GPU_ACCEL in components.available_component_ids()
    assert components.component_is_published(ComponentId.GPU_ACCEL)


def test_linux_entry_pins_the_windows_cuda_releases():
    windows = components.catalog_entry_for_platform(ComponentId.GPU_ACCEL, "win_amd64")
    linux = components.catalog_entry_for_platform(
        ComponentId.GPU_ACCEL, components.PLATFORM_LINUX_X86_64
    )

    def releases(entry):
        return sorted("-".join(a["name"].split("-")[:2]) for a in entry["archives"])

    assert releases(linux) == releases(windows)
    assert linux["version"] == windows["version"] == components.GPU_COMPONENT_VERSION
    assert linux["platform"] == components.PLATFORM_LINUX_X86_64
    assert all("manylinux" in a["name"] and a["url"].endswith(a["name"]) for a in linux["archives"])
    assert linux["install_bytes"] == 1_083_186_880
    assert sum(a["size_bytes"] for a in linux["archives"]) == 674_301_418


def test_wheel_extract_keeps_shared_objects_in_lib(tmp_path):
    wheel = _wheel(tmp_path / "cublas.whl", {
        "nvidia/cublas/lib/libcublas.so.12": b"cublas",
        "nvidia/cublas/lib/libcublasLt.so.12": b"lt",
        "nvidia/cublas/lib/__init__.py": "",
        "nvidia/cublas/include/cublas.h": "",
        "nvidia_cublas_cu12-12.9.2.10.dist-info/RECORD": "",
    })
    out = tmp_path / "out"

    components._safe_extract_nvidia_wheel(str(wheel), str(out), lambda *a: None, threading.Event())

    assert sorted(p.name for p in (out / "lib").iterdir()) == ["libcublas.so.12", "libcublasLt.so.12"]
    assert not (out / "bin").exists()


def test_install_on_linux_validates_the_lib_folder(component_root, tmp_path, monkeypatch, on_linux):
    sources = {
        "cublas.whl": _wheel(tmp_path / "cublas.whl", {
            "nvidia/cublas/lib/libcublas.so.12": b"cublas",
            "nvidia/cublas/lib/libcublasLt.so.12": b"lt",
        }),
        "nvrtc.whl": _wheel(tmp_path / "nvrtc.whl", {
            "nvidia/cuda_nvrtc/lib/libnvrtc-builtins.so.12.9": b"builtins",
        }),
    }

    def fake_download(_url, _sha, _size, destination, *_args, **_kwargs):
        Path(destination).write_bytes(sources[Path(destination).name].read_bytes())

    monkeypatch.setattr(components, "_download_verified", fake_download)
    entry = {
        "version": "test", "component_api": components.COMPONENT_API,
        "platform": components.PLATFORM_LINUX_X86_64, "install_bytes": 16,
        "archives": [
            {"name": name, "url": f"https://example.invalid/{name}", "sha256": "unused",
             "size_bytes": 8, "extract": "nvidia-wheel"}
            for name in sources
        ],
    }

    components.install_component(ComponentId.GPU_ACCEL, entry, lambda *a: None, threading.Event())

    lib = component_root / ComponentId.GPU_ACCEL / "lib"
    assert (lib / "libcublas.so.12").read_bytes() == b"cublas"
    assert (lib / "libnvrtc-builtins.so.12.9").read_bytes() == b"builtins"


def test_linux_payload_without_cublaslt_is_rejected(tmp_path, on_linux):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "libcublas.so.12").write_bytes(b"cublas")

    with pytest.raises(ComponentError, match="libcublasLt.so.12"):
        components._validate_component_payload(ComponentId.GPU_ACCEL, str(tmp_path))


def test_linux_activation_preloads_the_libraries(tmp_path, on_linux, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("libcublas.so.12", "libcublasLt.so.12", "README"):
        (lib / name).write_bytes(b"")
    loaded = []
    monkeypatch.setattr(component_runtime, "PRELOADED_LIBRARIES", [])
    answers = iter([False, True])  # before and after the preload
    monkeypatch.setattr(components, "gpu_runtime_available", lambda: next(answers))

    with patch("ctypes.CDLL", side_effect=lambda path, mode=0: loaded.append(Path(path).name) or object()):
        ok, reason = component_runtime._activate_gpu_libraries(str(tmp_path))

    assert (ok, reason) == (True, "")
    assert loaded == ["libcublas.so.12", "libcublasLt.so.12"]
    assert component_runtime.PRELOADED_LIBRARIES == loaded


def test_linux_activation_skips_a_second_copy(tmp_path, on_linux, monkeypatch):
    """CUDA from the pip wheels is loaded first; loading cuBLAS twice only costs memory."""
    (tmp_path / "lib").mkdir()
    monkeypatch.setattr(components, "gpu_runtime_available", lambda: True)

    with patch("ctypes.CDLL") as cdll:
        assert component_runtime._activate_gpu_libraries(str(tmp_path)) == (True, "")
    cdll.assert_not_called()


def test_linux_activation_reports_libraries_that_do_not_load(tmp_path, on_linux, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "libcublas.so.12").write_bytes(b"")
    monkeypatch.setattr(components, "gpu_runtime_available", lambda: False)

    with patch("ctypes.CDLL", side_effect=OSError("wrong ELF class")):
        ok, reason = component_runtime._activate_gpu_libraries(str(tmp_path))

    assert not ok and "could not be loaded" in reason


def test_downloads_copy_is_not_windows_only():
    from services.component_catalog import get_component_details

    details = get_component_details(ComponentId.GPU_ACCEL)
    assert "Linux" in details.best_for
    assert "Windows machines" not in details.best_for
    assert "DLLs on Windows" in details.local_format
