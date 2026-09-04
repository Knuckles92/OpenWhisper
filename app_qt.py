"""Qt entry point and pre-import native-library bootstrap."""

import logging
import os
import platform
import site
import subprocess
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message="pkg_resources is deprecated")


def _run_package_self_test() -> None:
    """Import release-critical modules and initialize Qt without starting UI."""
    import importlib

    modules = [
        "av",
        "ctranslate2",
        "faster_whisper",
        "lxml",
        "onnxruntime",
        "openpyxl",
        "pypdf",
        "sounddevice",
        "sqlalchemy.dialects.sqlite",
        "uvicorn",
    ]
    if sys.platform == "win32":
        modules.extend(("keyring.backends.Windows", "keyboard", "soundcard"))
    elif sys.platform == "darwin":
        modules.extend(
            (
                "keyring.backends.macOS",
                "pynput.keyboard",
                # Meeting Mode / Accessibility stack is imported lazily at
                # runtime; freeze self-test must prove the bindings survived.
                "objc",
                "Foundation",
                "HIServices",
                "ScreenCaptureKit",
                "CoreMedia",
                "Quartz",
            )
        )
    else:
        modules.extend(
            (
                "keyring.backends.SecretService",
                "pynput.keyboard",
                "secretstorage",
            )
        )
    for module_name in modules:
        importlib.import_module(module_name)

    from PyQt6.QtWidgets import QApplication

    qt_app = QApplication.instance() or QApplication([])

    from config import bundle_root

    root = Path(bundle_root())
    for relative in (
        "ui_qt/styles/theme.qss",
        "ui_qt/assets/openwhisper.ico",
        "ui_qt/assets/openwhisper.png",
        "webui/dist/index.html",
    ):
        if not (root / relative).is_file():
            raise RuntimeError(f"Missing bundled asset: {relative}")

    from PyQt6.QtGui import QIcon

    svg_icon = QIcon(str(root / "ui_qt/assets/check.svg"))
    if svg_icon.isNull():
        raise RuntimeError("Qt could not load the bundled SVG image plugin.")
    del qt_app
    print("OpenWhisper package self-test passed")


def _handle_early_cli() -> None:
    """Handle metadata-only CLI flags before native-library bootstrap."""
    if sys.argv[1:] == ["--version"]:
        from _version import __version__

        print(f"OpenWhisper {__version__}")
        raise SystemExit(0)


def _handle_package_self_test() -> None:
    """Run the import test after platform DLL/library bootstrap is complete."""
    if sys.argv[1:] == ["--self-test"]:
        _run_package_self_test()
        raise SystemExit(0)


_handle_early_cli()


# ``os.add_dll_directory`` unregisters a path when its returned handle is
# closed. Keep the handles alive for the process lifetime; relying on garbage
# collection here makes CUDA availability depend on timing.
_CUDA_DLL_DIRECTORY_HANDLES = []

# Same reasoning for Linux: a ``ctypes.CDLL`` object owns its ``dlopen`` handle
# and closes it when collected, which would unload libraries CTranslate2 is
# about to request by SONAME.
_CUDA_LIBRARY_HANDLES = []

# Names of the libraries preloaded on Linux, for ``ui_qt.bootstrap`` to log once
# logging is configured. This module runs before that, so it cannot log itself.
CUDA_PRELOADED_LIBRARIES: list = []


def _register_cuda_dll_directories() -> None:
    if sys.platform != "win32":
        return

    if getattr(sys, "frozen", False):
        executable_root = Path(sys.executable).resolve().parent
        search_roots = [
            Path(getattr(sys, "_MEIPASS", executable_root)),
            executable_root,
            executable_root / "_internal",
        ]
    else:
        search_roots = [Path(site_dir) for site_dir in site.getsitepackages()]
        user_site = site.getusersitepackages()
        if user_site:
            search_roots.append(Path(user_site))

    nvidia_subdirs = ("cublas", "cuda_runtime", "cuda_nvrtc")
    bin_dirs = []
    for root in dict.fromkeys(search_roots):
        nvidia_root = root / "nvidia"
        if not nvidia_root.is_dir():
            continue
        for subdir in nvidia_subdirs:
            bin_dir = nvidia_root / subdir / "bin"
            if bin_dir.is_dir():
                try:
                    handle = os.add_dll_directory(str(bin_dir))
                except OSError:
                    continue
                _CUDA_DLL_DIRECTORY_HANDLES.append(handle)
                bin_dirs.append(str(bin_dir))

    if bin_dirs:
        existing = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join(bin_dirs) + os.pathsep + existing


