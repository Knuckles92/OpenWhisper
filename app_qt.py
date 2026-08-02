"""Thin compatibility entrypoint for the Qt application"""

import os
import platform
import site
import subprocess
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", message="pkg_resources is deprecated")


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
    """Register NVIDIA CUDA DLL directories for CTranslate2 on Windows.

    Both the Windows DLL search path and ``PATH`` are updated because
    CTranslate2 loads CUDA libraries through Win32 ``LoadLibrary``. Source
    installs search ``site-packages``; frozen builds search bundle directories.
    """
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

    nvidia_subdirs = ("cublas", "cudnn", "cuda_runtime", "cuda_nvrtc")
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
    """Patch subprocess.Popen to hide console windows on Windows."""
    if platform.system() != "Windows":
        return

    original_popen = subprocess.Popen

    class _NoConsolePopen(original_popen):
        """Popen wrapper that adds CREATE_NO_WINDOW on Windows."""

        def __init__(self, *args, **kwargs):
            if "creationflags" not in kwargs:
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            elif not (kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW):
                kwargs["creationflags"] |= subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoConsolePopen


def _activate_downloadable_components() -> None:
    """Register DLLs from installed components (packaged builds only).

    Source installs get their CUDA runtime from ``requirements-gpu.txt`` via
    the two functions above; packaged builds get it from the downloadable GPU
    component instead.
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        from services.component_runtime import activate_components
        activate_components()
    except Exception:
        # Never let a broken component stop the app from starting.
        pass


_register_cuda_dll_directories()
_preload_cuda_libraries()
_activate_downloadable_components()
_patch_subprocess_for_windows()

from ui_qt.bootstrap import main

__all__ = ["main"]


if __name__ == "__main__":
    sys.exit(main())
