"""Inline progress for a transcription the user started inside the window.

The floating waveform overlay exists for hotkey-driven dictation, where the
user is looking at some other application and the cursor is the only place
feedback can go. A file dropped onto the Upload File tab is different: the
window is in front and the file card is where the user is looking, so the job
reports its stages there and the overlay stays out of it.
"""
from __future__ import annotations

import time
from enum import Enum
from typing import Final, Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ui_qt.overlay_state import OverlayState
from ui_qt.widgets.animated_progress_bar import AnimatedProgressBar
from ui_qt.widgets.eliding_label import ElidingLabel


class ProgressStage(Enum):
    PREPARING = "preparing"
    SPLITTING = "splitting"
    TRANSCRIBING = "transcribing"
    CLEANING = "cleaning"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_STAGES: Final[frozenset[ProgressStage]] = frozenset(
    {ProgressStage.DONE, ProgressStage.FAILED, ProgressStage.CANCELED}
)

_STAGE_TITLES: Final[dict[ProgressStage, str]] = {
    ProgressStage.PREPARING: "Preparing audio",
    ProgressStage.SPLITTING: "Splitting large file",
    ProgressStage.TRANSCRIBING: "Transcribing",
    ProgressStage.CLEANING: "Cleaning up",
    ProgressStage.DONE: "Done",
    ProgressStage.FAILED: "Failed",
    ProgressStage.CANCELED: "Canceled",
}

_OVERLAY_TO_STAGE: Final[dict[OverlayState, ProgressStage]] = {
    OverlayState.PROCESSING: ProgressStage.PREPARING,
    OverlayState.TRANSCRIBING: ProgressStage.TRANSCRIBING,
    OverlayState.CLEANING: ProgressStage.CLEANING,
    OverlayState.CANCELING: ProgressStage.CANCELED,
}

#: Which step of the stepper each in-flight stage lights up.
_STAGE_STEP_INDEX: Final[dict[ProgressStage, int]] = {
    ProgressStage.PREPARING: 0,
    ProgressStage.SPLITTING: 0,
    ProgressStage.TRANSCRIBING: 1,
    ProgressStage.CLEANING: 2,
}

_STEP_TITLES: Final[tuple[str, str, str]] = ("Prepare", "Transcribe", "Clean up")

_ELAPSED_TICK_MS: Final[int] = 1000


def stage_for_overlay_state(state: OverlayState) -> Optional[ProgressStage]:
    """The inline stage an overlay state maps to, or None when it has none."""
    return _OVERLAY_TO_STAGE.get(state)


def format_elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


