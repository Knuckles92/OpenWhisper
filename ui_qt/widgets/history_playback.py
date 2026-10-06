"""Play and stop a History entry's recording, with a thin progress line.

The player (services/audio_player.py) knows nothing about Qt. This side
polls it on an owned timer while one of its recordings plays, so no audio
thread ever reaches a widget, and a card rebuilt by a History refresh picks
up the playback it shows.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QPoint, QRectF, QTimer, pyqtSignal
from PyQt6.QtGui import QPainter, QPainterPath
from PyQt6.QtWidgets import QLabel, QPushButton, QSizePolicy, QToolTip, QWidget

from services import audio_player
from services.desktop_session import use_omarchy_ui
from services.history_manager import history_manager
from ui_qt.utils.icons import tabler_icon
from ui_qt.utils.palette import current_palette, token_color

logger = logging.getLogger(__name__)

RECORDING_BUSY = "Playback is off while you're recording"
MEETING_BUSY = "Playback is off during a meeting"
#: Retention removed the file; the entry still says it had one.
RECORDING_REMOVED = "This recording was removed to save space"
NOT_ON_HOST = "The host didn't keep this recording"
CANT_PLAY = "This recording can't be played"

#: Where each host-kept entry's recording was fetched to this session, so a
#: card rebuilt by a refresh still knows its playback.
_fetched: dict[str, str] = {}


def _icon(name: str):
    return tabler_icon(f"{name}{current_palette().css('icon-suffix')}.svg")


def clock(seconds: float) -> str:
    """``0:04``, ``12:30``."""
    whole = max(0, int(seconds))
    return f"{whole // 60}:{whole % 60:02d}"


def busy_reason(widget: QWidget) -> str:
    """Why nothing should play now, from the main window ``widget`` lives in.

    Playback would leak into a recording or a meeting's capture. A dialog
    looks through its parent window.
    """
    window = widget.window() if widget is not None else None
    seen = set()
    while window is not None and id(window) not in seen:
        seen.add(id(window))
        if getattr(window, "is_recording", False) is True:
            return RECORDING_BUSY
        meeting = getattr(window, "meeting_is_active", None)
        try:
            if callable(meeting) and meeting():
                return MEETING_BUSY
        except Exception:
            logger.debug("Could not ask whether a meeting is running", exc_info=True)
        parent = window.parentWidget()
        window = parent.window() if parent is not None else None
    return ""


class PlaybackProgress(QWidget):
    """A thin line that fills as the recording plays."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(4)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._fraction = 0.0

    @property
    def fraction(self) -> float:
        return self._fraction

    def set_fraction(self, fraction: float) -> None:
        fraction = min(1.0, max(0.0, float(fraction)))
        if abs(fraction - self._fraction) >= 0.001 or fraction in (0.0, 1.0):
            self._fraction = fraction
            self.update()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect())
        radius = 1.0 if use_omarchy_ui() else rect.height() / 2
        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip)
        painter.fillRect(rect, token_color("overlay-rgb", 28))
        filled = QRectF(rect.left(), rect.top(), rect.width() * self._fraction, rect.height())
        painter.fillRect(filled, token_color("accent"))
        painter.end()


