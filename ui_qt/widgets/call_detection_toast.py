"""Non-intrusive, focus-free desktop prompt for automatic call detection & auto-stop.

Sits in the corner of the screen without stealing keyboard focus (using
``WA_ShowWithoutActivating`` and ``WindowDoesNotAcceptFocus``), matching the
design language of ``WalkthroughReadyCard``. Supports three modes:

1. **Offer**: "<App> call detected" with Start Meeting Mode, Not now, and
   per-app Always record / Don't offer choices (auto-times out after 20 s).
2. **Auto-started notice**: "Recording <App> call" brief confirmation.
3. **Call-ended grace countdown**: "<App> call ended — Stopping in 30 s" with
   Keep recording and Stop now actions.
"""

from __future__ import annotations

import sys
from typing import Optional

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ui_qt.utils.icons import design_icon
from ui_qt.widgets.context_menu import context_menu

SCREEN_MARGIN = 18
OFFER_TIMEOUT_MS = 20_000
OFFER_LINGER_MS = 5_000
AUTO_STARTED_NOTICE_MS = 6_000


class CallDetectionCard(QWidget):
    """Focus-free corner toast for call detection offers and auto-stop countdowns."""

    start_requested = pyqtSignal(object, int)          # (CallCandidate, offer_id)
    not_now_requested = pyqtSignal(int)                # (offer_id)
    always_record_requested = pyqtSignal(object, int)  # (CallCandidate, offer_id)
    dont_offer_requested = pyqtSignal(object, int)     # (CallCandidate, offer_id)
    offer_timed_out = pyqtSignal(int)                  # (offer_id)
    keep_recording_requested = pyqtSignal()
    stop_now_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("callDetectionCard")
        self.setWindowTitle("Meeting Mode")
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        if sys.platform == "darwin":
            self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)

        self._mode: str = "idle"  # "idle" | "offer" | "auto_started" | "call_ended"
        self._candidate = None
        self._offer_id: Optional[int] = None
        self._grace_remaining_s: int = 0
        self._grace_app_label: str = ""

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self._on_dismiss_timeout)

        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._on_countdown_tick)

        self._build()

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def current_offer_id(self) -> Optional[int]:
        return self._offer_id

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        surface = QFrame()
        surface.setObjectName("callDetectionSurface")
        outer.addWidget(surface)

        root = QHBoxLayout(surface)
        root.setContentsMargins(14, 12, 10, 12)
        root.setSpacing(10)

        icon_label = QLabel()
        icon_label.setObjectName("callDetectionIcon")
        icon_label.setPixmap(design_icon("microphone-blue.svg").pixmap(20, 20))
        root.addWidget(icon_label, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(3)

        self.title_label = QLabel("")
        self.title_label.setObjectName("callDetectionTitle")
        col.addWidget(self.title_label)

        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("callDetectionSubtitle")
        self.subtitle_label.setWordWrap(True)
        self.subtitle_label.setMaximumWidth(310)
        col.addWidget(self.subtitle_label)

        # Action row for Offer mode
        self._offer_row_widget = QWidget()
        offer_row = QHBoxLayout(self._offer_row_widget)
        offer_row.setContentsMargins(0, 8, 0, 0)
        offer_row.setSpacing(6)

        self.start_btn = QToolButton()
        self.start_btn.setObjectName("callDetectionPrimaryBtn")
        self.start_btn.setText("Start Meeting Mode")
        self.start_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.start_btn.clicked.connect(self._on_start_clicked)
        offer_row.addWidget(self.start_btn)

        self.not_now_btn = QToolButton()
        self.not_now_btn.setObjectName("callDetectionSecondaryBtn")
        self.not_now_btn.setText("Not now")
        self.not_now_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.not_now_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.not_now_btn.clicked.connect(self._on_not_now_clicked)
        offer_row.addWidget(self.not_now_btn)

        offer_row.addStretch(1)

        self.more_menu = context_menu(self)
        self.always_record_action = self.more_menu.addAction("Always record")
        self.always_record_action.triggered.connect(self._on_always_record_clicked)
        self.dont_offer_action = self.more_menu.addAction("Don't offer")
        self.dont_offer_action.triggered.connect(self._on_dont_offer_clicked)
        self.more_menu.aboutToShow.connect(self._dismiss_timer.stop)
        self.more_menu.aboutToHide.connect(self._resume_offer_linger)

        self.more_btn = QToolButton()
        self.more_btn.setObjectName("callDetectionMoreBtn")
        self.more_btn.setIcon(design_icon("dots-gray.svg"))
        self.more_btn.setIconSize(QSize(15, 15))
        self.more_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.more_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.more_btn.setAccessibleName("More options")
        self.more_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more_btn.setMenu(self.more_menu)
        offer_row.addWidget(self.more_btn)
        col.addWidget(self._offer_row_widget)

        # Action row for Call-Ended grace countdown mode
        self._ended_row_widget = QWidget()
        ended_row = QHBoxLayout(self._ended_row_widget)
        ended_row.setContentsMargins(0, 8, 0, 0)
        ended_row.setSpacing(6)

        self.keep_recording_btn = QToolButton()
        self.keep_recording_btn.setObjectName("callDetectionPrimaryBtn")
        self.keep_recording_btn.setText("Keep recording")
        self.keep_recording_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.keep_recording_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.keep_recording_btn.clicked.connect(self._on_keep_recording_clicked)
        ended_row.addWidget(self.keep_recording_btn)

        self.stop_now_btn = QToolButton()
        self.stop_now_btn.setObjectName("callDetectionSecondaryBtn")
        self.stop_now_btn.setText("Stop now")
        self.stop_now_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_now_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.stop_now_btn.clicked.connect(self._on_stop_now_clicked)
        ended_row.addWidget(self.stop_now_btn)

        ended_row.addStretch(1)
        col.addWidget(self._ended_row_widget)

        root.addLayout(col, 1)

        close_btn = QToolButton()
        close_btn.setObjectName("callDetectionCloseBtn")
        close_btn.setIcon(design_icon("x-gray.svg"))
        close_btn.setIconSize(QSize(13, 13))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.setAccessibleName("Close")
        close_btn.clicked.connect(self._on_close_clicked)
        root.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)

    # ------------------------------------------------------------------
    # Presentation modes
    # ------------------------------------------------------------------

    def present_offer(
        self, candidate, offer_id: int, *, timeout_ms: int = OFFER_TIMEOUT_MS
    ) -> None:
        """Show the 'Ask each time' offer prompt for ``candidate``."""
        self._stop_timers()
        self._mode = "offer"
        self._candidate = candidate
        self._offer_id = offer_id

        self.title_label.setText(candidate.offer_title())
        self.subtitle_label.setText(candidate.offer_subtitle())

        provider = candidate.provider()
        provider_label = provider.label if provider is not None else candidate.app_label()
        self.always_record_action.setText(f"Always record {provider_label}")
        self.always_record_action.setVisible(candidate.can_always_record())
        self.dont_offer_action.setText(f"Don't offer for {provider_label}")
        self.dont_offer_action.setVisible(provider is not None)
        self.more_btn.setVisible(provider is not None)

        self._offer_row_widget.setVisible(True)
        self._ended_row_widget.setVisible(False)
        self._show_in_corner()
        self._dismiss_timer.start(max(1000, int(timeout_ms)))

    def withdraw_offer(self, offer_id: int) -> None:
        """Close the card if it is currently showing ``offer_id``."""
        if self._mode == "offer" and self._offer_id == offer_id:
            self._reset_and_hide()

    def present_auto_started(
        self, candidate, *, duration_ms: int = AUTO_STARTED_NOTICE_MS
    ) -> None:
        """Show a brief confirmation that Meeting Mode started automatically."""
        self._stop_timers()
        self._mode = "auto_started"
        self._candidate = candidate
        self._offer_id = None

        self.title_label.setText(f"Recording {candidate.app_label()} call")
        self.subtitle_label.setText("Meeting Mode started automatically")
        self._offer_row_widget.setVisible(False)
        self._ended_row_widget.setVisible(False)
        self._show_in_corner()
        self._dismiss_timer.start(max(1000, int(duration_ms)))

    def present_call_ended(self, app_label: str, grace_s: int = 30) -> None:
        """Show the auto-stop grace countdown for ``app_label``."""
        self._stop_timers()
        self._mode = "call_ended"
        self._candidate = None
        self._offer_id = None
        self._grace_app_label = app_label
        self._grace_remaining_s = max(1, int(grace_s))

        self.title_label.setText(f"{app_label} call ended")
        self.subtitle_label.setText(f"Stopping in {self._grace_remaining_s} s")
        self._offer_row_widget.setVisible(False)
        self._ended_row_widget.setVisible(True)
        self._show_in_corner()
        self._countdown_timer.start()

    def cancel_call_ended(self) -> None:
        """Hide the call-ended countdown (e.g. call resumed or user stopped)."""
        if self._mode == "call_ended":
            self._reset_and_hide()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _stop_timers(self) -> None:
        self._dismiss_timer.stop()
        self._countdown_timer.stop()

    def _reset_and_hide(self) -> None:
        self._stop_timers()
        self._mode = "idle"
        self._candidate = None
        self._offer_id = None
        self.hide()

    def _show_in_corner(self) -> None:
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()

    def _place(self) -> None:
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        size = self.sizeHint()
        self.move(
            area.right() - size.width() - SCREEN_MARGIN + 1,
            area.bottom() - size.height() - SCREEN_MARGIN + 1,
        )

    def enterEvent(self, event) -> None:
        if self._mode == "offer":
            self._dismiss_timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        if self._mode == "offer" and not self.more_menu.isVisible():
            self._resume_offer_linger()
        super().leaveEvent(event)

    def _resume_offer_linger(self) -> None:
        if self._mode == "offer" and self.isVisible() and not self.underMouse():
            self._dismiss_timer.start(OFFER_LINGER_MS)

    def _on_dismiss_timeout(self) -> None:
        if self._mode == "offer" and self._offer_id is not None:
            oid = self._offer_id
            self._reset_and_hide()
            self.offer_timed_out.emit(oid)
            return
        self._reset_and_hide()

    def _on_countdown_tick(self) -> None:
        if self._mode != "call_ended":
            self._countdown_timer.stop()
            return
        self._grace_remaining_s = max(0, self._grace_remaining_s - 1)
        if self._grace_remaining_s <= 0:
            self.subtitle_label.setText("Stopping Meeting Mode…")
            self._countdown_timer.stop()
            return
        self.subtitle_label.setText(f"Stopping in {self._grace_remaining_s} s")

    def _on_start_clicked(self) -> None:
        candidate, oid = self._candidate, self._offer_id
        self._reset_and_hide()
        if candidate is not None and oid is not None:
            self.start_requested.emit(candidate, oid)

    def _on_not_now_clicked(self) -> None:
        oid = self._offer_id
        self._reset_and_hide()
        if oid is not None:
            self.not_now_requested.emit(oid)

    def _on_always_record_clicked(self) -> None:
        candidate, oid = self._candidate, self._offer_id
        self._reset_and_hide()
        if candidate is not None and oid is not None:
            self.always_record_requested.emit(candidate, oid)

    def _on_dont_offer_clicked(self) -> None:
        candidate, oid = self._candidate, self._offer_id
        self._reset_and_hide()
        if candidate is not None and oid is not None:
            self.dont_offer_requested.emit(candidate, oid)

    def _on_keep_recording_clicked(self) -> None:
        self._reset_and_hide()
        self.keep_recording_requested.emit()

    def _on_stop_now_clicked(self) -> None:
        self._reset_and_hide()
        self.stop_now_requested.emit()

    def _on_close_clicked(self) -> None:
        if self._mode == "offer":
            self._on_not_now_clicked()
        elif self._mode == "call_ended":
            self._on_keep_recording_clicked()
        else:
            self._reset_and_hide()
