"""Compact Meeting Mode strip for the main window.

Idle state shows a Start Meeting button plus the cloud-intelligence toggle;
during a meeting it becomes a status pill with an elapsed timer and the
pause/end/dashboard/guest-link controls. All user intent leaves through
signals; state flows back in via ``set_meeting_state`` payload dicts (partial
updates — absent keys leave the current state untouched).
"""
import logging
import time
from typing import Any, Dict, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QWidget

from services.settings import SettingsKey, settings_manager
from ui_qt.widgets.buttons import Button, DangerButton, SuccessButton

logger = logging.getLogger(__name__)


class MeetingPanel(QWidget):
    """Main-window strip with Meeting Mode controls."""

    start_requested = pyqtSignal(bool)  # cloud_enabled
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    end_requested = pyqtSignal()
    open_dashboard_requested = pyqtSignal()
    copy_guest_link_requested = pyqtSignal()
    cloud_toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("meetingPanel")

        self._active = False
        self._paused = False
        self._elapsed_base_s = 0.0
        self._running_since: Optional[float] = None

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._refresh_elapsed)

        self._setup_ui()
        self._apply_active_state()

    def _setup_ui(self):
        """Build the strip layout."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 6, 16, 6)
        layout.setSpacing(8)

        self.start_button = SuccessButton("Start Meeting")
        self.start_button.setObjectName("meetingStartButton")
        self.start_button.clicked.connect(self._on_start_clicked)
        layout.addWidget(self.start_button)

        self.status_pill = QLabel("Meeting")
        self.status_pill.setObjectName("meetingStatusPill")
        self.status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.status_pill)

        self.elapsed_label = QLabel("00:00")
        self.elapsed_label.setObjectName("meetingElapsedLabel")
        layout.addWidget(self.elapsed_label)

        self.pause_button = Button("Pause")
        self.pause_button.setObjectName("meetingPauseButton")
        self.pause_button.clicked.connect(self._on_pause_clicked)
        layout.addWidget(self.pause_button)

        self.end_button = DangerButton("End")
        self.end_button.setObjectName("meetingEndButton")
        self.end_button.clicked.connect(self.end_requested)
        layout.addWidget(self.end_button)

        self.dashboard_button = Button("Open dashboard")
        self.dashboard_button.setObjectName("meetingDashboardButton")
        self.dashboard_button.clicked.connect(self.open_dashboard_requested)
        layout.addWidget(self.dashboard_button)

        self.guest_link_button = Button("Copy guest link")
        self.guest_link_button.setObjectName("meetingGuestLinkButton")
        self.guest_link_button.clicked.connect(self.copy_guest_link_requested)
        layout.addWidget(self.guest_link_button)

        layout.addStretch()

        self.cloud_checkbox = QCheckBox("Cloud intelligence")
        self.cloud_checkbox.setObjectName("meetingCloudCheckbox")
        self.cloud_checkbox.setChecked(
            bool(settings_manager.get(SettingsKey.MEETING_CLOUD_LAST_ENABLED, False))
        )
        self.cloud_checkbox.toggled.connect(self.cloud_toggled)
        layout.addWidget(self.cloud_checkbox)

    # ------------------------------------------------------------------
    # User intent
    # ------------------------------------------------------------------

    def _on_start_clicked(self):
        """Emit the start request with the current cloud choice."""
        self.start_requested.emit(self.cloud_checkbox.isChecked())

    def _on_pause_clicked(self):
        """Route the pause/resume button to the matching signal."""
        if self._paused:
            self.resume_requested.emit()
        else:
            self.pause_requested.emit()

    # ------------------------------------------------------------------
    # State inflow
    # ------------------------------------------------------------------

    def set_meeting_state(self, payload: Dict[str, Any]) -> None:
        """Apply a (possibly partial) meeting-state payload.

        Args:
            payload: Dict with any of ``active`` (bool), ``paused`` (bool),
                ``status`` (str), ``cloud_enabled`` (bool), ``elapsed_s``
                (float; resets the elapsed timer base).
        """
        if not isinstance(payload, dict):
            return

        if "cloud_enabled" in payload:
            self.cloud_checkbox.blockSignals(True)
            self.cloud_checkbox.setChecked(bool(payload["cloud_enabled"]))
            self.cloud_checkbox.blockSignals(False)

        if "elapsed_s" in payload:
            try:
                self._elapsed_base_s = float(payload["elapsed_s"])
            except (TypeError, ValueError):
                self._elapsed_base_s = 0.0
            self._running_since = time.monotonic()
            self._refresh_elapsed()

        if "paused" in payload:
            self._set_paused(bool(payload["paused"]))

        if "status" in payload:
            self.set_status_text(str(payload["status"]).capitalize())

        if "active" in payload:
            self._set_active(bool(payload["active"]))

    def set_status_text(self, text: str) -> None:
        """Update the status pill label.

        Args:
            text: Short status string (e.g. "Active", "Paused", "Ending").
        """
        if text:
            self.status_pill.setText(text)

    def _set_active(self, active: bool) -> None:
        """Switch between the idle and in-meeting layouts."""
        if active == self._active:
            return
        self._active = active
        if active:
            if self._running_since is None:
                self._elapsed_base_s = 0.0
                self._running_since = time.monotonic()
            self._elapsed_timer.start()
        else:
            self._elapsed_timer.stop()
            self._paused = False
            self._elapsed_base_s = 0.0
            self._running_since = None
            self.pause_button.setText("Pause")
        self._apply_active_state()

    def _set_paused(self, paused: bool) -> None:
        """Freeze or resume the elapsed timer to mirror the meeting clock."""
        if paused == self._paused:
            return
        self._paused = paused
        now = time.monotonic()
        if paused:
            if self._running_since is not None:
                self._elapsed_base_s += now - self._running_since
                self._running_since = None
            self.pause_button.setText("Resume")
        else:
            self._running_since = now
            self.pause_button.setText("Pause")

    def _apply_active_state(self) -> None:
        """Show idle vs in-meeting controls."""
        self.start_button.setVisible(not self._active)
        for widget in (
            self.status_pill,
            self.elapsed_label,
            self.pause_button,
            self.end_button,
            self.dashboard_button,
            self.guest_link_button,
        ):
            widget.setVisible(self._active)

    def _refresh_elapsed(self) -> None:
        """Update the elapsed label from the local pause-aware timer."""
        elapsed = self._elapsed_base_s
        if self._running_since is not None:
            elapsed += time.monotonic() - self._running_since
        total = int(max(0, elapsed))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            self.elapsed_label.setText(f"{hours}:{minutes:02d}:{seconds:02d}")
        else:
            self.elapsed_label.setText(f"{minutes:02d}:{seconds:02d}")

    @property
    def is_meeting_active(self) -> bool:
        """True while the panel shows the in-meeting layout."""
        return self._active
