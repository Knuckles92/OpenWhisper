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
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from services.settings import SettingsKey, settings_manager
from ui_qt.widgets.buttons import Button, DangerButton, PrimaryButton, SuccessButton
from ui_qt.widgets.cards import Card
from ui_qt.widgets.wrapped_label import WrappedLabel

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
    retry_insights_requested = pyqtSignal()
    retry_speakers_requested = pyqtSignal()
    #: Emitted whenever the visible controls change, so the window can keep
    #: enough height for the finalization checklist and summary stats.
    content_height_changed = pyqtSignal()

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

    @staticmethod
    def _keep_natural_height(widget: QWidget) -> None:
        """Stop a container from being compressed below its content height.

        The finalization boxes hold fixed-height rows, so the default shrinkable
        policy lets a short window squeeze them into overlapping text instead of
        reporting how much room they need.

        Args:
            widget: Container whose height should never drop below its hint.
        """
        widget.setSizePolicy(
            widget.sizePolicy().horizontalPolicy(),
            QSizePolicy.Policy.Minimum,
        )

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

        subtitle = WrappedLabel(
            "Capture a live meeting with mic and system audio, then follow "
            "the transcript and insights in the browser dashboard."
        )
        subtitle.setObjectName("meetingModeSubtitle")
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

        self.demo_hint = WrappedLabel(
            "Loads a fake transcript and opens the dashboard. Turn on "
            "Cloud intelligence, then End, to test cleanup and the report."
        )
        self.demo_hint.setObjectName("meetingDemoHint")
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
        self._keep_natural_height(self.finalization_card)
        self.finalization_card.setProperty("finalizationTone", "neutral")

        # Header row with title and step badge
        fin_header_layout = QHBoxLayout()
        fin_header_layout.setContentsMargins(0, 0, 0, 0)
        fin_header_layout.setSpacing(8)

        self.finalization_title = QLabel("Final insights")
        self.finalization_title.setObjectName("meetingFinalizationTitle")
        self.finalization_title.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        fin_header_layout.addWidget(self.finalization_title)

        fin_header_layout.addStretch()

        self.finalization_step_badge = QLabel("")
        self.finalization_step_badge.setObjectName("meetingFinalizationStepBadge")
        self.finalization_step_badge.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
        self.finalization_step_badge.setProperty("badgeTone", "neutral")
        self.finalization_step_badge.hide()
        fin_header_layout.addWidget(self.finalization_step_badge)
        self.finalization_card.layout.addLayout(fin_header_layout)

        # Progress bar
        self.finalization_progress = QProgressBar()
        self.finalization_progress.setObjectName("meetingFinalizationProgress")
        self.finalization_progress.setTextVisible(False)
        self.finalization_progress.setMaximumHeight(6)
        self.finalization_progress.hide()
        self.finalization_card.layout.addWidget(self.finalization_progress)

        # Active status / details highlight container
        self.finalization_active_box = QWidget()
        self.finalization_active_box.setObjectName("meetingFinalizationActiveBox")
        active_box_layout = QVBoxLayout(self.finalization_active_box)
        active_box_layout.setContentsMargins(10, 8, 10, 8)
        active_box_layout.setSpacing(4)

        self.finalization_message = WrappedLabel("")
        self.finalization_message.setObjectName("meetingFinalizationMessage")
        active_box_layout.addWidget(self.finalization_message)

        self.finalization_detail = WrappedLabel("")
        self.finalization_detail.setObjectName("meetingFinalizationDetail")
        active_box_layout.addWidget(self.finalization_detail)
        self.finalization_card.layout.addWidget(self.finalization_active_box)

        # Multi-step pipeline / checklist container
        self.finalization_steps_widget = QWidget()
        self.finalization_steps_widget.setObjectName("meetingFinalizationStepsWidget")
        self.finalization_steps_layout = QVBoxLayout(self.finalization_steps_widget)
        # Matches the active box's inner padding so both inset the same amount
        # from the card. QSS padding does not inset a plain widget's layout.
        self.finalization_steps_layout.setContentsMargins(10, 8, 10, 8)
        self.finalization_steps_layout.setSpacing(6)
        self.finalization_steps_widget.hide()
        self._keep_natural_height(self.finalization_steps_widget)
        self.finalization_card.layout.addWidget(self.finalization_steps_widget)

        # Summary statistics container (shown on complete)
        self.finalization_stats_widget = QWidget()
        self.finalization_stats_widget.setObjectName("meetingFinalizationStatsWidget")
        self.finalization_stats_layout = QHBoxLayout(self.finalization_stats_widget)
        self.finalization_stats_layout.setContentsMargins(0, 6, 0, 6)
        self.finalization_stats_layout.setSpacing(8)
        self.finalization_stats_widget.hide()
        self._keep_natural_height(self.finalization_stats_widget)
        self.finalization_card.layout.addWidget(self.finalization_stats_widget)

        # Buttons row
        fin_buttons_row = QHBoxLayout()
        fin_buttons_row.setSpacing(10)

        self.finalization_retry_button = PrimaryButton("Retry insights")
        self.finalization_retry_button.setObjectName(
            "meetingFinalizationRetryButton"
        )
        self.finalization_retry_button.clicked.connect(
            self.retry_insights_requested.emit
        )
        self.finalization_retry_button.hide()
        fin_buttons_row.addWidget(self.finalization_retry_button)

        self.finalization_retry_speakers_button = Button("Re-run speakers")
        self.finalization_retry_speakers_button.setObjectName(
            "meetingFinalizationRetrySpeakersButton"
        )
        self.finalization_retry_speakers_button.clicked.connect(
            self.retry_speakers_requested.emit
        )
        self.finalization_retry_speakers_button.hide()
        fin_buttons_row.addWidget(self.finalization_retry_speakers_button)

        self.finalization_dashboard_button = Button("Open dashboard")
        self.finalization_dashboard_button.setObjectName(
            "meetingFinalizationDashboardButton"
        )
        self.finalization_dashboard_button.clicked.connect(
            self.open_dashboard_requested
        )
        fin_buttons_row.addWidget(self.finalization_dashboard_button)
        self.finalization_card.layout.addLayout(fin_buttons_row)
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
                ``message`` and multi-step progression fields for the persistent
                result view.
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
            "stage": str(value.get("stage") or ""),
            "current_step": int(value.get("current_step") or 0),
            "total_steps": int(value.get("total_steps") or 0),
            "step_details": str(value.get("step_details") or ""),
            "steps": list(value.get("steps") or []),
            "summary_stats": dict(value.get("summary_stats") or {}),
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

        self.content_height_changed.emit()

    def _render_finalization(self, finalization: Dict[str, Any]) -> None:
        """Update finalization card copy, tone, steps, and stats visibility.

        Args:
            finalization: Mapping with status, message, steps, and stats.
        """
        status = str(finalization.get("status") or "")
        message = str(finalization.get("message") or "").strip()
        current_step = int(finalization.get("current_step") or 0)
        total_steps = int(finalization.get("total_steps") or 0)
        step_details = str(finalization.get("step_details") or "").strip()
        steps = list(finalization.get("steps") or [])
        summary_stats = dict(finalization.get("summary_stats") or {})

        titles = {
            "running": "Finalizing Meeting",
            "completed": "Final Insights Ready",
            "disabled": "Cloud Insights Off",
            "unavailable": "Final Insights Unavailable",
            "failed": "Final Insights Incomplete",
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
            self.finalization_retry_button.hide()
            self.finalization_retry_speakers_button.hide()

            if total_steps > 0:
                self.finalization_step_badge.setText(f"Step {current_step} of {total_steps}")
                self.finalization_step_badge.setProperty("badgeTone", "info")
                self.finalization_step_badge.show()
                # Progress calculation
                pct = int(max(5, min(95, ((current_step - 0.5) / total_steps) * 100)))
                self.finalization_progress.setRange(0, 100)
                self.finalization_progress.setValue(pct)
            else:
                self.finalization_step_badge.setText("In Progress")
                self.finalization_step_badge.setProperty("badgeTone", "info")
                self.finalization_step_badge.show()
                # Indeterminate only when steps not configured
                self.finalization_progress.setRange(0, 0)

            if step_details and step_details != message:
                self.finalization_detail.setText(step_details)
                self.finalization_detail.show()
            else:
                self.finalization_detail.hide()

            if steps:
                self._populate_steps(steps, current_step)
                self.finalization_steps_widget.show()
            else:
                self.finalization_steps_widget.hide()

            self.finalization_stats_widget.hide()

        else:
            self.finalization_progress.hide()
            self.finalization_progress.setRange(0, 1)
            self.finalization_progress.setValue(0)

            if status == "completed":
                tone = "success"
                self.finalization_step_badge.setText("Complete")
                self.finalization_step_badge.setProperty("badgeTone", "success")
                self.finalization_step_badge.show()
                self.finalization_retry_button.hide()
                self.finalization_retry_speakers_button.show()
                self.finalization_retry_speakers_button.setEnabled(True)

                if step_details and step_details != message:
                    self.finalization_detail.setText(step_details)
                    self.finalization_detail.show()
                else:
                    self.finalization_detail.hide()

                if steps:
                    self._populate_steps(steps, current_step=len(steps))
                    self.finalization_steps_widget.show()
                else:
                    self.finalization_steps_widget.hide()

                if summary_stats:
                    self._populate_stats(summary_stats)
                    self.finalization_stats_widget.show()
                else:
                    self.finalization_stats_widget.hide()

            elif status in {"unavailable", "failed"}:
                tone = "warning"
                badge_text = "Failed" if status == "failed" else "Unavailable"
                self.finalization_step_badge.setText(badge_text)
                self.finalization_step_badge.setProperty("badgeTone", "warning")
                self.finalization_step_badge.show()
                self.finalization_retry_button.show()
                self.finalization_retry_button.setEnabled(True)
                self.finalization_retry_speakers_button.show()
                self.finalization_retry_speakers_button.setEnabled(True)

                if step_details and step_details != message:
                    self.finalization_detail.setText(step_details)
                    self.finalization_detail.show()
                else:
                    self.finalization_detail.hide()

                if steps:
                    self._populate_steps(steps, current_step)
                    self.finalization_steps_widget.show()
                else:
                    self.finalization_steps_widget.hide()

                self.finalization_stats_widget.hide()

            else:
                tone = "info"
                self.finalization_step_badge.setText("Off")
                self.finalization_step_badge.setProperty("badgeTone", "neutral")
                self.finalization_step_badge.show()
                self.finalization_detail.hide()
                self.finalization_steps_widget.hide()
                self.finalization_stats_widget.hide()
                self.finalization_retry_button.hide()
                self.finalization_retry_speakers_button.show()
                self.finalization_retry_speakers_button.setEnabled(True)

        self.finalization_card.setProperty("finalizationTone", tone)
        # Force QSS to re-evaluate dynamic properties.
        for widget in (self.finalization_card, self.finalization_step_badge):
            style = widget.style()
            if style is not None:
                style.unpolish(widget)
                style.polish(widget)
        self.finalization_card.update()

    def _populate_steps(self, steps: List[Dict[str, Any]], current_step: int) -> None:
        """Render the step pipeline rows.

        Args:
            steps: List of step dicts with id, name, status, detail.
            current_step: 1-based index of current step.
        """
        while self.finalization_steps_layout.count():
            item = self.finalization_steps_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for idx, s in enumerate(steps, 1):
            row = QWidget()
            row.setObjectName("meetingFinalizationStepRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 3, 0, 3)
            row_layout.setSpacing(8)

            step_status = s.get("status", "pending")
            if step_status == "completed":
                icon_text = "✓"
                icon_color = "#30d158"
            elif step_status == "running":
                icon_text = "●"
                icon_color = "#0a84ff"
            elif step_status == "failed":
                icon_text = "✗"
                icon_color = "#ff453a"
            elif step_status == "skipped":
                icon_text = "–"
                icon_color = "#636366"
            else:
                icon_text = "○"
                icon_color = "#636366"

            icon_label = QLabel(icon_text)
            icon_label.setObjectName("meetingFinalizationStepIcon")
            icon_label.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
            icon_label.setStyleSheet(f"color: {icon_color};")
            icon_label.setFixedWidth(16)
            row_layout.addWidget(icon_label)

            name_label = QLabel(s.get("name", f"Step {idx}"))
            name_label.setObjectName("meetingFinalizationStepName")
            name_font = QFont("Segoe UI", 11)
            if step_status == "running":
                name_font.setWeight(QFont.Weight.DemiBold)
            name_label.setFont(name_font)
            row_layout.addWidget(name_label)

            row_layout.addStretch()

            detail_text = s.get("detail", "")
            if detail_text and step_status == "running":
                detail_label = QLabel("In progress")
            elif step_status == "completed":
                detail_label = QLabel("Done")
            elif step_status == "failed":
                detail_label = QLabel("Failed")
            elif step_status == "skipped":
                detail_label = QLabel("Skipped")
            else:
                detail_label = QLabel("Queued")
            detail_label.setObjectName("meetingFinalizationStepDetail")
            detail_label.setFont(QFont("Segoe UI", 10))
            row_layout.addWidget(detail_label)

            self.finalization_steps_layout.addWidget(row)

    def _populate_stats(self, stats: Dict[str, Any]) -> None:
        """Render summary metric cards upon meeting completion.

        Args:
            stats: Summary metrics dict.
        """
        while self.finalization_stats_layout.count():
            item = self.finalization_stats_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        items_to_show = []

        dur_s = float(stats.get("duration_s") or 0.0)
        if dur_s > 0:
            mins, secs = divmod(int(dur_s), 60)
            dur_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            items_to_show.append(("Duration", dur_str))

        segs = int(stats.get("segments") or 0)
        if segs > 0:
            items_to_show.append(("Segments", f"{segs}"))

        words = int(stats.get("words") or 0)
        if words > 0:
            items_to_show.append(("Words", f"{words:,}"))

        kp = int(stats.get("key_points") or 0)
        if kp > 0:
            items_to_show.append(("Key Points", f"{kp}"))

        dec = int(stats.get("decisions") or 0)
        if dec > 0:
            items_to_show.append(("Decisions", f"{dec}"))

        ai = int(stats.get("action_items") or 0)
        if ai > 0:
            items_to_show.append(("Action Items", f"{ai}"))

        if not items_to_show:
            return

        for label, val in items_to_show:
            card = QWidget()
            card.setObjectName("meetingFinalizationStatItem")
            c_layout = QVBoxLayout(card)
            c_layout.setContentsMargins(6, 4, 6, 4)
            c_layout.setSpacing(2)
            c_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

            val_label = QLabel(val)
            val_label.setObjectName("meetingFinalizationStatValue")
            val_label.setFont(QFont("Segoe UI", 12, QFont.Weight.DemiBold))
            val_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            c_layout.addWidget(val_label)

            lbl_label = QLabel(label)
            lbl_label.setObjectName("meetingFinalizationStatLabel")
            lbl_label.setFont(QFont("Segoe UI", 9))
            lbl_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            c_layout.addWidget(lbl_label)

            self.finalization_stats_layout.addWidget(card)

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
