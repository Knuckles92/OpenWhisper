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
    in ``app_qt.py`` for source installs.

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


def activate_component(component_id: str) -> Tuple[bool, str]:
    """Put one installed component into use in this process.

    GPU Acceleration registers its ``bin`` directory on the Windows loader
    path. Meeting-agent and speaker-id have no native ``bin`` tree — a
    completed install is already usable, so activation succeeds without
    ``os.add_dll_directory``. Also usable mid-session, right after an
    install. Never raises.

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

        manifest = read_manifest(component_id) or {}
        reason = check_compatibility(manifest)
        if reason:
            logger.warning(f"Component '{component_id}' is not usable: {reason}")
            return False, reason

        bin_dir = os.path.join(component_dir(component_id), "bin")
        if os.path.isdir(bin_dir):
            if not register_dll_directory(bin_dir):
                return False, "Its library folder is missing."
        elif component_id == ComponentId.GPU_ACCEL:
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
