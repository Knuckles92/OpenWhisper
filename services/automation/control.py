"""What the live API and MCP may ask the running app to do.

The desktop app registers an ``AppControl`` at startup
(``services/automation/app_control.py``); the in-app listener calls it from
its own thread. Every method may be called from any thread and returns within
about two seconds.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional, Protocol

#: ``ControlError.code`` values.
BUSY = "busy"
NOT_FOUND = "not_found"
UNAVAILABLE = "unavailable"
REFUSED = "refused"


class ControlError(Exception):
    """A refusal the API reports as-is; the message names no internals."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class LiveState:
    recording: bool
    busy: bool
    meeting: bool
    mode: str
    action_id: str
    job_id: str
    version: str

    def to_dict(self) -> dict:
        return {
            "recording": self.recording,
            "busy": self.busy,
            "meeting": self.meeting,
            "mode": self.mode,
            "action_id": self.action_id or None,
            "job_id": self.job_id or None,
            "version": self.version,
        }


class AppControl(Protocol):
    def state(self) -> LiveState: ...

    def list_actions(self) -> list[dict]: ...

    def run_action(self, action_id: str, text: str, *, source: str) -> str: ...

    def start_recording(self, *, action_id: str = "", source: str) -> str: ...

    def stop_recording(self) -> None: ...

    def cancel(self) -> None: ...

    def notify(self, text: str, *, title: str = "") -> None: ...


_control: Optional[AppControl] = None
_lock = threading.Lock()


def set_app_control(control: Optional[AppControl]) -> None:
    global _control
    with _lock:
        _control = control


def app_control() -> Optional[AppControl]:
    with _lock:
        return _control


def require_app_control() -> AppControl:
    control = app_control()
    if control is None:
        raise ControlError(UNAVAILABLE, "OpenWhisper isn't ready for this yet.")
    return control
