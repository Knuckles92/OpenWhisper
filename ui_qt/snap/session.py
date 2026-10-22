"""One snap from hotkey to kept image: capture, overlay, outcome.

``start_snap`` grabs the screen under the pointer before any of our UI
appears, then opens the overlay on that screen. The session it returns
reports exactly one outcome (``kept``, ``dropped`` or ``failed``), never
from inside ``start_snap``, so callers connect after it returns.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from functools import partial

from PyQt6.QtCore import (
    QCoreApplication,
    QEventLoop,
    QObject,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import QImage, QScreen
from PyQt6.QtWidgets import QWidget

from ui_qt.snap import capture, export
from ui_qt.snap.overlay import SnapOverlay
from ui_qt.snap.support import snap_support

logger = logging.getLogger(__name__)

CAPTURE_FAILED = "Couldn't capture the screen. Try again in a moment."
NO_SCREEN = "There's no screen to capture."

#: Sessions still waiting on their outcome. Holding them here lets a caller
#: write ``start_snap().kept.connect(...)`` without keeping the session.
_live: set[SnapSession] = set()


class SnapSession(QObject):
    """A snap in progress. Emits exactly one of ``kept``, ``dropped`` or ``failed``.

    ``keep()`` and ``drop()`` act like Enter and Esc and emit before they
    return; ``keep()`` while the capture is still pending emits once the
    screen has been captured. After the outcome ``active`` is False and the
    overlay is closed and deleted; the session itself stays a valid object.
    """

    #: The annotated crop in device pixels; its devicePixelRatio is the screen's.
    kept = pyqtSignal(QImage)
    #: The user cancelled: Esc, the Cancel button, or the overlay was closed.
    dropped = pyqtSignal()
    #: Capture failed or isn't supported here; a short user-facing reason.
    failed = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._overlay: SnapOverlay | None = None
        self._screen: QScreen | None = None
        self._hint = ""
        self._hidden: list[QWidget] = []
        self._decided = False
        self._keep_when_captured = False
        self._failure = ""
        # Owned timers: they die with the session, never firing at a dead one.
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.timeout.connect(self._capture)
        self._notice = QTimer(self)
        self._notice.setSingleShot(True)
        self._notice.setInterval(0)
        self._notice.timeout.connect(self._emit_failure)
        # Should an owner delete the session mid-snap, nothing is left
        # hidden. Bound to the list, not the session, which is gone by then.
        self.destroyed.connect(partial(capture.restore_windows, self._hidden))

    @property
    def active(self) -> bool:
        """True while the capture is pending or the overlay is open."""
        return not self._decided

    def keep(self) -> None:
        """As Enter: keep the selection, or the whole screen when there is none."""
        if self._decided:
            return
        if self._overlay is not None:
            self._overlay.keep()
        else:
            self._keep_when_captured = True

    def drop(self) -> None:
        """As Esc (always the whole snap, even while a label is being typed)."""
        if self._decided:
            return
        if self._overlay is not None:
            self._overlay.drop()
            return
        self._settle.stop()
        self._restore_windows()
        self._finish(self.dropped.emit)

    # ---- capture ----

    def _start(self, keep_out: Sequence[QWidget], hint: str) -> None:
        supported, reason = snap_support()
        if not supported:
            self._fail_soon(reason)
            return
        screen = capture.screen_under_cursor()
        if screen is None:
            self._fail_soon(NO_SCREEN)
            return
        self._screen, self._hint = screen, hint
        self._hidden.extend(capture.hide_windows(keep_out))
        if not self._hidden:
            self._capture()
            return
        # Let the hide reach the window system, then give the compositor a
        # couple of frames to stop drawing those windows before grabbing.
        QCoreApplication.processEvents(
            QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents
        )
        if not self._decided:
            self._settle.start(capture.settle_ms(screen))

    def _capture(self) -> None:
        if self._decided:
            return
        try:
            image = capture.grab(self._screen)
        finally:
            self._restore_windows()
        if image is None:
            self._fail_soon(CAPTURE_FAILED)
        elif self._keep_when_captured:
            whole = export.render(image, export.logical_rect(image))
            self._finish(lambda: self.kept.emit(whole))
        else:
            self._open(image)

    def _restore_windows(self) -> None:
        capture.restore_windows(self._hidden)
        self._hidden.clear()

    def _open(self, image: QImage) -> None:
        overlay = SnapOverlay(image, self._screen, hint=self._hint)
        overlay.kept.connect(self._on_kept)
        overlay.dropped.connect(self._on_dropped)
        # An overlay outliving its session would cover the screen for good.
        self.destroyed.connect(overlay.deleteLater)
        self._overlay = overlay
        overlay.present()

    # ---- outcome ----

    def _on_kept(self, image: QImage) -> None:
        self._finish(lambda: self.kept.emit(image))

    def _on_dropped(self) -> None:
        self._finish(self.dropped.emit)

    def _fail_soon(self, reason: str) -> None:
        """Fail now, but report it from the event loop: never inside start_snap."""
        logger.info("Snap failed: %s", reason)
        self._decided = True
        self._failure = reason
        self._notice.start()

    def _emit_failure(self) -> None:
        try:
            self.failed.emit(self._failure)
        finally:
            _live.discard(self)

    def _finish(self, emit: Callable[[], None]) -> None:
        """Hide the overlay first: the live screen is back while callers handle it."""
        if self._decided:
            return
        self._decided = True
        overlay, self._overlay = self._overlay, None
        if overlay is not None:
            overlay.hide()
        try:
            emit()
        finally:
            if overlay is not None:
                overlay.deleteLater()
            _live.discard(self)


def start_snap(
    *,
    keep_out: Sequence[QWidget] = (),
    hint: str = "",
    parent: QObject | None = None,
) -> SnapSession:
    """Freeze the screen under the pointer and open the overlay on it (Qt thread only).

    Args:
        keep_out: Our windows that must not appear in the shot. Those kept
            out with ``keep_out_of_screenshots`` stay on screen; the rest are
            hidden for a couple of frames around the grab, then shown again.
        hint: Shown in the pill at the top, before "Enter to keep · Esc to cancel".
        parent: Optional owner of the returned session.

    Returns:
        The session; connect to its signals right away. One outcome follows,
        always after this function has returned.
    """
    app = QCoreApplication.instance()
    if app is None or QThread.currentThread() is not app.thread():
        raise RuntimeError("start_snap must be called on the Qt thread")
    session = SnapSession(parent)
    _live.add(session)
    session._start(tuple(keep_out), hint)
    return session
