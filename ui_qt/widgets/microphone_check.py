"""A short microphone test: a live level bar and a plain verdict.

The microphone opens only while the test runs and closes on Stop, after
``LISTEN_MS``, or when the widget leaves the screen. Nothing is kept: the take
is cancelled.
"""
from __future__ import annotations

from typing import Callable

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QProgressBar, QVBoxLayout, QWidget

from ui_qt.utils.restyle import set_style_property
from ui_qt.widgets.wrapped_label import WrappedLabel

LISTEN_MS = 15_000
#: How long silence lasts before the verdict says nothing was heard.
QUIET_MS = 4_000
#: RMS levels: a quiet room reports about 0.003, speech at a desk 0.04 to 0.16.
_FLOOR = 0.004
HEARD = 0.02
_FULL = 0.2


class MicrophoneCheck(QWidget):
    """Opens the saved microphone and shows whether it hears anything."""

    running_changed = pyqtSignal(bool)
    #: A level from the audio thread, delivered to the GUI thread.
    _level = pyqtSignal(float)

    def __init__(self, recorder_factory: Callable[[], object], parent=None):
        """Build the test; the microphone stays closed until :meth:`start`.

        Args:
            recorder_factory: Makes an ``AudioRecorder`` on the saved microphone.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self._recorder_factory = recorder_factory
        self._recorder = None
        self._heard = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        self.bar = QProgressBar()
        self.bar.setObjectName("microphoneCheckBar")
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(8)
        self.bar.setAccessibleName("Microphone level")
        layout.addWidget(self.bar)
        self.status = WrappedLabel("")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setObjectName("guidedDetail")
        layout.addWidget(self.status)
        self._stop_timer = QTimer(self)
        self._stop_timer.setSingleShot(True)
        self._stop_timer.setInterval(LISTEN_MS)
        self._stop_timer.timeout.connect(self.stop)
        self._quiet_timer = QTimer(self)
        self._quiet_timer.setSingleShot(True)
        self._quiet_timer.setInterval(QUIET_MS)
        self._quiet_timer.timeout.connect(self._on_quiet)
        self._level.connect(self._on_level)

    @property
    def running(self) -> bool:
        return self._recorder is not None

    def start(self) -> bool:
        """Open the microphone. False, with the reason shown, when it can't."""
        self.stop()
        self._heard = False
        self.bar.setValue(0)
        try:
            recorder = self._recorder_factory()
            recorder.set_audio_level_callback(self._emit_level)
            self._recorder = recorder
            started = recorder.start_recording()
            reason = "" if started else getattr(recorder, "last_start_error", "") or ""
        except Exception as exc:  # a missing or busy device must not break setup
            started, reason = False, str(exc)
        if not started:
            self._release()
            self._say(
                f"Couldn't open the microphone: {reason}." if reason
                else "Couldn't open the microphone. Pick another one, or check that "
                "OpenWhisper is allowed to use it.",
                "attention",
            )
            return False
        self._say("Listening. Say something and watch the bar. Nothing is recorded.")
        self._stop_timer.start()
        self._quiet_timer.start()
        self.running_changed.emit(True)
        return True

    def stop(self) -> None:
        """Close the microphone, keeping the verdict on screen."""
        if self._recorder is None:
            return
        self._stop_timer.stop()
        self._quiet_timer.stop()
        try:
            self._recorder.cancel_recording()
        except Exception:
            pass
        self._release()
        self.bar.setValue(0)
        if not self._heard:
            self._say(
                "Didn't hear anything. Check that the microphone isn't muted, "
                "or pick another one.",
                "attention",
            )
        self.running_changed.emit(False)

    def _release(self) -> None:
        recorder, self._recorder = self._recorder, None
        if recorder is not None:
            try:
                recorder.cleanup()
            except Exception:
                pass

    def _say(self, text: str, tone: str = "") -> None:
        self.status.setText(text)
        set_style_property(self.status, "tone", tone)

    def _emit_level(self, level: float) -> None:
        try:
            self._level.emit(level)
        except RuntimeError:
            pass  # the widget is gone; its recorder is being cleaned up

    def _on_level(self, level: float) -> None:
        if self._recorder is None:
            return
        shown = max(0.0, level - _FLOOR) / (_FULL - _FLOOR)
        self.bar.setValue(round(100 * min(1.0, shown) ** 0.5))
        if level >= HEARD and not self._heard:
            self._heard = True
            self._quiet_timer.stop()
            self._say("Hearing you. This microphone works.", "ready")

    def _on_quiet(self) -> None:
        if self._recorder is not None and not self._heard:
            self._say(
                "Not hearing anything yet. Check that the microphone isn't muted, "
                "or pick another one.",
                "attention",
            )

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.stop()
