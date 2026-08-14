"""Meeting Mode tab for the main window.

Idle state shows a Start Meeting control plus the cloud-intelligence toggle;
during a meeting it becomes a status card with an elapsed timer and the
pause/end/dashboard/guest-link controls. After capture ends, a persistent
finalization card reports running/completed/disabled/unavailable/failed cloud
outcomes without blocking other tabs. All user intent leaves through signals;
state flows back in via ``set_meeting_state`` payload dicts (partial updates —
absent keys leave the current state untouched).
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
    QProgressBar,
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
    demo_requested = pyqtSignal(bool)  # cloud_enabled
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
        self._finalization: Optional[Dict[str, str]] = None
        self._has_dashboard = False
        self._developer_mode = False

        self._elapsed_timer = QTimer(self)
        self._elapsed_timer.setInterval(1000)
        self._elapsed_timer.timeout.connect(self._refresh_elapsed)

        self._setup_ui()
        self.set_developer_mode(
            bool(settings_manager.get(SettingsKey.DEVELOPER_MODE, False))
        )
        self._apply_layout_state()

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

        self.demo_button = Button("Load demo meeting")
        self.demo_button.setObjectName("meetingDemoButton")
        self.demo_button.setMaximumWidth(320)
        self.demo_button.setToolTip(
            "Open the dashboard with a fake transcript so you can test "
            "end-of-meeting cleanup and the final report without recording."
        )
        self.demo_button.clicked.connect(self._on_demo_clicked)
        idle_layout.addWidget(
            self.demo_button, alignment=Qt.AlignmentFlag.AlignCenter
        )

        self.demo_hint = QLabel(
            "Loads a fake transcript and opens the dashboard. Turn on "
            "Cloud intelligence, then End, to test cleanup and the report."
        )
        self.demo_hint.setObjectName("meetingDemoHint")
        self.demo_hint.setWordWrap(True)
        self.demo_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.demo_hint.setFont(QFont("Segoe UI", 10))
        idle_layout.addWidget(self.demo_hint)

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

        # Post-meeting finalization / result card
        self.finalization_card = Card()
        self.finalization_card.setObjectName("meetingFinalizationCard")
        self.finalization_card.setMinimumHeight(0)
        self.finalization_card.setProperty("finalizationTone", "neutral")

        self.finalization_title = QLabel("Final insights")
        self.finalization_title.setObjectName("meetingFinalizationTitle")
        self.finalization_title.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        self.finalization_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.finalization_card.layout.addWidget(self.finalization_title)

        self.finalization_message = QLabel("")
        self.finalization_message.setObjectName("meetingFinalizationMessage")
        self.finalization_message.setWordWrap(True)
        self.finalization_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.finalization_card.layout.addWidget(self.finalization_message)

        self.finalization_progress = QProgressBar()
        self.finalization_progress.setObjectName("meetingFinalizationProgress")
        self.finalization_progress.setTextVisible(False)
        self.finalization_progress.setMaximumHeight(6)
        self.finalization_progress.hide()
        self.finalization_card.layout.addWidget(self.finalization_progress)

        self.finalization_dashboard_button = Button("Open dashboard")
        self.finalization_dashboard_button.setObjectName(
            "meetingFinalizationDashboardButton"
        )
        self.finalization_dashboard_button.clicked.connect(
            self.open_dashboard_requested
        )
        self.finalization_card.layout.addWidget(
            self.finalization_dashboard_button,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        content_layout.addWidget(self.finalization_card)

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

    def _on_demo_clicked(self):
        """Emit the developer-mode demo meeting request."""
        self.demo_requested.emit(self.cloud_checkbox.isChecked())

    def _on_pause_clicked(self):
        """Route the pause/resume button to the matching signal."""
        if self._paused:
            self.resume_requested.emit()
        else:
            self.pause_requested.emit()

    # ------------------------------------------------------------------
    # State inflow
    # ------------------------------------------------------------------

    def set_developer_mode(self, enabled: bool) -> None:
        """Show or hide demo-meeting controls.

        Args:
            enabled: True when Settings → Advanced → Developer mode is on.
        """
        self._developer_mode = bool(enabled)
        self._apply_layout_state()

    def set_meeting_state(self, payload: Dict[str, Any]) -> None:
        """Apply a (possibly partial) meeting-state payload.

        Args:
            payload: Dict with any of ``active`` (bool), ``paused`` (bool),
                ``status`` (str), ``cloud_enabled`` (bool), ``elapsed_s``
                (float; resets the elapsed timer base), ``finalization``
                (``{status, message}`` or ``None`` to clear), and
                ``dashboard_available`` (bool).
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
            status = str(payload["status"])
            self.set_status_text(status.capitalize())
            # A new meeting start clears any previous finalization result.
            if status == "starting":
                self._finalization = None

        if "dashboard_available" in payload:
            self._has_dashboard = bool(payload["dashboard_available"])

        if "finalization" in payload:
            self._set_finalization(payload.get("finalization"))

        if "active" in payload:
            self._set_active(bool(payload["active"]))
        else:
            self._apply_layout_state()

    def set_status_text(self, text: str) -> None:
        """Update the status pill label.

        Args:
            text: Short status string (e.g. "Active", "Paused", "Ending").
        """
        if text:
            self.status_pill.setText(text)

    def set_dashboard_available(self, available: bool) -> None:
        """Enable Open Dashboard when a retained URL exists.

        Args:
            available: True when the runtime still holds a dashboard URL.
        """
        self._has_dashboard = bool(available)
        self._apply_layout_state()

    def _set_finalization(self, value: Any) -> None:
        """Store a finalization payload or clear it.

        Args:
            value: ``None`` clears the card; a mapping keeps ``status`` /
                ``message`` for the persistent result view.
        """
        if value is None:
            self._finalization = None
            return
        if not isinstance(value, dict):
            return
        status = str(value.get("status") or "").strip()
        if not status:
            self._finalization = None
            return
        self._finalization = {
            "status": status,
            "message": str(value.get("message") or ""),
        }

    def _set_active(self, active: bool) -> None:
        """Switch between the idle and in-meeting layouts."""
        if active == self._active:
            self._apply_layout_state()
            return
        self._active = active
        if active:
            # Starting a live session replaces any prior finalization result.
            self._finalization = None
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
        self._apply_layout_state()

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

    def _apply_layout_state(self) -> None:
        """Show idle, in-meeting, and/or finalization controls."""
        finalization = self._finalization
        final_status = (finalization or {}).get("status") or ""
        running_finalization = final_status == "running"
        show_finalization = bool(finalization) and not self._active

        self.session_card.setVisible(self._active)
        # Hide Start only while finalization is actively running; terminal
        # outcomes restore Start so the user can begin another meeting.
        self.idle_card.setVisible(not self._active and not running_finalization)
        self.start_button.setVisible(not running_finalization)
        show_demo = (
            self._developer_mode
            and not self._active
            and not running_finalization
        )
        self.demo_button.setVisible(show_demo)
        self.demo_hint.setVisible(show_demo)
        self.finalization_card.setVisible(show_finalization)

        if show_finalization and finalization is not None:
            self._render_finalization(finalization)

        dashboard_enabled = self._active or self._has_dashboard or show_finalization
        self.dashboard_button.setEnabled(self._active or self._has_dashboard)
        self.finalization_dashboard_button.setEnabled(
            self._has_dashboard or self._active
        )
        self.finalization_dashboard_button.setVisible(dashboard_enabled)

    def _render_finalization(self, finalization: Dict[str, str]) -> None:
        """Update finalization card copy, tone, and progress visibility.

        Args:
            finalization: Mapping with ``status`` and ``message``.
        """
        status = finalization.get("status") or ""
        message = (finalization.get("message") or "").strip()
        titles = {
            "running": "Preparing final insights",
            "completed": "Final insights ready",
            "disabled": "Cloud insights off",
            "unavailable": "Final insights unavailable",
            "failed": "Final insights incomplete",
        }
        defaults = {
            "running": "Preparing final cloud insights…",
            "completed": "Final cloud insights are ready.",
            "disabled": "Cloud intelligence is off for this meeting.",
            "unavailable": "Final cloud insights could not run.",
            "failed": "Final cloud insights failed.",
        }
        self.finalization_title.setText(titles.get(status, "Final insights"))
        self.finalization_message.setText(message or defaults.get(status, ""))

        if status == "running":
            tone = "neutral"
            self.finalization_progress.show()
            # Indeterminate only — no fabricated percentage.
            self.finalization_progress.setRange(0, 0)
        else:
            self.finalization_progress.hide()
            self.finalization_progress.setRange(0, 1)
            self.finalization_progress.setValue(0)
            if status == "completed":
                tone = "success"
            elif status in {"unavailable", "failed"}:
                tone = "warning"
            else:
                tone = "info"

        self.finalization_card.setProperty("finalizationTone", tone)
        # Force QSS to re-evaluate dynamic properties.
        style = self.finalization_card.style()
        if style is not None:
            style.unpolish(self.finalization_card)
            style.polish(self.finalization_card)
        self.finalization_card.update()

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

    @property
    def finalization_status(self) -> Optional[str]:
        """Current finalization status, or None when no result is shown."""
        if not self._finalization:
            return None
        return self._finalization.get("status")
