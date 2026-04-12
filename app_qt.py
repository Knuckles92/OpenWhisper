"""Thin compatibility entrypoint for the Qt application."""

import platform
import subprocess
import sys
import warnings

warnings.filterwarnings("ignore", message="pkg_resources is deprecated")


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


_patch_subprocess_for_windows()

from services.application_controller import ApplicationController
from ui_qt.bootstrap import main

__all__ = ["ApplicationController", "main"]


if __name__ == "__main__":
    sys.exit(main())
