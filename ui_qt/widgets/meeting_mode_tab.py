"""Meeting Mode tab for the main window.

Idle state shows a Start Meeting control plus the cloud-intelligence toggle;
during a meeting it becomes a status card with an elapsed timer and the
pause/end/dashboard/guest-link controls. All user intent leaves through
signals; state flows back in via ``set_meeting_state`` payload dicts (partial
updates — absent keys leave the current state untouched).
"""
import logging
import time
from typing import Any, Dict, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from services.settings import SettingsKey, settings_manager
from ui_qt.widgets.buttons import Button, DangerButton, SuccessButton
from ui_qt.widgets.cards import Card

logger = logging.getLogger(__name__)


class MeetingModeTab(QWidget):
    """Full-page tab with Meeting Mode session controls."""

    start_requested = pyqtSignal(bool)  # cloud_enabled
    pause_requested = pyqtSignal()
    resume_requested = pyqtSignal()
    end_requested = pyqtSignal()
    open_dashboard_requested = pyqtSignal()
    copy_guest_link_requested = pyqtSignal()
    cloud_toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("meetingModeTab")

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
        """Build the full-tab layout."""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        content = QWidget()
        content.setObjectName("meetingModeContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(24, 14, 24, 16)
        content_layout.setSpacing(16)

        center_wrapper = QHBoxLayout()
        center_wrapper.addStretch()
        center_wrapper.addWidget(content, stretch=1)
        center_wrapper.addStretch()
        content.setMaximumWidth(700)
        content.setMinimumWidth(500)
        main_layout.addLayout(center_wrapper)

        intro_card = Card()
        intro_card.setMinimumHeight(0)
        title = QLabel("Meeting Mode")
        title.setObjectName("headerLabel")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.DemiBold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        intro_card.layout.addWidget(title)

        subtitle = QLabel(
            "Capture a live meeting with mic and system audio, then follow "
            "the transcript and insights in the browser dashboard."
        )
        subtitle.setObjectName("meetingModeSubtitle")
        subtitle.setWordWrap(True)
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setFont(QFont("Segoe UI", 11))
        intro_card.layout.addWidget(subtitle)
        content_layout.addWidget(intro_card)

        # Idle controls
        self.idle_card = Card()
        self.idle_card.setMinimumHeight(0)
        idle_inner = QWidget()
        idle_layout = QVBoxLayout(idle_inner)
        idle_layout.setContentsMargins(0, 8, 0, 8)
        idle_layout.setSpacing(16)
        idle_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.start_button = SuccessButton("Start Meeting")
        self.start_button.setObjectName("meetingStartButton")
        self.start_button.setMinimumHeight(48)
        self.start_button.setMaximumWidth(320)
        self.start_button.clicked.connect(self._on_start_clicked)
        idle_layout.addWidget(self.start_button, alignment=Qt.AlignmentFlag.AlignCenter)

        self.idle_card.layout.addWidget(idle_inner)
        content_layout.addWidget(self.idle_card)

        # Active session card
        self.session_card = Card()
        self.session_card.setMinimumHeight(0)

        status_row = QHBoxLayout()
        status_row.setSpacing(12)
        status_row.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.status_pill = QLabel("Meeting")
        self.status_pill.setObjectName("meetingStatusPill")
        self.status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_row.addWidget(self.status_pill)

        self.elapsed_label = QLabel("00:00")
        self.elapsed_label.setObjectName("meetingElapsedLabel")
        status_row.addWidget(self.elapsed_label)
        self.session_card.layout.addLayout(status_row)

        controls_row = QHBoxLayout()
        controls_row.setSpacing(10)

        self.pause_button = Button("Pause")
        self.pause_button.setObjectName("meetingPauseButton")
        self.pause_button.clicked.connect(self._on_pause_clicked)
        controls_row.addWidget(self.pause_button, stretch=1)

        self.end_button = DangerButton("End")
        self.end_button.setObjectName("meetingEndButton")
        self.end_button.clicked.connect(self.end_requested)
        controls_row.addWidget(self.end_button, stretch=1)
        self.session_card.layout.addLayout(controls_row)

        links_row = QHBoxLayout()
        links_row.setSpacing(10)

        self.dashboard_button = Button("Open dashboard")
        self.dashboard_button.setObjectName("meetingDashboardButton")
        self.dashboard_button.clicked.connect(self.open_dashboard_requested)
        links_row.addWidget(self.dashboard_button, stretch=1)

        self.guest_link_button = Button("Copy guest link")
        self.guest_link_button.setObjectName("meetingGuestLinkButton")
        self.guest_link_button.clicked.connect(self.copy_guest_link_requested)
        links_row.addWidget(self.guest_link_button, stretch=1)
        self.session_card.layout.addLayout(links_row)

        content_layout.addWidget(self.session_card)

        self.cloud_checkbox = QCheckBox("Cloud intelligence")
        self.cloud_checkbox.setObjectName("meetingCloudCheckbox")
        self.cloud_checkbox.setChecked(
            bool(settings_manager.get(SettingsKey.MEETING_CLOUD_LAST_ENABLED, False))
        )
        self.cloud_checkbox.toggled.connect(self.cloud_toggled)
        content_layout.addWidget(
            self.cloud_checkbox, alignment=Qt.AlignmentFlag.AlignCenter
        )

        content_layout.addStretch()

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
            self.elapsed_label.setText("00:00")
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
        self.idle_card.setVisible(not self._active)
        self.session_card.setVisible(self._active)

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
        """True while the tab shows the in-meeting layout."""
        return self._active
