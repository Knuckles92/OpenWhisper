"""Whether the code on disk is still the code this process started from.

OpenWhisper imports most features the first time they are used, so a copy
updated underneath a running app (a pull or merge into a git checkout, or a
package manager replacing an installed build) mixes modules already loaded
with newer ones read from disk. That fails in confusing ways, such as an
``ImportError`` for a name the older module never had, and only a restart
puts the two back in step.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from config import bundle_root, is_frozen

_at_launch: Optional[str] = None


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except (OSError, ValueError):
        return ""


def _git_head(git_dir: str) -> Optional[str]:
    """The commit a checkout's HEAD names, read from the files git keeps it in.

    No ``git`` process: this runs on the UI thread every few seconds.
    """
    head = _read(os.path.join(git_dir, "HEAD"))
    if not head.startswith("ref: "):
        return head or None  # Detached: HEAD holds the commit itself.
    ref = head[len("ref: "):]
    commit = _read(os.path.join(git_dir, *ref.split("/")))
    if commit:
        return commit
    # Packed refs share one file, a "<commit> <ref>" line each.
    for line in _read(os.path.join(git_dir, "packed-refs")).splitlines():
        commit, _, name = line.partition(" ")
        if name == ref:
            return commit
    return None


def code_identity() -> Optional[str]:
    """What the code on disk is now, or None where that can't be told.

    For an installed build, the program file, which an upgrade replaces; for
    a git checkout, the checked-out commit. Uncommitted edits don't count:
    they are work in progress, not an update.
    """
    if is_frozen():
        try:
            info = os.stat(sys.executable)
        except OSError:
            return None
        return f"{info.st_ino}:{info.st_size}:{info.st_mtime_ns}"
    git_dir = os.path.join(bundle_root(), ".git")
    if os.path.isdir(git_dir):
        return _git_head(git_dir)
    return None


def remember_launch() -> None:
    """Record the code on disk now as the version this process runs."""
    global _at_launch
    _at_launch = code_identity()


def changed_since_launch() -> bool:
    """True once the code on disk is no longer what this process started from."""
    if _at_launch is None:
        return False
    now = code_identity()
    return now is not None and now != _at_launch
