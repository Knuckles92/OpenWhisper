"""Tests for the native-library search paths set up before Qt and CTranslate2."""
from services import native_libraries


def test_frozen_startup_reuses_cuda_dlls_from_an_older_bundle(
    tmp_path, monkeypatch
):
    """An installer upgrade must not discard a previously working GPU setup."""
    bundle_root = tmp_path / "_internal"
    bin_dir = bundle_root / "nvidia" / "cublas" / "bin"
    bin_dir.mkdir(parents=True)

    registered = []
    handle = object()

    def add_dll_directory(path):
        registered.append(path)
        return handle

    monkeypatch.setattr(native_libraries.sys, "platform", "win32")
    monkeypatch.setattr(native_libraries.sys, "frozen", True, raising=False)
    monkeypatch.setattr(native_libraries.sys, "_MEIPASS", str(bundle_root), raising=False)
    monkeypatch.setattr(native_libraries.sys, "executable", str(tmp_path / "OpenWhisper.exe"))
    monkeypatch.setattr(
        native_libraries.site,
        "getsitepackages",
        lambda: (_ for _ in ()).throw(AssertionError("system site inspected")),
    )
    monkeypatch.setattr(
        native_libraries.site,
        "getusersitepackages",
        lambda: (_ for _ in ()).throw(AssertionError("user site inspected")),
    )
    monkeypatch.setattr(
        native_libraries.os, "add_dll_directory", add_dll_directory, raising=False
    )
    monkeypatch.setenv("PATH", "C:\\Windows\\System32")
    monkeypatch.setattr(native_libraries, "_CUDA_DLL_DIRECTORY_HANDLES", [])

    native_libraries.register_cuda_dll_directories()

    assert registered == [str(bin_dir)]
    assert native_libraries._CUDA_DLL_DIRECTORY_HANDLES == [handle]
    assert str(bin_dir) in native_libraries.os.environ["PATH"].split(native_libraries.os.pathsep)


def test_frozen_startup_registers_system32_for_qt_icu(tmp_path, monkeypatch):
    system32 = tmp_path / "System32"
    system32.mkdir()
    qt_bin = tmp_path / "_internal" / "PyQt6" / "Qt6" / "bin"
    qt_bin.mkdir(parents=True)

    registered = []

    def add_dll_directory(path):
        registered.append(path)
        return object()

    monkeypatch.setattr(native_libraries.sys, "platform", "win32")
    monkeypatch.setattr(native_libraries.sys, "frozen", True, raising=False)
    monkeypatch.setattr(native_libraries.sys, "_MEIPASS", str(tmp_path / "_internal"), raising=False)
    monkeypatch.setattr(native_libraries.sys, "executable", str(tmp_path / "OpenWhisper.exe"))
    monkeypatch.setattr(native_libraries.os, "add_dll_directory", add_dll_directory, raising=False)
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    monkeypatch.setenv("PATH", "C:\\Windows\\System32")
    monkeypatch.setattr(native_libraries, "_QT_ICU_DLL_HANDLES", [])

    native_libraries.register_qt_icu_directories()

    assert str(qt_bin) in registered
    assert str(system32) in registered
    path_parts = native_libraries.os.environ["PATH"].split(native_libraries.os.pathsep)
    assert str(qt_bin) in path_parts
    assert str(system32) in path_parts


def _linux_nvidia_tree(tmp_path, monkeypatch, *, libraries):
    """Stage a fake site-packages/nvidia tree and pretend we are on Linux."""
    site_packages = tmp_path / "site-packages"
    for relative in libraries:
        target = site_packages / "nvidia" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"")

    monkeypatch.setattr(native_libraries.sys, "platform", "linux")
    monkeypatch.setattr(native_libraries.site, "getsitepackages", lambda: [str(site_packages)])
    monkeypatch.setattr(native_libraries.site, "getusersitepackages", lambda: "")
    monkeypatch.setattr(native_libraries, "_CUDA_LIBRARY_HANDLES", [])
    monkeypatch.setattr(native_libraries, "CUDA_PRELOADED_LIBRARIES", [])


def test_linux_startup_preloads_nvidia_wheel_libraries(tmp_path, monkeypatch):
    """CTranslate2 dlopens libcublas.so.12 by bare SONAME.

    LD_LIBRARY_PATH cannot be changed from inside a running process, so the
    libraries must already be loaded with RTLD_GLOBAL for that lookup to resolve.
    """
    _linux_nvidia_tree(
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

    native_libraries.preload_cuda_libraries()

    assert [mode for _, mode in loaded] == [256, 256]
    assert sorted(native_libraries.CUDA_PRELOADED_LIBRARIES) == [
        "libcublas.so.12", "libcublasLt.so.12",
    ]
    # Handles must outlive the call: dropping them closes the dlopen handle.
    assert native_libraries._CUDA_LIBRARY_HANDLES == [handle, handle]


def test_linux_preload_survives_an_unloadable_library(tmp_path, monkeypatch):
    """One broken library must not stop the others, or block startup."""
    _linux_nvidia_tree(
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

    native_libraries.preload_cuda_libraries()

    assert native_libraries.CUDA_PRELOADED_LIBRARIES == ["libcublas.so.12"]


def test_preload_is_a_no_op_on_windows(tmp_path, monkeypatch):
    """Windows uses os.add_dll_directory; preloading there would be redundant."""
    _linux_nvidia_tree(
        tmp_path, monkeypatch, libraries=["cublas/lib/libcublas.so.12"],
    )
    monkeypatch.setattr(native_libraries.sys, "platform", "win32")

    native_libraries.preload_cuda_libraries()

    assert native_libraries.CUDA_PRELOADED_LIBRARIES == []