def _preload_cuda_libraries() -> None:
    """Preload NVIDIA pip-wheel CUDA libraries for CTranslate2 on Linux.

    Linux has no mutable equivalent of the Windows DLL search path:
    ``LD_LIBRARY_PATH`` is read by ``ld.so`` at process start, so setting it
    from inside a running process has no effect. CTranslate2 ``dlopen``s
    ``libcublas.so.12`` by bare SONAME, and glibc satisfies such a request from
    already-loaded objects first — so loading the wheel's copy from its absolute
    path with ``RTLD_GLOBAL`` here makes that later lookup resolve without any
    environment variable. PyTorch bootstraps its own NVIDIA wheels the same way.

    Never raises: absent or broken CUDA libraries must degrade to CPU
    transcription, never prevent the application from starting.
    """
    if sys.platform != "linux":
        return

    import ctypes

    try:
        search_roots = [Path(site_dir) for site_dir in site.getsitepackages()]
        user_site = site.getusersitepackages()
        if user_site:
            search_roots.append(Path(user_site))
    except Exception:
        return

    for root in dict.fromkeys(search_roots):
        nvidia_root = root / "nvidia"
        if not nvidia_root.is_dir():
            continue
        # Load order does not matter: each wheel's libraries resolve their
        # siblings through their own RPATH, so anything that cannot load on its
        # own is not a library CTranslate2 would have found either.
        for library in sorted(nvidia_root.glob("*/lib/*.so.*")):
            try:
                handle = ctypes.CDLL(str(library), mode=ctypes.RTLD_GLOBAL)
            except OSError:
                continue
            _CUDA_LIBRARY_HANDLES.append(handle)
            CUDA_PRELOADED_LIBRARIES.append(library.name)


def _patch_subprocess_for_windows() -> None:
    if platform.system() != "Windows":
        return

    original_popen = subprocess.Popen

    class _NoConsolePopen(original_popen):
        def __init__(self, *args, **kwargs):
            if "creationflags" not in kwargs:
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            elif not (kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW):
                kwargs["creationflags"] |= subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoConsolePopen


def _activate_downloadable_components() -> None:
    if not getattr(sys, "frozen", False):
        return
    try:
        from services.component_runtime import activate_components
        activate_components()
    except Exception:
        # Never let a broken component stop the app from starting.
        pass


_QT_ICU_DLL_HANDLES = []


def _register_qt_icu_directories() -> None:
    """Make Qt able to resolve ``icuuc.dll`` before ``PyQt6`` is imported.

    Qt 6.11's ``Qt6Core.dll`` imports ``icuuc.dll``, but the PyQt6 6.11
    wheel does not ship ICU. Windows 10+ provides the libraries in
    System32; a frozen onedir also keeps copies next to ``Qt6Core.dll``
    when the installer build can collect them. Either path must be on
    the Python 3.8+ DLL search list or a clean launch dies in the
    Windows loader before Python starts.
    """
    if sys.platform != "win32":
        return

    roots = []
    if getattr(sys, "frozen", False):
        executable_root = Path(sys.executable).resolve().parent
        meipass = Path(getattr(sys, "_MEIPASS", executable_root))
        roots.extend((
            meipass / "PyQt6" / "Qt6" / "bin",
            executable_root / "_internal" / "PyQt6" / "Qt6" / "bin",
        ))
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    roots.append(Path(system_root) / "System32")

    existing = os.environ.get("PATH", "")
    path_parts = existing.split(os.pathsep)
    prepend = []
    for root in roots:
        if not root.is_dir():
            continue
        path = str(root)
        try:
            _QT_ICU_DLL_HANDLES.append(os.add_dll_directory(path))
        except OSError:
            continue
        if path not in path_parts and path not in prepend:
            prepend.append(path)
    if prepend:
        os.environ["PATH"] = os.pathsep.join(prepend) + os.pathsep + existing


def _early_update_gate() -> None:
    """Wait out a running setup, and refuse a launch the updater owns."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    from services.app_update_apply import (
        acquire_application_mutex_or_exit,
        maybe_exit_if_update_in_progress,
        recover_before_start,
    )

    maybe_exit_if_update_in_progress()
    if recover_before_start():
        raise SystemExit(0)
    acquire_application_mutex_or_exit()


_early_update_gate()
_register_cuda_dll_directories()
_register_qt_icu_directories()
_preload_cuda_libraries()
_activate_downloadable_components()
_patch_subprocess_for_windows()

if sys.platform.startswith("linux"):
    from services.linux_deps import check_linux_dependencies

    if check_linux_dependencies() != 0:
        sys.exit(1)

# PyQt imports in the self-test must happen only after Windows ICU directories
# and Linux shared-library preflight have been configured.
_handle_package_self_test()

from ui_qt.bootstrap import main

__all__ = ["main"]


if __name__ == "__main__":
    exit_code = main()
    # ``main`` has already run every cleanup path and logged the shutdown.
    # Finalizing on top of Qt's teardown and the keyboard library's listener
    # thread — which cannot be stopped — instead produced access violations in
    # openwhisper.crash.log on the way out. Nothing is left to lose here.
    logging.shutdown()
    os._exit(exit_code)
