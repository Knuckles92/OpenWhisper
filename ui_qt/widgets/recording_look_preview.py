"""A live recording overlay inside Settings, so a look can be judged as it is set.

It plays a sample voice on a loop: two phrases, a pause and a breath of
silence, at the levels a desk microphone really reports, then the look's own
finish (Processing, Transcribing, the fade out) before it starts again.
"Try with my microphone" swaps in the user's own voice and stays recording;
the microphone opens only while that is on and closes when it is turned off
or the preview leaves the screen. Nothing is recorded: the take is thrown away.
"""
from __future__ import annotations

import math
import os
import random
import tempfile
from typing import Callable, List, Optional, Tuple

from PyQt6.QtCore import QElapsedTimer, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QSizePolicy, QVBoxLayout, QWidget

from ui_qt.overlays.waveform_overlay import WaveformOverlay
from ui_qt.widgets.buttons import Button, neutral_button
from ui_qt.widgets.wrapped_label import WrappedLabel

_TICK_MS = 33
#: One loop of the sample: the recording, then the finish, then a breath.
#: Each entry is when that stage starts, in seconds.
STAGES = (("recording", 0.0), ("processing", 4.8), ("transcribing", 6.2), ("done", 7.0))
LOOP_S = 7.6
_PREVIEW_PATH = os.path.join(tempfile.gettempdir(), "openwhisper_overlay_preview.wav")


def _sample_words(seed: int = 3) -> List[Tuple[float, float, float]]:
    """Words as ``(start, length, peak RMS)``: two phrases with a pause."""
    rng = random.Random(seed)
    words = []
    for start, end in ((0.5, 2.1), (2.9, 4.6)):
        t = start
        while t < end:
            length = rng.uniform(0.14, 0.32)
            words.append((t, length, rng.uniform(0.04, 0.16)))
            t += length + rng.uniform(0.03, 0.12)
    return words


_WORDS = _sample_words()


def sample_level(t: float) -> float:
    """The sample voice's RMS level ``t`` seconds into the loop."""
    t %= LOOP_S
    level = 0.0025 + 0.001 * math.sin(t * 37)
    for start, length, peak in _WORDS:
        if start <= t <= start + length:
            level = max(level, peak * math.sin(math.pi * (t - start) / length) ** 0.7)
    return level


