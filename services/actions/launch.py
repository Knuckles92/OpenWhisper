"""Opening links and terminals for actions (stream C2 owns this module).

Words reach a link percent-encoded and a terminal as one argv element; they
never pass through a shell, cmd.exe or AppleScript source.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional, Sequence


class LaunchError(RuntimeError):
    """Opening failed; the message is safe to show."""


def open_uri(uri: str) -> None:
    """Hand ``uri`` to the app registered for its scheme; blocks briefly.

    Raises:
        LaunchError: Nothing could open it.
    """
    if not uri or any(ch in uri for ch in "\r\n\x00"):
        raise LaunchError("That link can't be opened.")
    try:
        if sys.platform == "win32":
            os.startfile(uri)  # type: ignore[attr-defined]
            return
        command = ["open", uri] if sys.platform == "darwin" else ["xdg-open", uri]
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=10, check=False,
        )
    except OSError as exc:
        raise LaunchError("No app on this computer opens that link.") from exc
    except subprocess.TimeoutExpired as exc:
        raise LaunchError("Opening the link took too long.") from exc
    if completed.returncode != 0:
        raise LaunchError("No app on this computer opens that link.")


def handler_available(scheme: str) -> Optional[bool]:
    """Whether an app is registered for ``scheme``; None when it can't be told."""
    return None


def open_in_terminal(argv: Sequence[str], *, cwd: str = "") -> None:
    """Run ``argv`` in a new terminal window.

    Raises:
        LaunchError: No terminal could be opened.
    """
    raise LaunchError("Opening a terminal isn't available yet.")