class PlaybackControl(QObject):
    """Play/Stop for one entry, driving a button, a progress line and a time label.

    Owned by the card or dialog that shows them, which lays the three out;
    the line and the label only show while this entry plays.
    """

    _remote_ready = pyqtSignal(str, str)
    #: Emitted with True when this entry starts playing, False when it stops.
    playing_changed = pyqtSignal(bool)
    POLL_MS = 50

    def __init__(
        self,
        entry,
        button: QPushButton,
        parent: QObject,
        *,
        busy: Optional[Callable[[], str]] = None,
    ):
        super().__init__(parent)
        self.button = button
        self.progress = PlaybackProgress()
        self.time_label = QLabel("")
        self.time_label.setObjectName("historyPlayTime")
        self._entry_id = entry.id
        self._stored_on = getattr(entry, "stored_on", None)
        self._busy = busy or (lambda: busy_reason(self.button))
        self._session = 0
        self._fetching = False
        self.note = ""
        self._path: Optional[str] = None
        self.available = False
        self._unavailable = ""
        if self._stored_on:
            self._path = _fetched.get(entry.id)
            self.available = bool(getattr(entry, "remote_audio", False))
            self._unavailable = NOT_ON_HOST
        elif entry.audio_file:
            self._path = history_manager.get_recording_path(entry.audio_file)
            self.available = bool(self._path)
            self._unavailable = RECORDING_REMOVED

        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self._sync)
        self._remote_ready.connect(self._on_remote_ready)
        button.clicked.connect(self.toggle)
        self._set_idle()
        current = audio_player.player() if self._path else None
        if current is not None and current.is_playing and current.path == self._path:
            self._session = current.session
            self._set_playing()

    @property
    def is_playing(self) -> bool:
        return bool(self._session) and audio_player.player().session == self._session

    def toggle(self) -> None:
        if self.is_playing:
            self.stop()
            return
        if not self.available or self._fetching:
            return
        reason = self._busy()
        if reason:
            self._say(reason)
            return
        if self._stored_on and not self._path:
            self._fetch()
            return
        self._play(self._path)

    def stop(self) -> None:
        """Stop this entry's playback, if it is the one playing."""
        if self.is_playing:
            audio_player.player().stop()
        self._set_idle()

    def _play(self, path: Optional[str]) -> None:
        player = audio_player.player()
        if not path or not player.play(path):
            self._say(CANT_PLAY)
            return
        self._session = player.session
        self._set_playing()

    def _fetch(self) -> None:
        self._fetching = True
        self.button.setEnabled(False)
        self.button.setText("Getting it…")
        entry_id = self._entry_id
        ready = self._remote_ready

        def fetch() -> None:
            from services.remote_records.sync import record_sync

            try:
                path = record_sync.audio_for(entry_id)
            except Exception as exc:
                logger.warning("Could not fetch a recording from the host: %s", exc)
                ready.emit("", str(exc) or type(exc).__name__)
                return
            ready.emit(path, "")

        threading.Thread(target=fetch, name="history-playback-fetch", daemon=True).start()

    def _on_remote_ready(self, path: str, error: str) -> None:
        self._fetching = False
        self.button.setEnabled(True)
        self._set_idle()
        if error or not path:
            self._say(f"Couldn't get the recording: {error}" if error else CANT_PLAY)
            return
        _fetched[self._entry_id] = path
        self._path = path
        reason = self._busy()
        if reason:
            self._say(reason)
            return
        self._play(path)

    def _say(self, message: str) -> None:
        self.note = message
        QToolTip.showText(
            self.button.mapToGlobal(QPoint(0, self.button.height())), message, self.button,
        )

    def _set_playing(self) -> None:
        self.button.setText("Stop")
        self.button.setIcon(_icon("player-stop-danger"))
        self.button.setToolTip("Stop playing")
        self.progress.show()
        self.time_label.show()
        self._sync()
        self._timer.start()
        self.playing_changed.emit(True)

    def _set_idle(self) -> None:
        was_playing = self._timer.isActive()
        self._timer.stop()
        self._session = 0
        if not self._fetching:
            self.button.setText("Play")
        self.button.setIcon(_icon("player-play-gray"))
        self.button.setEnabled(self.available and not self._fetching)
        self.button.setToolTip("Play the recording" if self.available else self._unavailable)
        self.progress.set_fraction(0.0)
        self.progress.hide()
        self.time_label.hide()
        if was_playing:
            self.playing_changed.emit(False)

    def _sync(self) -> None:
        player = audio_player.player()
        if not self._session or player.session != self._session:
            self._set_idle()
            return
        duration = player.duration
        position = min(player.position, duration)
        self.progress.set_fraction(position / duration if duration else 0.0)
        self.time_label.setText(f"{clock(position)} / {clock(duration)}")
