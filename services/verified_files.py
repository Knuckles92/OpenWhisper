"""File digests, free-space checks and lock retries.

Shared by the app updater (``services.app_update``), the native update helper
(``services.app_update_apply``) and optional components
(``services.components``). Stdlib-only, like ``services.update_contract``: the
windowless updater executable imports this module, so it must not import
networking, Qt or settings code. Downloads live in
``services.verified_download``, which the helper never needs.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import stat
import time
from typing import Callable, Final, Optional, Tuple, TypeVar

logger = logging.getLogger(__name__)

CHUNK_BYTES: Final[int] = 1 << 20
# Free space asked for beyond the bytes an operation will write.
SPACE_MARGIN: Final[float] = 1.15
# Access denied, sharing violation, lock violation.
LOCK_WINERRORS: Final[frozenset] = frozenset({5, 32, 33})

Result = TypeVar("Result")


class NotARegularFile(Exception):
    """A path that must be one ordinary file is a link, a directory or a device."""


def sha256_file(path: str, *, regular_only: bool = False) -> str:
    """Return the lowercase hex SHA-256 of the file at ``path``.

    ``regular_only`` opens without following a final symlink and raises
    :class:`NotARegularFile` for anything but a single-link regular file, so
    a planted link cannot redirect what gets verified.
    """
    if not regular_only:
        with open(path, "rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise NotARegularFile(path)
        return hashlib.file_digest(handle, "sha256").hexdigest()


def free_space_shortfall(
    path: str,
    required_bytes: int,
    margin: float = SPACE_MARGIN,
) -> Optional[Tuple[int, int]]:
    """Return ``(needed, free)`` bytes when ``path``'s drive is short, else None.

    ``needed`` is ``required_bytes`` plus ``margin``. ``path`` is created
    first so the check can run before anything has been written there.
    """
    if required_bytes <= 0:
        return None
    os.makedirs(path, exist_ok=True)
    free = shutil.disk_usage(path).free
    needed = int(required_bytes * margin)
    return (needed, free) if free < needed else None


def is_windows_lock_error(exc: OSError) -> bool:
    """Whether Windows refused a file operation only because of a lock."""
    return getattr(exc, "winerror", None) in LOCK_WINERRORS


def retry_while_locked(
    what: str,
    action: Callable[[], Result],
    *,
    timeout_s: float,
    poll_s: float = 0.25,
    locked: Callable[[OSError], bool] = is_windows_lock_error,
    wait: Callable[[float], object] = time.sleep,
) -> Result:
    """Run ``action``, retrying for up to ``timeout_s`` while ``locked`` says so.

    Antimalware scanners open files they have never seen, and a process that
    has just exited keeps its image mapped for a moment; both surface as a
    brief sharing violation or access denial. Any other error, or a lock that
    outlasts ``timeout_s``, is raised. ``wait`` sleeps between attempts; pass
    a cancel event's ``wait`` to wake early on cancel.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            return action()
        except OSError as exc:
            if not locked(exc) or time.monotonic() >= deadline:
                raise
            logger.info("%s is locked (%s); retrying", what, exc)
            wait(poll_s)
