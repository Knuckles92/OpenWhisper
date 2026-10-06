"""The capture service: identity on the caller's thread, text on its own.

Windows and macOS name the focused app synchronously inside ``request``
(native calls, well under a millisecond). Linux asks Hyprland or X11 on a
short-lived thread, because both can block. Text near the cursor is read on
one dedicated thread, because accessibility calls can hang on an
unresponsive app:

* every Future resolves by ``config.CONTEXT_CAPTURE_DEADLINE_S``, with the
  identity and whatever text was read by then;
* while a read is still running, new requests get identity only at once;
  they never queue behind it;
* a read stuck for ``WEDGED_AFTER_S`` abandons its thread. The app it was
  reading gets no more text reads this session, and a fresh thread takes
  over, at most ``MAX_WORKER_REPLACEMENTS`` times; after that text capture
  stays off for the session;
* an app that misses the deadline ``MAX_CONSECUTIVE_MISSES`` times in a row
  also gets no more text reads this session.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable, Optional

from config import config
from services.focus_context import (
    AppIdentity,
    ContextCaptureService,
    FocusSnapshot,
    NullCaptureService,
    TextContext,
    catalog,
)

logger = logging.getLogger(__name__)

WEDGED_AFTER_S = 2.0
MAX_WORKER_REPLACEMENTS = 3
MAX_CONSECUTIVE_MISSES = 3
_RECENT_APPS = 12


class _WindowsPlatform:
    name = "windows"
    sync_identity = True
    text_supported = True

    def identity(self) -> Optional[AppIdentity]:
        from services.focus_context._win import foreground_identity

        return foreground_identity()

    def text_reader(self):
        from services.focus_context._win_uia import UiaTextReader

        return UiaTextReader()


class _MacPlatform:
    name = "macos"
    sync_identity = True

    @property
    def text_supported(self) -> bool:
        from services.focus_context import _mac

        return _mac._AX_TEXT_ENABLED

    def identity(self) -> Optional[AppIdentity]:
        from services.focus_context._mac import frontmost_identity

        return frontmost_identity()

    def text_reader(self):
        from services.focus_context._mac import AxTextReader

        return AxTextReader()


class _LinuxPlatform:
    name = "linux"
    sync_identity = False
    text_supported = False

    def identity(self) -> Optional[AppIdentity]:
        from services.focus_context._linux import active_identity

        return active_identity()

    def text_reader(self):
        return None


def create_service() -> ContextCaptureService:
    if sys.platform == "win32":
        return CaptureService(_WindowsPlatform())
    if sys.platform == "darwin":
        return CaptureService(_MacPlatform())
    if sys.platform.startswith("linux"):
        return CaptureService(_LinuxPlatform())
    return NullCaptureService()


def _load_settings() -> dict:
    from services.settings import settings_manager

    return settings_manager.load_all_settings()


def _record_metrics(**values) -> None:
    from services.diagnostics import record_metrics

    record_metrics(**values)


def _settle(future: Future, snapshot: FocusSnapshot) -> bool:
    """Resolve ``future`` unless it already is; True when this call did."""
    try:
        future.set_result(snapshot)
        return True
    except Exception:
        return False


def _same_target(current: Optional[AppIdentity], expected: AppIdentity) -> bool:
    return current is not None and (current.app_id, current.pid, current.window) == (
        expected.app_id, expected.pid, expected.window
    )


@dataclass
class _Read:
    identity: AppIdentity
    include_text: bool
    include_selection: bool
    future: Future
    started: float
    timer: Optional[threading.Timer] = field(default=None, repr=False)


@dataclass
class _Reread:
    identity: AppIdentity
    callback: Callable[[Optional[TextContext]], None]


class _TextWorker:
    """The thread that owns one text reader (and its COM apartment)."""

    def __init__(self, service: "CaptureService"):
        self._service = service
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        #: Set by the service under its lock; None while idle.
        self.busy_since: Optional[float] = None
        self.task_app = ""
        self.abandoned = False
        self._thread = threading.Thread(target=self._run, name="focus-context-text", daemon=True)
        self._thread.start()

    def start(self, task) -> None:
        self.busy_since = time.monotonic()
        self.task_app = task.identity.app_id
        self._queue.put(task)

    def stop(self) -> None:
        self._queue.put(None)

    def _run(self) -> None:
        reader = self._service._open_reader()
        try:
            while True:
                task = self._queue.get()
                if task is None:
                    return
                try:
                    self._service._execute(task, reader)
                except Exception as exc:
                    logger.debug("Text capture task failed (%s)", type(exc).__name__)
                finally:
                    self._service._finished(self)
                if self.abandoned:
                    return
        finally:
            if reader is not None:
                try:
                    reader.close()
                except Exception:
                    logger.debug("Text reader did not close cleanly", exc_info=True)


class CaptureService(ContextCaptureService):
    def __init__(
        self,
        platform,
        *,
        deadline_s: Optional[float] = None,
        wedged_after_s: float = WEDGED_AFTER_S,
        settings: Callable[[], dict] = _load_settings,
        metrics: Callable[..., None] = _record_metrics,
    ):
        self._platform = platform
        self._deadline_s = (
            config.CONTEXT_CAPTURE_DEADLINE_S if deadline_s is None else deadline_s
        )
        self._wedged_after_s = wedged_after_s
        self._settings = settings
        self._metrics = metrics
        self._lock = threading.Lock()
        self._worker: Optional[_TextWorker] = None
        self._replacements = 0
        self._text_off = False
        self._disabled: set[str] = set()
        self._misses: dict[str, int] = {}
        self._recent: list[AppIdentity] = []
        self._closed = False

    def request(self, *, include_text: bool, include_selection: bool = False) -> Future:
        started = time.monotonic()
        future: Future = Future()
        try:
            if self._closed:
                _settle(future, FocusSnapshot())
            elif self._platform.sync_identity:
                self._continue(future, self._identity(), include_text, include_selection, started)
            else:
                self._identify_async(future, include_text, include_selection, started)
        except Exception:
            logger.debug("Focus capture failed", exc_info=True)
            _settle(future, FocusSnapshot())
        return future

    def current_identity(self) -> Optional[AppIdentity]:
        if self._closed or not self._platform.sync_identity:
            return None
        return self._identity()

    def reread(self, identity: AppIdentity,
               callback: Callable[[Optional[TextContext]], None]) -> None:
        try:
            if (
                self._closed
                or not self._platform.sync_identity
                or not self._text_allowed(identity)
                or not self._submit(_Reread(identity, callback))
            ):
                self._deliver(callback, None)
        except Exception:
            logger.debug("Text re-read failed", exc_info=True)
            self._deliver(callback, None)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            worker, self._worker = self._worker, None
        if worker is not None:
            worker.stop()

    def recent_apps(self) -> tuple[AppIdentity, ...]:
        with self._lock:
            return tuple(self._recent)


    def _identity(self) -> Optional[AppIdentity]:
        try:
            return self._platform.identity()
        except Exception:
            logger.debug("Focused app unavailable", exc_info=True)
            return None

    def _identify_async(self, future: Future, include_text: bool,
                        include_selection: bool, started: float) -> None:
        self._start_timer(self._deadline_s, _settle, future, FocusSnapshot())

        def identify() -> None:
            identity = self._identity()
            if not future.done():
                self._continue(future, identity, include_text, include_selection, started)

        threading.Thread(target=identify, name="focus-context-identity", daemon=True).start()

    def _continue(self, future: Future, identity: Optional[AppIdentity],
                  include_text: bool, include_selection: bool, started: float) -> None:
        self._remember(identity)
        if not (include_text or include_selection) or not self._text_allowed(identity):
            _settle(future, FocusSnapshot(identity))
            return
        task = _Read(identity, include_text, include_selection, future, started)
        remaining = self._deadline_s - (time.monotonic() - started)
        task.timer = self._start_timer(max(0.0, remaining), self._expire, task)
        if not self._submit(task):
            task.timer.cancel()
            _settle(future, FocusSnapshot(identity))

    @staticmethod
    def _start_timer(delay: float, function, *args) -> threading.Timer:
        timer = threading.Timer(delay, function, args=args)
        timer.daemon = True
        timer.start()
        return timer

    def _text_allowed(self, identity: Optional[AppIdentity]) -> bool:
        if identity is None or identity.is_self or not identity.app_id:
            return False
        if not self._platform.text_supported or catalog.is_terminal(identity):
            return False
        with self._lock:
            return not self._text_off and identity.app_id not in self._disabled

    def _submit(self, task) -> bool:
        """Hand ``task`` to the text thread; False when it cannot take it now."""
        with self._lock:
            if self._closed or self._text_off:
                return False
            worker = self._worker
            if worker is not None and worker.busy_since is not None:
                if time.monotonic() - worker.busy_since < self._wedged_after_s:
                    return False
                self._abandon(worker)
                worker = None
                if self._text_off or task.identity.app_id in self._disabled:
                    return False
            if worker is None:
                worker = self._worker = _TextWorker(self)
            worker.start(task)
            return True

    def _abandon(self, worker: _TextWorker) -> None:
        # Called with the lock held. The stuck call cannot be cancelled; its
        # daemon thread exits if the call ever returns.
        worker.abandoned = True
        worker.stop()
        if worker.task_app:
            self._disabled.add(worker.task_app)
        self._worker = None
        self._replacements += 1
        if self._replacements > MAX_WORKER_REPLACEMENTS:
            self._text_off = True
        logger.warning(
            "Text near the cursor stopped answering in %s; not reading it there again "
            "this session%s", worker.task_app or "an app",
            " (text capture is now off)" if self._text_off else "",
        )

    def _expire(self, task: _Read) -> None:
        if _settle(task.future, FocusSnapshot(task.identity)):
            self._note_miss(task.identity.app_id)
            self._record(
                context_capture_ms=round(self._deadline_s * 1000, 1),
                context_capture_timeouts=1,
            )

    def _record(self, **values) -> None:
        try:
            self._metrics(**values)
        except Exception:
            logger.debug("Could not record capture metrics", exc_info=True)

    def _note_miss(self, app_id: str) -> None:
        with self._lock:
            misses = self._misses.get(app_id, 0) + 1
            self._misses[app_id] = misses
            if misses >= MAX_CONSECUTIVE_MISSES and app_id not in self._disabled:
                self._disabled.add(app_id)
                logger.info("Text near the cursor is too slow in %s; not reading it "
                            "there again this session", app_id)

    def _remember(self, identity: Optional[AppIdentity]) -> None:
        if identity is None or identity.is_self:
            return
        key = (identity.app_id, identity.title_hint)
        with self._lock:
            self._recent = [identity] + [
                seen for seen in self._recent if (seen.app_id, seen.title_hint) != key
            ][: _RECENT_APPS - 1]


    def _open_reader(self):
        try:
            return self._platform.text_reader()
        except Exception as exc:
            with self._lock:
                self._text_off = True
            logger.warning("Text near the cursor is unavailable on this computer (%s)",
                           type(exc).__name__)
            return None

    def _finished(self, worker: _TextWorker) -> None:
        with self._lock:
            worker.busy_since = None
            worker.task_app = ""

    def _excluded(self, identity: AppIdentity) -> bool:
        try:
            values = self._settings().get("app_context_excluded_apps")
        except Exception:
            logger.debug("Excluded apps unavailable", exc_info=True)
            return True
        return any(
            catalog.matches(identity, value)
            for value in (values if isinstance(values, list) else ())
            if isinstance(value, str)
        )

    def _read(self, reader, identity: AppIdentity, *, include_text: bool,
              include_selection: bool) -> Optional[TextContext]:
        if reader is None or self._excluded(identity):
            return None
        try:
            return reader.read(identity, include_text=include_text,
                               include_selection=include_selection)
        except Exception as exc:
            # Exception text could quote the app's content; the type cannot.
            logger.debug("Reading text near the cursor failed (%s)", type(exc).__name__)
            return None

    def _execute(self, task, reader) -> None:
        if isinstance(task, _Reread):
            context = None
            if _same_target(self._identity(), task.identity):
                context = self._read(reader, task.identity, include_text=True,
                                     include_selection=True)
            self._deliver(task.callback, context)
            return
        context = self._read(reader, task.identity, include_text=task.include_text,
                             include_selection=task.include_selection)
        if task.timer is not None:
            task.timer.cancel()
        if _settle(task.future, FocusSnapshot(task.identity, context)):
            with self._lock:
                self._misses.pop(task.identity.app_id, None)
            self._record(
                context_capture_ms=round((time.monotonic() - task.started) * 1000, 1),
                context_capture_timeouts=0,
            )

    @staticmethod
    def _deliver(callback, context: Optional[TextContext]) -> None:
        try:
            callback(context)
        except Exception:
            logger.debug("Text re-read callback failed", exc_info=True)
