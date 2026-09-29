"""Activate installed components before dependent imports and Qt startup."""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Kept for the process lifetime. os.add_dll_directory returns a handle whose
# close() un-registers the directory; holding the references makes the intent
# explicit and guarantees the paths stay registered for as long as we run.
_DLL_DIRECTORY_HANDLES: List[object] = []
# Same for Linux: a ctypes.CDLL closes its dlopen handle when collected.
_SHARED_LIBRARY_HANDLES: List[object] = []
# Their names, for ui_qt.bootstrap to log: activation runs before logging is.
PRELOADED_LIBRARIES: List[str] = []


@dataclass(frozen=True)
class ActivationReport:
    """Outcome of activating installed components."""

    activated: Tuple[str, ...] = ()
    rejected: Tuple[Tuple[str, str], ...] = ()


def register_dll_directory(path: str) -> bool:
    """Make ``path`` searchable for native library loads.

    Registers the directory two ways, because the two consumers use different
    search mechanisms:

    * ``os.add_dll_directory`` satisfies Python's own loader and ``ctypes``.
    * prepending to ``PATH`` satisfies CTranslate2's C++ ``LoadLibrary`` call,
      which ignores the ``add_dll_directory`` registry.

    Without the PATH prepend, transcription fails with "Library
    cublas64_12.dll is not found or cannot be loaded" even though the DLL is
    present. This mirrors the behavior of ``_register_cuda_dll_directories``
    in ``main.py`` for source installs.

    """
    if sys.platform != "win32" or not os.path.isdir(path):
        return False

    try:
        _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(path))
    except OSError as exc:
        logger.warning(f"Could not register DLL directory {path}: {exc}")
        return False

    existing = os.environ.get("PATH", "")
    if path not in existing.split(os.pathsep):
        os.environ["PATH"] = path + os.pathsep + existing
    return True


def preload_shared_libraries(path: str) -> List[str]:
    """Load every shared object in ``path`` globally (Linux); returns their names.

    Linux has nothing like the Windows DLL search path to add a folder to:
    ``LD_LIBRARY_PATH`` is read once, when the process starts. CTranslate2
    ``dlopen``s ``libcublas.so.12`` by name the first time a GPU model runs,
    and glibc answers a by-name request from libraries already loaded, so
    loading them from this folder with ``RTLD_GLOBAL`` first is enough.
    ``main._preload_cuda_libraries`` does the same for the pip wheels.
    A library that fails to load is skipped, not fatal.
    """
    if not sys.platform.startswith("linux") or not os.path.isdir(path):
        return []
    import ctypes

    loaded = []
    for name in sorted(os.listdir(path)):
        if not (name.endswith(".so") or ".so." in name):
            continue
        try:
            handle = ctypes.CDLL(os.path.join(path, name), mode=ctypes.RTLD_GLOBAL)
        except OSError as exc:
            logger.debug(f"Could not preload {name}: {exc}")
            continue
        _SHARED_LIBRARY_HANDLES.append(handle)
        loaded.append(name)
    PRELOADED_LIBRARIES.extend(loaded)
    return loaded


def _activate_gpu_libraries(component_dir: str) -> Tuple[bool, str]:
    """Windows: register ``bin`` on the loader path. Linux: preload ``lib``."""
    if sys.platform.startswith("linux"):
        from services.components import gpu_runtime_available

        # CUDA from the pip wheels or the system is already loaded (main.py
        # preloads it first); a second copy of cuBLAS would only cost memory.
        if gpu_runtime_available():
            return True, ""
        lib_dir = os.path.join(component_dir, "lib")
        if not os.path.isdir(lib_dir):
            return False, "Its library folder is missing."
        if not preload_shared_libraries(lib_dir) or not gpu_runtime_available():
            return False, "Its CUDA libraries could not be loaded."
        return True, ""
    bin_dir = os.path.join(component_dir, "bin")
    if not os.path.isdir(bin_dir) or not register_dll_directory(bin_dir):
        return False, "Its library folder is missing."
    return True, ""


def activate_component(component_id: str) -> Tuple[bool, str]:
    """Put one installed component into use in this process.

    GPU Acceleration registers its ``bin`` directory on the Windows loader
    path, or preloads its ``lib`` directory on Linux. The meeting agent has
    no native ``bin`` tree — a completed install is already
    usable, so activation succeeds without ``os.add_dll_directory``. Also
    usable mid-session, right after an install. Never raises.

    """
    try:
        from services.components import (
            ComponentId,
            check_compatibility,
            component_dir,
            is_installed,
            read_manifest,
        )

        if not is_installed(component_id):
            return False, "The component is not installed."

        manifest = read_manifest(component_id)
        if manifest is None:
            return False, "The component manifest is missing or invalid."
        reason = check_compatibility(manifest)
        if reason:
            logger.warning(f"Component '{component_id}' is not usable: {reason}")
            return False, reason

        from services.local_asr.catalog import RUNTIME_IDS
        if component_id in RUNTIME_IDS:
            return True, ""
        if component_id == ComponentId.GPU_ACCEL:
            ok, reason = _activate_gpu_libraries(component_dir(component_id))
            if not ok:
                return False, reason
        else:
            bin_dir = os.path.join(component_dir(component_id), "bin")
            if os.path.isdir(bin_dir) and not register_dll_directory(bin_dir):
                return False, "Its library folder is missing."

        logger.info(
            f"Activated component '{component_id}' "
            f"version {manifest.get('version', 'unknown')}"
        )
        return True, ""
    except Exception as exc:
        logger.warning(f"Failed to activate component '{component_id}': {exc}")
        return False, str(exc)


def activate_components() -> ActivationReport:
    """Put every usable installed component on the native search path.

    Never raises. Each component is activated independently so one damaged
    payload cannot block the others or prevent the application from starting.
    """
    activated: List[str] = []
    rejected: List[Tuple[str, str]] = []

    try:
        from services.components import (
            available_component_ids, is_installed, prune_orphans,
        )
    except Exception:
        logger.debug("Component system unavailable", exc_info=True)
        return ActivationReport()

    try:
        prune_orphans()
    except Exception:
        logger.debug("Could not prune component orphans", exc_info=True)

    for component_id in available_component_ids():
        if not is_installed(component_id):
            continue
        ok, reason = activate_component(component_id)
        if ok:
            activated.append(component_id)
        else:
            rejected.append((component_id, reason))

    return ActivationReport(tuple(activated), tuple(rejected))
