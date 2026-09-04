"""Main-window tab container."""
import logging
from typing import Optional
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QTabBar, QStackedWidget
)
from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont

from meeting.platform import (
    linux_meeting_implementation_ready,
    meeting_mode_supported,
    meeting_unsupported_os_name,
)
from services.settings import (
    SettingsKey,
    resolve_meeting_linux_preview_ack,
    resolve_meeting_unsupported_platform_ack,
    settings_manager,
)

logger = logging.getLogger(__name__)


class TabbedContentWidget(QWidget):
    """Container widget with a tab bar and stacked content area."""

    tab_changed = pyqtSignal(int)

    TAB_QUICK_RECORD = 0
    TAB_UPLOAD_FILE = 1
    TAB_MEETING_MODE = 2

    def __init__(self, parent=None):
        super().__init__(parent)

        self._last_saved_tab_index: Optional[int] = None
        self._pending_tab_index: Optional[int] = None
        self._last_allowed_index = self.TAB_QUICK_RECORD
        self._programmatic_tab_change = False
        self._recording_active = False
        self._recording_source_tab = -1
        self._meeting_unlocked = True
        self._tab_save_timer = QTimer(self)
        self._tab_save_timer.setSingleShot(True)
        self._tab_save_timer.timeout.connect(self._save_pending_tab_selection)

        self._setup_ui()
        self._apply_style()
        self._connect_signals()
        self._init_meeting_tab_lock()
        self._restore_last_tab()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.tab_bar = QTabBar()
        self.tab_bar.setObjectName("contentTabBar")
        self.tab_bar.setFont(QFont("Segoe UI", 13, QFont.Weight.DemiBold))
        self.tab_bar.setDrawBase(False)
        self.tab_bar.setExpanding(True)
        self.tab_bar.setUsesScrollButtons(False)

        self.tab_bar.addTab("Quick Record")
        self.tab_bar.addTab("Upload File")
        self.tab_bar.addTab("Meeting Mode")

        tab_container = QWidget()
        tab_container_layout = QVBoxLayout(tab_container)
        tab_container_layout.setContentsMargins(24, 16, 24, 8)
        tab_container_layout.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        tab_container_layout.addWidget(
            self.tab_bar,
            alignment=Qt.AlignmentFlag.AlignHCenter,
        )

        layout.addWidget(tab_container)

        self.stack = QStackedWidget()
        self.stack.setObjectName("contentStack")
        layout.addWidget(self.stack, stretch=1)

    def _apply_style(self):
        # Meeting Mode is the last tab; [unsupportedMeeting] greys it on
        # macOS/Linux without setTabEnabled(False), which would steal the
        # current index the moment a meeting is selected.
        self.tab_bar.setStyleSheet("""
            QTabBar::tab {
                background-color: transparent;
                color: #8e8e93;
                border: none;
                padding: 12px 14px;
                font-size: 14px;
                font-weight: 600;
                min-width: 120px;
            }
            QTabBar::tab:selected {
                color: #0a84ff;
                border-bottom: 2px solid #0a84ff;
            }
            QTabBar::tab:hover:!selected {
                color: #f5f5f7;
            }
            QTabBar::tab:disabled {
                color: #48484a;
            }
            QTabBar[unsupportedMeeting="true"]::tab:last {
                color: #48484a;
            }
            QTabBar[unsupportedMeeting="true"]::tab:last:hover {
                color: #636366;
            }
            QTabBar[unsupportedMeeting="true"]::tab:last:selected {
                color: #636366;
                border-bottom: 2px solid #545456;
            }
        """)

    def _connect_signals(self):
        self.tab_bar.currentChanged.connect(self._on_tab_changed)
        self.tab_bar.installEventFilter(self)

    def _init_meeting_tab_lock(self) -> None:
        """Grey Meeting Mode on unsupported OSes until (and after) first ack."""
        import sys

        supported = meeting_mode_supported()
        preview = (
            sys.platform.startswith("linux")
            and linux_meeting_implementation_ready()
        )
        acknowledged = (
            resolve_meeting_linux_preview_ack()
            if preview else resolve_meeting_unsupported_platform_ack()
        )
        self._meeting_unlocked = supported or acknowledged
        unsupported = not supported
        self.tab_bar.setProperty("unsupportedMeeting", unsupported)
        self.tab_bar.style().unpolish(self.tab_bar)
        self.tab_bar.style().polish(self.tab_bar)
        self.tab_bar.update()
        self._apply_meeting_tab_tooltip()

    def _apply_meeting_tab_tooltip(self) -> None:
        if meeting_mode_supported():
            self.tab_bar.setTabToolTip(
                self.TAB_MEETING_MODE,
                "Meeting Mode is in beta. Transcripts and insights may be inaccurate.",
            )
            return
        os_name = meeting_unsupported_os_name()
        try:
            import sys

            preview = (
                sys.platform.startswith("linux")
                and linux_meeting_implementation_ready()
            )
        except Exception:
            preview = False
        if preview:
            if self._meeting_unlocked:
                tip = (
                    f"Meeting Mode on {os_name} is a preview; "
                    "capture is implemented but not publicly supported yet."
                )
            else:
                tip = (
                    f"Meeting Mode on {os_name} is a preview "
                    "(not publicly supported yet). Click to continue."
                )
        elif self._meeting_unlocked:
            tip = (
                f"Meeting Mode is unsupported on {os_name}; "
                "system audio is unavailable."
            )
        else:
            tip = (
                f"Meeting Mode is not supported on {os_name}. "
                "Click to continue anyway."
            )
        self.tab_bar.setTabToolTip(self.TAB_MEETING_MODE, tip)

    def meeting_tab_is_locked(self) -> bool:
        """True when Meeting Mode still needs its platform acknowledgement."""
        return not meeting_mode_supported() and not self._meeting_unlocked

    def unlock_meeting_tab(self) -> None:
        """Record that the unsupported-platform warning was accepted."""
        self._meeting_unlocked = True
        self._apply_meeting_tab_tooltip()

    def eventFilter(self, obj, event):
        if obj is self.tab_bar and event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
        ):
            if event.button() == Qt.MouseButton.LeftButton:
                pos = (
                    event.position().toPoint()
                    if hasattr(event, "position")
                    else event.pos()
                )
                index = self.tab_bar.tabAt(pos)
                if index == self.TAB_MEETING_MODE and self.meeting_tab_is_locked():
                    self._on_locked_meeting_tab_activated()
                    return True
        return super().eventFilter(obj, event)

    def _on_locked_meeting_tab_activated(self) -> None:
        """Prompt for the first-time platform acknowledgement."""
        from ui_qt.dialogs.meeting_unsupported_dialog import (
            acknowledge_unsupported_meeting_mode,
        )

        if not acknowledge_unsupported_meeting_mode(self.window()):
            return
        self.unlock_meeting_tab()
        self.set_current_index(self.TAB_MEETING_MODE)

    def _restore_last_tab(self):
        try:
            settings = settings_manager.load_all_settings()
            last_tab = settings.get(SettingsKey.LAST_TAB_INDEX, self.TAB_QUICK_RECORD)
            if last_tab == self.TAB_MEETING_MODE and self.meeting_tab_is_locked():
                last_tab = self.TAB_QUICK_RECORD
            if 0 <= last_tab < self.tab_bar.count():
                self._last_saved_tab_index = last_tab
                self._programmatic_tab_change = True
                try:
                    self.tab_bar.setCurrentIndex(last_tab)
                finally:
                    self._programmatic_tab_change = False
        except Exception as e:
            logger.warning(f"Failed to restore last tab: {e}")

    def _on_tab_changed(self, index: int):
        if (
            index == self.TAB_MEETING_MODE
            and self.meeting_tab_is_locked()
            and not self._programmatic_tab_change
        ):
            fallback = self._last_allowed_index
            if fallback == self.TAB_MEETING_MODE:
                fallback = self.TAB_QUICK_RECORD
            self.tab_bar.blockSignals(True)
            self.tab_bar.setCurrentIndex(fallback)
            self.stack.setCurrentIndex(fallback)
            self.tab_bar.blockSignals(False)
            self._on_locked_meeting_tab_activated()
            return

        self.stack.setCurrentIndex(index)
        self._last_allowed_index = index

        self._schedule_tab_selection_save(index)

        self.tab_changed.emit(index)
        logger.debug(f"Tab changed to index {index}")

    def _schedule_tab_selection_save(self, index: int) -> None:
        """Persist tab selection after the UI has had time to switch."""
        if index == self._last_saved_tab_index:
            self._pending_tab_index = None
            if self._tab_save_timer.isActive():
                self._tab_save_timer.stop()
            return

        self._pending_tab_index = index
        self._tab_save_timer.start(250)

    def _save_pending_tab_selection(self) -> None:
        """Save the most recent tab selection outside the tab-click path."""
        if self._pending_tab_index is None:
            return

        index = self._pending_tab_index
        self._pending_tab_index = None

        try:
            settings_manager.save_setting(SettingsKey.LAST_TAB_INDEX, index)
            self._last_saved_tab_index = index
        except Exception as e:
            logger.warning(f"Failed to save tab selection: {e}")

    def flush_pending_tab_selection(self) -> None:
        """Synchronously persist any queued tab selection."""
        if self._tab_save_timer.isActive():
            self._tab_save_timer.stop()
        self._save_pending_tab_selection()

    def add_tab(self, widget: QWidget, title: str) -> int:
        index = self.stack.addWidget(widget)
        logger.debug(f"Added tab '{title}' at index {index}")
        return index

    def sync_stack_with_tab_bar(self):
        current_tab = self.tab_bar.currentIndex()
        if self.stack.currentIndex() != current_tab:
            logger.debug(
                f"Syncing stack (was {self.stack.currentIndex()}) "
                f"with tab bar (index {current_tab})"
            )
            self.stack.setCurrentIndex(current_tab)

    def current_index(self) -> int:
        return self.tab_bar.currentIndex()

    def set_current_index(self, index: int):
        if 0 <= index < self.tab_bar.count():
            self._programmatic_tab_change = True
            try:
                self.tab_bar.setCurrentIndex(index)
            finally:
                self._programmatic_tab_change = False

    def set_recording_state(self, is_recording: bool, source_tab: int):
        self._recording_active = is_recording
        self._recording_source_tab = source_tab
        for i in range(self.tab_bar.count()):
            self.tab_bar.setTabEnabled(i, not is_recording or i == source_tab)

        logger.debug(
            f"Recording state: active={is_recording}, source_tab={source_tab}"
        )
