"""Open the desktop file manager on a file or a folder.

Every desktop spells "select this file" differently — Explorer wants
``/select,``, Finder wants ``open -R``, and Linux has no portable equivalent —
so Linux (and every failure) falls back to opening the containing folder.
Revealing a file is cosmetic, so callers get a bool and never an exception.
"""
import logging
import os
import subprocess
import sys

from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QDesktopServices

logger = logging.getLogger(__name__)


def reveal_in_file_manager(path: str) -> bool:
    """Show ``path`` in the file manager, selected where the platform allows."""
    if not path:
        return False

    target = os.path.abspath(path)

    if os.path.exists(target):
        try:
            if sys.platform == "win32":
                # A raw command line, not a list: Explorer only honors the
                # selection when the path is quoted *after* "/select,", which
                # list-form quoting cannot produce. No shell is involved, and
                # Explorer exits 1 even on success, so the code tells us nothing.
                subprocess.Popen(f'explorer /select,"{target}"')
                return True
            if sys.platform == "darwin":
                subprocess.run(["open", "-R", target], check=False)
                return True
        except Exception as e:
            logger.warning(f"Failed to reveal {target}: {e}")

    return _open_containing_folder(target)


def open_folder_in_file_manager(path: str) -> bool:
    """Open ``path`` itself so its contents are visible.

    Use this instead of :func:`reveal_in_file_manager` when the folder *is*
    the thing the user wants — selecting it would only highlight it inside
    its parent and leave the files one double-click away. A folder that is
    gone is reported, never quietly swapped for its parent.
    """
    if not path:
        return False
    folder = os.path.abspath(path)
    if not os.path.isdir(folder):
        logger.warning(f"No folder to open: {folder}")
        return False
    return QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


def _open_containing_folder(target: str) -> bool:
    """Open the folder holding ``target`` — the fallback when selecting fails."""
    folder = target if os.path.isdir(target) else os.path.dirname(target)
    if not folder or not os.path.isdir(folder):
        logger.warning(f"No folder to open for {target}")
        return False
    return QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
