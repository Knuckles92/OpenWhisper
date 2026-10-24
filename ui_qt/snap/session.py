"""One snap from hotkey to kept image: capture, overlay, outcome.

``start_snap`` grabs every screen before any of our UI appears, then opens
an overlay on each (on Hyprland, the focused screen alone), so the user can
select on whichever screen holds what they want. The session it returns
reports exactly one outcome (``kept``, ``dropped`` or ``failed``), never
from inside ``start_snap``, so callers connect after it returns.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from functools import partial
from typing import Protocol

from PyQt6.QtCore import (
    QCoreApplication,
    QEventLoop,
    QObject,
    QRect,
    QThread,
    QTimer,
    pyqtSignal,
)
from PyQt6 import sip
from PyQt6.QtGui import QCursor, QGuiApplication, QImage, QScreen
from PyQt6.QtWidgets import QWidget

from ui_qt.snap import capture, export, macos
from ui_qt.snap.overlay import SnapOverlay
from ui_qt.snap.support import snap_support

logger = logging.getLogger(__name__)

CAPTURE_FAILED = "Couldn't capture the screen. Try again in a moment."
NO_SCREEN = "There's no screen to capture."

#: Sessions still waiting on their outcome. Holding them here lets a caller
#: write ``start_snap().kept.connect(...)`` without keeping the session.
_live: set[SnapSession] = set()


class Parkable(Protocol):
    """A window that stays in sight above an open snap, out of the way."""

    def park(self, area: QRect) -> None:
        """Move into a corner of ``area`` (global coordinates), above the overlay."""

    def unpark(self) -> None:
        """Go back to where it was before ``park``."""


def _unpark(windows: list[Parkable]) -> None:
    for window in windows:
        if not (isinstance(window, QObject) and sip.isdeleted(window)):
            window.unpark()
    windows.clear()


class SnapSession(QObject):
    """A snap in progress. Emits exactly one of ``kept``, ``dropped`` or ``failed``.

    ``keep()`` and ``drop()`` act like Enter and Esc and emit before they
    return; ``keep()`` while the capture is still pending emits once the
    screen has been captured. After the outcome ``active`` is False and the
    overlays are closed and deleted; the session itself stays a valid object.

    One selection at a time: starting one on a screen clears any other.
    """

    #: The annotated crop in device pixels; its devicePixelRatio is the screen's.
    kept = pyqtSignal(QImage)
    #: The user cancelled: Esc, the Cancel button, or the overlay was closed.
    dropped = pyqtSignal()
    #: Capture failed or isn't supported here; a short user-facing reason.
    failed = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        # One per covered screen, the pointer's first.
        self._overlays: list[SnapOverlay] = []
        self._screens: list[QScreen] = []
        self._hint = ""
        self._hidden: list[QWidget] = []
        self._float_above: tuple[Parkable, ...] = ()
        self._parked: list[Parkable] = []
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
        self.destroyed.connect(partial(_unpark, self._parked))

    @property
    def active(self) -> bool:
        """True while the capture is pending or the overlays are open."""
        return not self._decided

    @property
    def _overlay(self) -> SnapOverlay | None:
        """Where a keep comes from: the selection's screen, else the pointer's."""
        if not self._overlays:
            return None
        for overlay in self._overlays:
            if overlay.selection is not None:
                return overlay
        pointed = QGuiApplication.screenAt(QCursor.pos())
        for overlay in self._overlays:
            if overlay._screen is pointed:
                return overlay
        return self._overlays[0]

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

    def _start(
        self, keep_out: Sequence[QWidget], hint: str, float_above: Sequence[Parkable]
    ) -> None:
        supported, reason = snap_support()
        if not supported:
            self._fail_soon(reason)
            return
        screens = capture.screens_to_cover()
        if not screens:
            self._fail_soon(NO_SCREEN)
            return
        self._screens, self._hint = screens, hint
        self._float_above = tuple(float_above)
        # Opening the overlay makes OpenWhisper active on macOS; whoever
        # had the focus gets it back at the end (``_finish``).
        macos.remember_focus()
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
            self._settle.start(capture.settle_ms(screens[0]))

    def _capture(self) -> None:
        if self._decided:
            return
        try:
            images = [capture.grab(screen) for screen in self._screens]
        finally:
            self._restore_windows()
        if images[0] is None:
            self._fail_soon(CAPTURE_FAILED)
        elif self._keep_when_captured:
            whole = export.render(images[0], export.logical_rect(images[0]))
            self._finish(lambda: self.kept.emit(whole))
        else:
            # Another screen that couldn't be captured is left uncovered.
            self._open([(s, i) for s, i in zip(self._screens, images) if i is not None])

    def _restore_windows(self) -> None:
        capture.restore_windows(self._hidden)
        self._hidden.clear()

    def _open(self, frozen: list[tuple[QScreen, QImage]]) -> None:
        for screen, image in frozen:
            overlay = SnapOverlay(image, screen, hint=self._hint)
            overlay.kept.connect(self._on_kept)
            overlay.dropped.connect(self._on_dropped)
            overlay.selecting.connect(partial(self._on_selecting, overlay))
            # An overlay outliving its session would cover the screen for good.
            self.destroyed.connect(overlay.deleteLater)
            self._overlays.append(overlay)
        # The pointer's screen opens last, so it has the focus for Enter and Esc.
        for overlay in reversed(self._overlays):
            overlay.present()
        area = self._screens[0].geometry()
        for window in self._float_above:
            window.park(area)
            self._parked.append(window)

    def _on_selecting(self, chosen: SnapOverlay) -> None:
        """The selection moved to ``chosen``'s screen: clear the rest, focus it."""
        for overlay in self._overlays:
            if overlay is not chosen and overlay.selection is not None:
                overlay.clear_selection()
        if not chosen.isActiveWindow():
            chosen.activateWindow()
            chosen.setFocus()

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
        overlays, self._overlays = self._overlays, []
        for overlay in overlays:
            overlay.hide()
        _unpark(self._parked)
        macos.return_focus()
        try:
            emit()
        finally:
            for overlay in overlays:
                overlay.deleteLater()
            _live.discard(self)


def start_snap(
    *,
    keep_out: Sequence[QWidget] = (),
    hint: str = "",
    float_above: Sequence[Parkable] = (),
    parent: QObject | None = None,
) -> SnapSession:
    """Freeze the screens and open an overlay on each (Qt thread only).

    Args:
        keep_out: Our windows that must not appear in the shot. Those kept
            out with ``keep_out_of_screenshots`` stay on screen; the rest are
            hidden for a couple of frames around the grab, then shown again.
        hint: Shown in the pill at the top, before "Enter to keep · Esc to cancel".
        float_above: Windows that stay in sight while the overlay is open,
            parked in a corner above it and put back once the snap ends.
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
    session._start(tuple(keep_out), hint, tuple(float_above))
    return session
