"""Which app has focus when a dictation starts, and the text near its caret.

Knowing the app is native and local. Text from other apps is opt-in, is
never logged or stored in history, and only ever reaches the AI cleanup
provider the user configured; never a speech engine or a remote host. A
slow, missing or failing read means "no context", never an error or a
delayed dictation.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class AppIdentity:
    app_id: str
    name: str
    pid: int | None = None
    window: str = ""
    #: Site or product derived from the window title; never the raw title.
    title_hint: str = ""
    #: One of OpenWhisper's own windows.
    is_self: bool = False
    platform: str = ""


@dataclass(frozen=True)
class TextContext:
    before: str = ""
    selected: str = ""
    after: str = ""
    #: ``before``/``after`` really are the text around the caret.
    caret_known: bool = False
    #: ``selected`` was read, so "" means nothing is selected rather than
    #: "unknown".
    selection_known: bool = False
    source: str = ""


@dataclass(frozen=True)
class FocusSnapshot:
    identity: AppIdentity | None = None
    text: TextContext | None = None


class ContextCaptureService:
    """Captures focus context off the caller's thread.

    Every method is safe from any thread and never raises. ``request``'s
    Future resolves within ``config.CONTEXT_CAPTURE_DEADLINE_S`` with
    whatever was known by then; Qt-thread callers only read it with
    ``timeout=0``.
    """

    def request(self, *, include_text: bool, include_selection: bool = False) -> Future:
        """Start a capture; the Future resolves to a FocusSnapshot."""
        raise NotImplementedError

    def current_identity(self) -> AppIdentity | None:
        """The focused app now, or None where it is only known asynchronously."""
        raise NotImplementedError

    def reread(self, identity: AppIdentity,
               callback: Callable[[Optional[TextContext]], None]) -> None:
        """Read ``identity``'s text again on the service thread, then call back."""
        raise NotImplementedError

    def shutdown(self) -> None:
        raise NotImplementedError


class NullCaptureService(ContextCaptureService):
    """Knows nothing: every snapshot is empty. Tests install this one."""

    def request(self, *, include_text: bool, include_selection: bool = False) -> Future:
        future: Future = Future()
        future.set_result(FocusSnapshot())
        return future

    def current_identity(self) -> AppIdentity | None:
        return None

    def reread(self, identity: AppIdentity,
               callback: Callable[[Optional[TextContext]], None]) -> None:
        callback(None)

    def shutdown(self) -> None:
        pass


_service: ContextCaptureService | None = None
_service_lock = threading.Lock()


def _create_service() -> ContextCaptureService:
    # No platform capture exists yet, so nothing about the focused app is known.
    return NullCaptureService()


def get_service() -> ContextCaptureService:
    """The process-wide capture service, created on first use."""
    global _service
    with _service_lock:
        if _service is None:
            _service = _create_service()
        return _service


def set_service(service: ContextCaptureService | None) -> None:
    """Install ``service``; None goes back to creating the default on next use."""
    global _service
    with _service_lock:
        _service = service


def shutdown_service() -> None:
    """Shut the service down if one was ever created, without creating one."""
    global _service
    with _service_lock:
        service, _service = _service, None
    if service is not None:
        service.shutdown()


def prompt_block(snapshot: FocusSnapshot | None) -> str:
    """The cleanup-prompt block describing the text around the caret, or ""."""
    return ""


def join_with_context(text: str, context: TextContext | None) -> str:
    """``text`` adjusted to continue what is already before the caret."""
    return text
