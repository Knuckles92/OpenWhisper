"""Tests for the thin Qt entrypoint import behavior and startup profiler."""
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from ui_qt.startup_profiler import StartupProfiler

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_app_qt_import_does_not_eagerly_import_application_controller():
    code = """
import sys
import app_qt
assert hasattr(app_qt, 'main')
assert 'services.application_controller' not in sys.modules
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


def test_frozen_startup_reuses_cuda_dlls_from_an_older_bundle(
    tmp_path, monkeypatch
):
    """An installer upgrade must not discard a previously working GPU setup."""
    import app_qt

    bundle_root = tmp_path / "_internal"
    bin_dir = bundle_root / "nvidia" / "cublas" / "bin"
    bin_dir.mkdir(parents=True)

    registered = []
    handle = object()

    def add_dll_directory(path):
        registered.append(path)
        return handle

    monkeypatch.setattr(app_qt.sys, "platform", "win32")
    monkeypatch.setattr(app_qt.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_qt.sys, "_MEIPASS", str(bundle_root), raising=False)
    monkeypatch.setattr(app_qt.sys, "executable", str(tmp_path / "OpenWhisper.exe"))
    monkeypatch.setattr(
        app_qt.site,
        "getsitepackages",
        lambda: (_ for _ in ()).throw(AssertionError("system site inspected")),
    )
    monkeypatch.setattr(
        app_qt.site,
        "getusersitepackages",
        lambda: (_ for _ in ()).throw(AssertionError("user site inspected")),
    )
    monkeypatch.setattr(
        app_qt.os, "add_dll_directory", add_dll_directory, raising=False
    )
    monkeypatch.setenv("PATH", "C:\\Windows\\System32")
    app_qt._CUDA_DLL_DIRECTORY_HANDLES.clear()

    app_qt._register_cuda_dll_directories()

    assert registered == [str(bin_dir)]
    assert app_qt._CUDA_DLL_DIRECTORY_HANDLES == [handle]
    assert str(bin_dir) in app_qt.os.environ["PATH"].split(app_qt.os.pathsep)


def test_frozen_startup_registers_system32_for_qt_icu(tmp_path, monkeypatch):
    import app_qt

    system32 = tmp_path / "System32"
    system32.mkdir()
    qt_bin = tmp_path / "_internal" / "PyQt6" / "Qt6" / "bin"
    qt_bin.mkdir(parents=True)

    registered = []

    def add_dll_directory(path):
        registered.append(path)
        return object()

    monkeypatch.setattr(app_qt.sys, "platform", "win32")
    monkeypatch.setattr(app_qt.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_qt.sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    monkeypatch.setattr(app_qt.sys, "executable", str(tmp_path / "OpenWhisper.exe"))
    monkeypatch.setattr(app_qt.os, "add_dll_directory", add_dll_directory, raising=False)
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setenv("PATH", "C:\\Windows\\System32")
    app_qt._QT_ICU_DLL_HANDLES.clear()

    app_qt._register_qt_icu_directories()

    assert str(qt_bin) in registered
    assert str(system32) in registered
    path_parts = app_qt.os.environ["PATH"].split(app_qt.os.pathsep)
    assert str(qt_bin) in path_parts
    assert str(system32) in path_parts


def _linux_nvidia_tree(tmp_path, monkeypatch, *, libraries):
    """Stage a fake site-packages/nvidia tree and pretend we are on Linux."""
    import app_qt

    site_packages = tmp_path / "site-packages"
    for relative in libraries:
        target = site_packages / "nvidia" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")

    monkeypatch.setattr(app_qt.sys, "platform", "linux")
    monkeypatch.setattr(app_qt.site, "getsitepackages", lambda: [str(site_packages)])
    monkeypatch.setattr(app_qt.site, "getusersitepackages", lambda: "")
    app_qt._CUDA_LIBRARY_HANDLES.clear()
    app_qt.CUDA_PRELOADED_LIBRARIES.clear()
    return app_qt


def test_linux_startup_preloads_nvidia_wheel_libraries(tmp_path, monkeypatch):
    """CTranslate2 dlopens libcublas.so.12 by bare SONAME.

    LD_LIBRARY_PATH cannot be changed from inside a running process, so the
    libraries must already be loaded with RTLD_GLOBAL for that lookup to resolve.
    """
    app_qt = _linux_nvidia_tree(
        tmp_path, monkeypatch,
        libraries=["cublas/lib/libcublas.so.12", "cublas/lib/libcublasLt.so.12"],
    )

    loaded = []
    handle = object()

    class _FakeCtypes:
        RTLD_GLOBAL = 256

        @staticmethod
        def CDLL(path, mode=None):
            loaded.append((path, mode))
            return handle

    monkeypatch.setitem(__import__("sys").modules, "ctypes", _FakeCtypes)

    app_qt._preload_cuda_libraries()

    assert [mode for _, mode in loaded] == [256, 256]
    assert sorted(app_qt.CUDA_PRELOADED_LIBRARIES) == [
        "libcublas.so.12", "libcublasLt.so.12",
    ]
    # Handles must outlive the call: dropping them closes the dlopen handle.
    assert app_qt._CUDA_LIBRARY_HANDLES == [handle, handle]


def test_linux_preload_survives_an_unloadable_library(tmp_path, monkeypatch):
    """One broken library must not stop the others, or block startup."""
    app_qt = _linux_nvidia_tree(
        tmp_path, monkeypatch,
        libraries=["cublas/lib/libcublas.so.12", "cudnn/lib/libcudnn.so.9"],
    )

    class _FakeCtypes:
        RTLD_GLOBAL = 256

        @staticmethod
        def CDLL(path, mode=None):
            if "cudnn" in path:
                raise OSError("cannot open shared object file")
            return object()

    monkeypatch.setitem(__import__("sys").modules, "ctypes", _FakeCtypes)

    app_qt._preload_cuda_libraries()

    assert app_qt.CUDA_PRELOADED_LIBRARIES == ["libcublas.so.12"]


def test_preload_is_a_no_op_on_windows(tmp_path, monkeypatch):
    """Windows uses os.add_dll_directory; preloading there would be redundant."""
    app_qt = _linux_nvidia_tree(
        tmp_path, monkeypatch, libraries=["cublas/lib/libcublas.so.12"],
    )
    monkeypatch.setattr(app_qt.sys, "platform", "win32")

    app_qt._preload_cuda_libraries()

    assert app_qt.CUDA_PRELOADED_LIBRARIES == []


# Startup profiling hooks.

def test_startup_profiler_records_elapsed_times():
    with patch(
        "ui_qt.startup_profiler.time.perf_counter",
        side_effect=[10.25, 10.75],
    ):
        profiler = StartupProfiler(start_time=10.0)
        profiler.mark("first")
        profiler.mark("second")

    assert profiler.events == [("first", 0.25), ("second", 0.75)]


def test_startup_profiler_logs_totals_and_deltas(caplog):
    profiler = StartupProfiler(
        start_time=0.0,
        events=[("first", 0.5), ("second", 0.8)],
    )

    with caplog.at_level(logging.INFO, logger="ui_qt.startup_profiler"):
        profiler.log_summary()

    assert "Startup timing summary:" in caplog.text
    assert "first" in caplog.text
    assert "total=  0.500s" in caplog.text
    assert "delta=  0.500s" in caplog.text
    assert "second" in caplog.text
    assert "total=  0.800s" in caplog.text
    assert "delta=  0.300s" in caplog.text
