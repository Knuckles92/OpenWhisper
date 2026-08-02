"""Activate installed components at process start.

Must run before anything that depends on a component is imported, and before
Qt is loaded. Called from ``app_qt.py``.

Currently only ``gpu-accel`` exists, which contributes DLLs and never Python
packages, so activation is purely a matter of making its ``bin`` directory
visible to two different loaders. Nothing here ever raises: a damaged
component must degrade to "GPU unavailable", never prevent startup.
"""

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
    rejected: Tuple[Tuple[str, str], ...] = ()  # (component_id, reason)


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

    Args:
        path: Directory containing DLLs.

    Returns:
        True when the directory was registered.
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


def activate_components() -> ActivationReport:
    """Put every usable installed component on the native search path.

    Never raises. Each component is activated independently so one damaged
    payload cannot block the others or prevent the application from starting.
    """
    activated: List[str] = []
    rejected: List[Tuple[str, str]] = []

    try:
        from services.components import (
            available_component_ids, check_compatibility, component_dir,
            is_installed, prune_orphans, read_manifest,
        )
    except Exception:
        logger.debug("Component system unavailable", exc_info=True)
        return ActivationReport()

    try:
        prune_orphans()
    except Exception:
        logger.debug("Could not prune component orphans", exc_info=True)

    for component_id in available_component_ids():
        try:
            if not is_installed(component_id):
                continue

            manifest = read_manifest(component_id) or {}
            reason = check_compatibility(manifest)
            if reason:
                logger.warning(f"Component '{component_id}' is not usable: {reason}")
                rejected.append((component_id, reason))
                continue

            bin_dir = os.path.join(component_dir(component_id), "bin")
            if register_dll_directory(bin_dir):
                activated.append(component_id)
                logger.info(
                    f"Activated component '{component_id}' "
                    f"version {manifest.get('version', 'unknown')}"
                )
            else:
                rejected.append((component_id, "Its library folder is missing."))
        except Exception as exc:
            logger.warning(f"Failed to activate component '{component_id}': {exc}")
            rejected.append((component_id, str(exc)))

    return ActivationReport(tuple(activated), tuple(rejected))