class RecordingLookPreview(QWidget):
    """The real overlay, recording, beside a microphone toggle."""

    #: A level from the audio thread, delivered to the GUI thread.
    _heard = pyqtSignal(float)

    def __init__(self, recorder_factory: Optional[Callable[[], object]] = None,
                 microphone_name: Optional[Callable[[], str]] = None, parent=None):
        """Build the preview; nothing animates until it is shown.

        Args:
            recorder_factory: Makes an ``AudioRecorder`` for the microphone
                toggle; None hides the toggle.
            microphone_name: Names the microphone that will be used.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setObjectName("recordingLookPreview")
        self._recorder_factory = recorder_factory
        self._microphone_name = microphone_name
        self._recorder = None

        self.overlay = WaveformOverlay(self, preview=True)
        self.overlay.hide()
        stage = QHBoxLayout()
        stage.setContentsMargins(0, 6, 0, 6)
        stage.addStretch(1)
        stage.addWidget(self.overlay)
        stage.addStretch(1)
        # The overlay hides between loops; a strut keeps its room so nothing jumps.
        stage.addStrut(self.overlay.height())

        self.source_label = WrappedLabel("", parent=self)
        # Settings' secondary caption style.
        self.source_label.setObjectName("infoLabel")
        self.mic_button = neutral_button(Button("Try with my microphone", self))
        self.mic_button.setCheckable(True)
        self.mic_button.toggled.connect(self._on_mic_toggled)
        # Stacked, so the caption wraps at full width in a narrow window.
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(stage)
        layout.addWidget(self.source_label)
        layout.addWidget(self.mic_button, 0, Qt.AlignmentFlag.AlignLeft)
        # Only once parented: a parentless widget made visible becomes its
        # own window and takes activation from Settings.
        self.mic_button.setVisible(recorder_factory is not None)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        self._clock = QElapsedTimer()
        self._stage = "recording"
        self._timer = QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._heard.connect(self._on_heard)
        self._update_source_label()

    # -- the look ---------------------------------------------------------------
    def set_look(self, look: str) -> None:
        """Show ``look`` and replay the overlay's entry."""
        self.overlay.set_recording_look(look)
        if self._timer.isActive():
            self._restart()

    def set_label_parts(self, dot: bool, text: bool, clock: bool) -> None:
        """Show or hide the live dot, the "Recording" text and the clock."""
        self.overlay.set_label_parts(dot, text, clock)

    def _restart(self) -> None:
        self.overlay.hide()
        self.overlay.show_at_cursor(WaveformOverlay.STATE_RECORDING)
        self._clock.restart()
        self._stage = "recording"

    # -- the voice --------------------------------------------------------------
    def _tick(self) -> None:
        if self.mic_button.isChecked():
            return  # the microphone feeds the overlay as levels arrive
        elapsed = self._clock.elapsed() / 1000
        if elapsed >= LOOP_S:
            self._restart()
            elapsed = 0.0
        stage = [name for name, start in STAGES if elapsed >= start][-1]
        if stage != self._stage:
            self._stage = stage
            if stage == "processing":
                self.overlay.set_state(WaveformOverlay.STATE_PROCESSING)
            elif stage == "transcribing":
                self.overlay.set_state(WaveformOverlay.STATE_TRANSCRIBING)
            elif stage == "done":
                self.overlay.hide()  # fades out, as when the text lands
        if stage == "recording":
            self.overlay.update_audio_levels([sample_level(elapsed)] * 20)

    def _on_heard(self, level: float) -> None:
        if self.mic_button.isChecked():
            self.overlay.update_audio_levels([level] * 20)

    def _emit_heard(self, level: float) -> None:
        """Hand a level from the audio thread to the GUI thread."""
        try:
            self._heard.emit(level)
        except RuntimeError:
            pass  # the preview is gone; its recorder is being cleaned up

    def _on_mic_toggled(self, on: bool) -> None:
        if on and not self._open_microphone():
            self.mic_button.blockSignals(True)
            self.mic_button.setChecked(False)
            self.mic_button.blockSignals(False)
            return
        if not on:
            self._close_microphone()
        self.mic_button.setText("Stop microphone" if on else "Try with my microphone")
        self._update_source_label()
        if self._timer.isActive():
            self._restart()

    def _open_microphone(self) -> bool:
        try:
            self._recorder = self._recorder_factory()
            self._recorder.set_audio_level_callback(self._emit_heard)
            if self._recorder.start_recording():
                return True
            reason = getattr(self._recorder, "last_start_error", "") or ""
        except Exception as exc:  # a missing or busy device must not break Settings
            reason = str(exc)
        self._release_recorder()
        self.source_label.setText(
            f"Couldn't open the microphone: {reason}." if reason else "Couldn't open the microphone."
        )
        return False

    def _close_microphone(self) -> None:
        if self._recorder is not None:
            try:
                self._recorder.cancel_recording()
            except Exception:
                pass
        self._release_recorder()

    def _release_recorder(self) -> None:
        if self._recorder is not None:
            try:
                self._recorder.cleanup()
            except Exception:
                pass
            self._recorder = None

    def _update_source_label(self) -> None:
        if self.mic_button.isChecked():
            name = self._microphone_name() if self._microphone_name else ""
            text = f"Listening to {name}. Nothing is recorded." if name else "Listening to your microphone. Nothing is recorded."
        else:
            text = "Playing a sample voice."
        self.source_label.setText(text)

    # -- visibility -------------------------------------------------------------
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._restart()
        self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()  # first, so closing the microphone doesn't replay
        if self.mic_button.isChecked():
            self.mic_button.setChecked(False)  # closes the microphone
        self.overlay.hide()

    @staticmethod
    def preview_path() -> str:
        """Where the throwaway take spools while the microphone is on."""
        return _PREVIEW_PATH