class _StepChip(QWidget):
    """One step of the stepper: a dot and a title, both colored by state."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.dot = QLabel()
        self.dot.setObjectName("uploadStepDot")
        self.dot.setFixedSize(8, 8)
        layout.addWidget(self.dot)

        self.title = QLabel(title)
        self.title.setObjectName("uploadStepTitle")
        layout.addWidget(self.title)

        self._state = ""
        self.set_state("pending")

    @property
    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        for widget in (self.dot, self.title):
            widget.setProperty("stepState", state)
            widget.style().unpolish(widget)
            widget.style().polish(widget)


class TranscriptionProgressPanel(QFrame):
    """Stage title, sweeping bar, and a three-step stepper for one job.

    The bar sweeps while work is in flight because none of the backends report
    a fraction; it fills on completion so the end reads as an end. The panel
    keeps its own clock because the stats widget only appears afterwards, and a
    CPU transcription of a long file can run for minutes with nothing else on
    screen changing.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("uploadProgressPanel")
        self._stage: Optional[ProgressStage] = None
        self._started_at: Optional[float] = None
        self._with_cleanup = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)

        self.title_label = QLabel()
        self.title_label.setObjectName("uploadProgressTitle")
        header.addWidget(self.title_label, stretch=1)

        self.elapsed_label = QLabel("0:00")
        self.elapsed_label.setObjectName("uploadProgressElapsed")
        self.elapsed_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self.elapsed_label)
        layout.addLayout(header)

        self.bar = AnimatedProgressBar(bar_height=6)
        layout.addWidget(self.bar)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)

        self.steps: list[_StepChip] = []
        self._connectors: list[QFrame] = []
        for index, title in enumerate(_STEP_TITLES):
            if index:
                connector = QFrame()
                connector.setObjectName("uploadStepConnector")
                connector.setFixedSize(14, 1)
                footer.addWidget(connector)
                self._connectors.append(connector)
            chip = _StepChip(title)
            footer.addWidget(chip)
            self.steps.append(chip)

        footer.addSpacing(8)
        self.detail_label = ElidingLabel()
        self.detail_label.setObjectName("uploadProgressDetail")
        self.detail_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        footer.addWidget(self.detail_label, stretch=1)
        layout.addLayout(footer)

        self._clock = QTimer(self)
        self._clock.setInterval(_ELAPSED_TICK_MS)
        self._clock.timeout.connect(self._tick)

    @property
    def stage(self) -> Optional[ProgressStage]:
        return self._stage

    @property
    def is_running(self) -> bool:
        """True from ``start`` until a terminal stage is reached."""
        return self._stage is not None and self._stage not in TERMINAL_STAGES

    def start(self, with_cleanup: bool) -> None:
        """Begin a job at the preparing stage.

        Args:
            with_cleanup: Whether the AI cleanup pass is switched on, which
                decides whether the stepper shows its third step at all.
        """
        self._with_cleanup = with_cleanup
        self.steps[2].setVisible(with_cleanup)
        self._connectors[1].setVisible(with_cleanup)
        self._started_at = time.monotonic()
        self.elapsed_label.setText(format_elapsed(0))
        self.detail_label.setText("")
        self.bar.set_indeterminate()
        self._clock.start()
        self.set_stage(ProgressStage.PREPARING)

    def set_stage(self, stage: ProgressStage, detail: Optional[str] = None) -> None:
        self._stage = stage
        self.title_label.setText(_STAGE_TITLES[stage])
        self.title_label.setProperty("stage", stage.value)
        self.title_label.style().unpolish(self.title_label)
        self.title_label.style().polish(self.title_label)
        if detail is not None:
            self.detail_label.setText(detail)
        self._apply_steps(stage)

        if stage in TERMINAL_STAGES:
            self._clock.stop()
            self._tick()
            if stage is ProgressStage.DONE:
                self.bar.set_fraction(1.0)
            else:
                self.bar.reset()
        elif not self.bar.is_indeterminate:
            self.bar.set_indeterminate()

    def set_detail(self, text: str) -> None:
        self.detail_label.setText(text)

    def set_large_file(self, file_size_mb: float, is_splitting: bool) -> None:
        stage = ProgressStage.SPLITTING if is_splitting else ProgressStage.PREPARING
        self.set_stage(stage, detail=f"{file_size_mb:.1f} MB file")

    def apply_overlay_state(self, state: OverlayState) -> bool:
        """Map a routed overlay state onto the stepper.

        Returns:
            Whether the state described a stage this panel shows.
        """
        stage = stage_for_overlay_state(state)
        if stage is None:
            return False
        self.set_stage(stage)
        return True

    def finish(self, success: bool) -> None:
        self.set_stage(ProgressStage.DONE if success else ProgressStage.FAILED)

    def _apply_steps(self, stage: ProgressStage) -> None:
        if stage is ProgressStage.DONE:
            for chip in self.steps:
                chip.set_state("done")
            return

        active = _STAGE_STEP_INDEX.get(stage)
        if active is None:
            # Failed or canceled: whatever was lit stays lit as the failed step,
            # so the user can see how far the job got.
            for chip in self.steps:
                if chip.state == "active":
                    chip.set_state("failed")
            return

        for index, chip in enumerate(self.steps):
            if index < active:
                chip.set_state("done")
            elif index == active:
                chip.set_state("active")
            else:
                chip.set_state("pending")

    def _tick(self) -> None:
        if self._started_at is None:
            return
        self.elapsed_label.setText(
            format_elapsed(time.monotonic() - self._started_at)
        )

    def hideEvent(self, event):
        super().hideEvent(event)
        self._clock.stop()

    def showEvent(self, event):
        super().showEvent(event)
        if self.is_running:
            self._clock.start()
