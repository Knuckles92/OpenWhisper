import logging
import sys
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QPushButton
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QEvent, QPropertyAnimation, QRect
from PyQt6.QtGui import QAction, QKeySequence

from config import config
from services.hotkey_manager import format_hotkey_display
from services.settings import (
    SettingsKey,
    resolve_meeting_mode_intro_seen,
    settings_manager,
)
from ui_qt.utils.collapse_animation import (
    SECTION_COLLAPSE_DURATION_MS,
    SECTION_COLLAPSE_EASING,
    UNLIMITED_HEIGHT,
)

logger = logging.getLogger(__name__)


class CustomTitleBar(QFrame):
    _MENU_BAR_STYLE = """
        QMenuBar {
            background-color: transparent;
            color: #8e8e93;
            font-size: 12px;
            border: none;
            spacing: 0px;
        }
        QMenuBar::item {
            background-color: transparent;
            padding: 8px 10px 4px 10px;
        }
        QMenuBar::item:selected {
            background-color: #3a3a3c;
            color: #ffffff;
        }
        QMenuBar::item:pressed {
            background-color: #48484a;
        }
        QMenu::separator {
            height: 1px;
            background-color: #3a3a3c;
            margin: 4px 8px;
        }
    """

    _TITLE_LABEL_STYLE = """
        QLabel {
            background-color: transparent;
            color: #f5f5f7;
            font-size: 13px;
            font-weight: 600;
            font-family: 'Segoe UI', sans-serif;
        }
    """

    _WINDOW_BUTTON_STYLE = """
        QPushButton {
            background-color: transparent;
            border: none;
            color: #8e8e93;
            font-size: 14px;
            font-family: 'Segoe UI', sans-serif;
        }
        QPushButton:hover {
            background-color: #3a3a3c;
            color: #ffffff;
        }
    """

    _CLOSE_BUTTON_STYLE = """
        QPushButton {
            background-color: transparent;
            border: none;
            color: #8e8e93;
            font-size: 14px;
            font-family: 'Segoe UI', sans-serif;
        }
        QPushButton:hover {
            background-color: #ff453a;
            color: #ffffff;
        }
    """

    _TITLE_BAR_STYLE = """
        #customTitleBar {
            background-color: #2c2c2e;
            border-bottom: 1px solid #3a3a3c;
        }
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_window = parent
        self._drag_position = None
        self._is_maximized = False
        self._normal_geometry = None
        self.setFixedHeight(32)
        self.setObjectName("customTitleBar")
        self.setAutoFillBackground(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(0)

        self._build_menu_bar(layout)
        layout.addStretch()
        self._build_title_label(layout)
        layout.addStretch()
        self._build_window_buttons(layout)

        self.setStyleSheet(self._TITLE_BAR_STYLE)

    def _build_menu_bar(self, layout: QHBoxLayout) -> None:
        from PyQt6.QtWidgets import QMenuBar
        self.menu_bar = QMenuBar()
        self.menu_bar.setStyleSheet(self._MENU_BAR_STYLE)
        layout.addWidget(self.menu_bar)

    def _build_title_label(self, layout: QHBoxLayout) -> None:
        self.title_label = QLabel("OpenWhisper")
        self.title_label.setStyleSheet(self._TITLE_LABEL_STYLE)
        layout.addWidget(self.title_label)

    def _build_window_buttons(self, layout: QHBoxLayout) -> None:
        self.minimize_btn = QPushButton("─")
        self.minimize_btn.setFixedSize(46, 32)
        self.minimize_btn.setStyleSheet(self._WINDOW_BUTTON_STYLE)
        self.minimize_btn.setToolTip("Minimize")
        self.minimize_btn.clicked.connect(self._minimize)

        self.maximize_btn = QPushButton("□")
        self.maximize_btn.setFixedSize(46, 32)
        self.maximize_btn.setStyleSheet(self._WINDOW_BUTTON_STYLE)
        self.maximize_btn.setToolTip("Maximize")
        self.maximize_btn.clicked.connect(self._toggle_maximize)

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(46, 32)
        self.close_btn.setStyleSheet(self._CLOSE_BUTTON_STYLE)
        self.close_btn.setToolTip("Close")
        self.close_btn.clicked.connect(self._close)

        layout.addWidget(self.minimize_btn)
        layout.addWidget(self.maximize_btn)
        layout.addWidget(self.close_btn)

    def _minimize(self):
        if self.parent_window:
            self.parent_window.showMinimized()

    def _toggle_maximize(self):
        if self.parent_window:
            if getattr(self.parent_window, "_compact_mode", False):
                return
            if self._is_maximized:
                if self._normal_geometry:
                    self.parent_window.setGeometry(self._normal_geometry)
                self.maximize_btn.setText("□")
                self.maximize_btn.setToolTip("Maximize")
            else:
                self._normal_geometry = self.parent_window.geometry()
                self.parent_window.showMaximized()
                self.maximize_btn.setText("❐")
                self.maximize_btn.setToolTip("Restore")
            self._is_maximized = not self._is_maximized

    def _close(self):
        if self.parent_window:
            self.parent_window.close()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.parent_window:
            global_pos = event.globalPosition().toPoint()
            local_pos = self.parent_window.mapFromGlobal(global_pos)
            edge = self.parent_window._get_resize_edge(local_pos)
            if edge != (0, 0):
                self.parent_window._begin_resize(edge, global_pos)
                event.accept()
                return
            self._drag_position = global_pos - self.parent_window.frameGeometry().topLeft()
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.parent_window and self.parent_window._resizing:
            self.parent_window._apply_resize_delta(event.globalPosition().toPoint())
            event.accept()
            return
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_position and self.parent_window:
            self.parent_window.move(event.globalPosition().toPoint() - self._drag_position)
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.parent_window:
            self._drag_position = None
            if self.parent_window._resizing:
                self.parent_window._finish_resize()
                event.accept()
                return

        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()


from ui_qt.widgets import (
    Button,
    HistorySidebar, HistoryEdgeTab, HotkeyHintFilter,
    TabbedContentWidget, QuickRecordTab, UploadFileTab, MeetingModeTab,
    CompactRecordController,
)
from services.history_manager import history_manager
from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog


class MainWindow(QMainWindow):
    # Window-local keyboard shortcuts. Distinct from the global hotkeys in
    # config.DEFAULT_HOTKEYS, which work even when the app is unfocused.
    HISTORY_SHORTCUT = "Ctrl+H"
    COMPACT_SHORTCUT = "Ctrl+Shift+C"
    QUIT_SHORTCUT = "Ctrl+Q"

    record_toggled = pyqtSignal(bool)
    record_canceled = pyqtSignal()
    model_changed = pyqtSignal(str)
    whisper_engine_changed = pyqtSignal()  # Local engine (model/device/quant) changed
    settings_requested = pyqtSignal()
    model_manager_requested = pyqtSignal(str)
    hotkeys_requested = pyqtSignal()
    about_requested = pyqtSignal()
    check_for_updates_requested = pyqtSignal()
    retranscribe_requested = pyqtSignal(str)  # audio_path
    upload_file_requested = pyqtSignal(str, float)
    upload_copy_requested = pyqtSignal(str)
    meeting_dashboard_requested = pyqtSignal()
    past_meeting_requested = pyqtSignal(str)
    past_meeting_copy_requested = pyqtSignal(str)
    past_meeting_delete_requested = pyqtSignal(str, bool)
    past_meetings_clear_requested = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("OpenWhisper")

        # Keep the explicit Window type flag: setWindowFlags() replaces *all*
        # flags, and a bare FramelessWindowHint drops the top-level Window type.
        # On macOS that produces an NSWindow that fails to order back to the
        # front after hide() (i.e. can't be restored from the tray); on Windows
        # it happens to work either way. Including Window is safe on both.
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        )
        self.setMinimumSize(
            config.MAIN_WINDOW_MIN_WIDTH,
            config.MAIN_WINDOW_MIN_HEIGHT,
        )
        self.setMaximumWidth(config.MAIN_WINDOW_MAX_WIDTH)
        self.resize(
            config.MAIN_WINDOW_DEFAULT_WIDTH,
            config.MAIN_WINDOW_DEFAULT_HEIGHT,
        )

        self.is_recording = False
        self.current_model = config.MODEL_CHOICES[0]
        self._force_quit = False
        self._initial_show_complete = False
        self._compact_mode = False
        self._full_geometry = None

        self._collapsed_width = config.MAIN_WINDOW_DEFAULT_WIDTH
        self._sidebar_width = config.MAIN_WINDOW_HISTORY_SIDEBAR_WIDTH
        self._geometry_format = "collapsed_content_v1"

        # Height actually reclaimed by the last transcription collapse, so the
        # matching expand restores exactly that much (see _on_transcription_collapsed).
        self._collapse_freed_height = 0

        self._current_tab_index = TabbedContentWidget.TAB_QUICK_RECORD
        self._transcription_tab_height = config.MAIN_WINDOW_DEFAULT_HEIGHT

        self._meeting_height_timer = QTimer(self)
        self._meeting_height_timer.setSingleShot(True)
        self._meeting_height_timer.timeout.connect(self._sync_meeting_mode_height)
        self._meeting_intro_scheduled = False

        self._resize_margin = 8
        self._resizing = False
        self._resize_edge = None
        self._resize_start_pos = None
        self._resize_start_geometry = None

        self._geometry_save_timer = None
        self._tab_history_refresh_timer = QTimer(self)
        self._tab_history_refresh_timer.setSingleShot(True)
        self._tab_history_refresh_timer.timeout.connect(
            self._refresh_history_sidebar_if_expanded
        )

        self.on_show_copied_animation: Optional[Callable] = None

        self._setup_ui()
        self._setup_menu()
        self._load_saved_settings()
        self._restore_window_geometry()
        self._restore_compact_mode()

        self.setMouseTracking(True)
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)

    def _setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        central_widget.setStyleSheet("""
            QWidget#centralWidget {
                border: 1px solid #3a3a3c;
            }
        """)
        central_widget.setObjectName("centralWidget")
        central_widget.setMouseTracking(True)

        outer_layout = QVBoxLayout(central_widget)
        outer_layout.setContentsMargins(1, 1, 1, 1)  # 1px margin for border visibility
        outer_layout.setSpacing(0)

        self.title_bar = CustomTitleBar(self)
        outer_layout.addWidget(self.title_bar)

        content_wrapper = QWidget()
        root_layout = QHBoxLayout(content_wrapper)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        outer_layout.addWidget(content_wrapper, stretch=1)

        main_area = QWidget()
        main_area_layout = QVBoxLayout(main_area)
        main_area_layout.setContentsMargins(0, 0, 0, 0)
        main_area_layout.setSpacing(0)

        self.tabbed_content = TabbedContentWidget()
        self.quick_record_tab = QuickRecordTab()

        self.tabbed_content.add_tab(self.quick_record_tab, "Quick Record")

        self.upload_file_tab = UploadFileTab()
        self.tabbed_content.add_tab(self.upload_file_tab, "Upload File")

        self.meeting_mode_tab = MeetingModeTab()
        self.meeting_mode_tab.content_height_changed.connect(
            self._schedule_meeting_mode_height_sync
        )
        self.tabbed_content.add_tab(self.meeting_mode_tab, "Meeting Mode")

        self.transcription_tabs = (self.quick_record_tab, self.upload_file_tab)

        # Sync the stack with the tab bar after all tabs have been added
        # (fixes timing issue where tab bar index is restored before stack has widgets)
        self.tabbed_content.sync_stack_with_tab_bar()

        self.compact_controller = CompactRecordController()
        self.compact_controller.hide()
        self.compact_controller.record_requested.connect(
            self.quick_record_tab.record_button.click
        )
        self.compact_controller.stop_requested.connect(
            self.quick_record_tab.stop_button.click
        )
        self.compact_controller.cancel_requested.connect(
            self.quick_record_tab.cancel_button.click
        )

        self.tabbed_content.tab_changed.connect(self._on_tab_changed)

        for tab in self.transcription_tabs:
            tab.model_changed.connect(self._on_model_changed)
            tab.engine_settings_changed.connect(self._on_engine_settings_changed)
            tab.manage_models_requested.connect(
                lambda: self.model_manager_requested.emit("downloads")
            )
            tab.transcription_collapsed.connect(self._on_transcription_collapsed)
            tab.stats_widget.visibility_changed.connect(self._on_stats_visibility_changed)

        self.quick_record_tab.record_toggled.connect(self._on_quick_record_toggled)
        self.quick_record_tab.record_canceled.connect(self._on_quick_record_canceled)
        self.upload_file_tab.upload_requested.connect(self._on_upload_file_transcribe)
        self.upload_file_tab.copy_requested.connect(self.upload_copy_requested.emit)

        main_area_layout.addWidget(self.tabbed_content)
        main_area_layout.addWidget(self.compact_controller)

        root_layout.addWidget(main_area, stretch=1)

        self.history_edge_tab = HistoryEdgeTab()
        self.history_edge_tab.set_shortcut_hint(self.HISTORY_SHORTCUT)
        self.history_edge_tab.clicked.connect(self.toggle_history)
        root_layout.addWidget(self.history_edge_tab)

        self.history_sidebar = HistorySidebar()
        self.history_sidebar.entry_selected.connect(self._on_history_entry_selected)
        self.history_sidebar.entry_copied.connect(self._on_history_entry_copied)
        self.history_sidebar.entry_deleted.connect(self._on_history_entry_deleted)
        self.history_sidebar.retranscribe_requested.connect(self._on_retranscribe_requested)
        self.history_sidebar.past_meeting_selected.connect(
            self.past_meeting_requested.emit
        )
        self.history_sidebar.past_meeting_copy_requested.connect(
            self.past_meeting_copy_requested.emit
        )
        self.history_sidebar.past_meeting_delete_requested.connect(
            self.past_meeting_delete_requested.emit
        )
        self.history_sidebar.past_meetings_clear_requested.connect(
            self.past_meetings_clear_requested.emit
        )
        self.history_sidebar.width_animated.connect(self._on_sidebar_width_animated)
        self.history_sidebar.animation.finished.connect(
            self._on_sidebar_animation_finished
        )
        root_layout.addWidget(self.history_sidebar)

        # Sync the sidebar with the restored tab (must be after history_sidebar is created)
        self._on_tab_changed(self.tabbed_content.current_index())

        self._build_footer(outer_layout)

    _FOOTER_BAR_STYLE = """
        QWidget#footerBar {
            background-color: #1c1c1e;
            border-top: 1px solid #2c2c2e;
        }
    """

    _MODELS_BUTTON_STYLE = """
        QPushButton#modelsButton {
            background-color: #2c2c2e;
            color: #30d158;
            border: 1px solid #3a3a3c;
            border-radius: 8px;
            padding: 6px 18px;
            font-weight: 600;
            font-size: 13px;
        }
        QPushButton#modelsButton:hover {
            background-color: #30d158;
            color: #ffffff;
            border: 1px solid #30d158;
        }
        QPushButton#modelsButton:pressed {
            background-color: #248a3d;
            color: #ffffff;
        }
    """

    _TRAY_BUTTON_STYLE = """
        QPushButton#trayButton {
            background-color: #2c2c2e;
            color: #e5e5e7;
            border: 1px solid #3a3a3c;
            border-radius: 8px;
            padding: 6px 18px;
            font-weight: 600;
            font-size: 13px;
        }
        QPushButton#trayButton:hover {
            background-color: #0a84ff;
            color: #ffffff;
            border: 1px solid #0a84ff;
        }
        QPushButton#trayButton:pressed {
            background-color: #0060df;
            color: #ffffff;
        }
    """

    _COMPACT_BUTTON_STYLE = """
        QPushButton#compactButton {
            background-color: #2c2c2e;
            color: #64d2ff;
            border: 1px solid #3a3a3c;
            border-radius: 8px;
            padding: 6px 18px;
            font-weight: 600;
            font-size: 13px;
        }
        QPushButton#compactButton:hover {
            background-color: #0a84ff;
            color: #ffffff;
            border: 1px solid #0a84ff;
        }
        QPushButton#compactButton:pressed {
            background-color: #0060df;
            color: #ffffff;
        }
    """

    _QUIT_BUTTON_STYLE = """
        QPushButton#quitButton {
            background-color: #2c2c2e;
            color: #ff453a;
            border: 1px solid #3a3a3c;
            border-radius: 8px;
            padding: 6px 18px;
            font-weight: 600;
            font-size: 13px;
        }
        QPushButton#quitButton:hover {
            background-color: #ff453a;
            color: #ffffff;
            border: 1px solid #ff453a;
        }
        QPushButton#quitButton:pressed {
            background-color: #d70015;
            color: #ffffff;
        }
    """

    def _build_footer(self, outer_layout: QVBoxLayout) -> None:
        self.footer = QWidget()
        self.footer.setObjectName("footerBar")
        self.footer.setFixedHeight(48)
        self.footer.setStyleSheet(self._FOOTER_BAR_STYLE)

        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(16, 7, 16, 7)
        footer_layout.setSpacing(0)
        footer_layout.addStretch()

        self.models_button = QPushButton("Model Manager")
        self.models_button.setObjectName("modelsButton")
        self.models_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.models_button.setFixedHeight(34)
        self.models_button.setMinimumWidth(130)
        self.models_button.setStyleSheet(self._MODELS_BUTTON_STYLE)
        self.models_button.setToolTip(
            "Browse, download, and activate voice and text models"
        )
        self.models_button.clicked.connect(self.open_model_manager)
        footer_layout.addWidget(self.models_button)

        footer_layout.addSpacing(10)

        self.tray_button = Button("Minimize to Tray")
        self.tray_button.setObjectName("trayButton")
        self.tray_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tray_button.setFixedHeight(34)
        self.tray_button.setMinimumWidth(140)
        self.tray_button.setStyleSheet(self._TRAY_BUTTON_STYLE)
        self.tray_button.set_hotkey(
            format_hotkey_display(config.DEFAULT_HOTKEYS["minimize_tray"])
        )
        self.tray_button.clicked.connect(self.minimize_to_tray)
        footer_layout.addWidget(self.tray_button)

        footer_layout.addSpacing(10)

        self.compact_button = QPushButton("Compact")
        self.compact_button.setObjectName("compactButton")
        self.compact_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.compact_button.setFixedHeight(34)
        self.compact_button.setMinimumWidth(100)
        self.compact_button.setStyleSheet(self._COMPACT_BUTTON_STYLE)
        HotkeyHintFilter(self.compact_button, self.COMPACT_SHORTCUT)
        self.compact_button.clicked.connect(self.toggle_compact_mode)
        footer_layout.addWidget(self.compact_button)

        footer_layout.addSpacing(10)

        self.quit_button = QPushButton("Quit")
        self.quit_button.setObjectName("quitButton")
        self.quit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.quit_button.setFixedHeight(34)
        self.quit_button.setMinimumWidth(100)
        self.quit_button.setStyleSheet(self._QUIT_BUTTON_STYLE)
        HotkeyHintFilter(self.quit_button, self.QUIT_SHORTCUT)
        self.quit_button.clicked.connect(self.quit_application)
        footer_layout.addWidget(self.quit_button)

        footer_layout.addStretch()

        outer_layout.addWidget(self.footer)

    def _setup_menu(self):
        self.menuBar().hide()

        menubar = self.title_bar.menu_bar

        file_menu = menubar.addMenu("File")
        # Qt auto-assigns PreferencesRole to actions named "Settings", which
        # rewrites the label to "Preferences" on Windows. Keep our wording.
        settings_action = file_menu.addAction("Settings", self.open_settings)
        settings_action.setMenuRole(QAction.MenuRole.NoRole)
        models_action = file_menu.addAction("Model Manager...", self.open_model_manager)
        models_action.setMenuRole(QAction.MenuRole.NoRole)
        downloads_action = file_menu.addAction("Downloads...", self.open_downloads)
        downloads_action.setMenuRole(QAction.MenuRole.NoRole)
        file_menu.addAction("Hotkeys", self.open_hotkey_settings)
        file_menu.addSeparator()
        file_menu.addAction("Minimize to Tray", self.minimize_to_tray)
        quit_action = file_menu.addAction(
            "Quit" if sys.platform == "darwin" else "Exit", self.quit_application
        )
        quit_action.setMenuRole(QAction.MenuRole.NoRole)
        quit_action.setShortcut(QKeySequence(self.QUIT_SHORTCUT))

        view_menu = menubar.addMenu("View")
        sidebar_name = (
            "Past Meetings"
            if self.tabbed_content.current_index() == TabbedContentWidget.TAB_MEETING_MODE
            else "History"
        )
        self.sidebar_action = view_menu.addAction(sidebar_name, self.toggle_history)
        self.sidebar_action.setShortcut(QKeySequence(self.HISTORY_SHORTCUT))
        compact_action = view_menu.addAction("Compact Mode", self.toggle_compact_mode)
        compact_action.setShortcut(QKeySequence(self.COMPACT_SHORTCUT))
        view_menu.addSeparator()
        view_menu.addAction(
            "Open Meeting Dashboard", self.meeting_dashboard_requested.emit
        )

        help_menu = menubar.addMenu("Help")
        updates_action = help_menu.addAction(
            "Check for Updates...", self.check_for_updates
        )
        updates_action.setMenuRole(QAction.MenuRole.NoRole)
        about_action = help_menu.addAction("About", self.show_about)
        about_action.setMenuRole(QAction.MenuRole.NoRole)

    def _load_saved_settings(self):
        try:
            saved_model = settings_manager.load_model_selection()
            for tab in self.transcription_tabs:
                tab.set_model_selection(saved_model)
            self.current_model = self.quick_record_tab.current_model
            self._apply_local_engine_visibility(self.current_model)
            logger.info(f"Loaded saved model selection: {saved_model}")
        except Exception as e:
            logger.error(f"Failed to load saved settings: {e}")

    def _on_tab_changed(self, index: int):
        logger.debug(f"Tab changed to index {index}")

        if self._compact_mode and index != TabbedContentWidget.TAB_QUICK_RECORD:
            self.set_compact_mode(False)

        prev_index = getattr(
            self, "_current_tab_index", TabbedContentWidget.TAB_QUICK_RECORD
        )
        self._current_tab_index = index

        meeting_mode = index == TabbedContentWidget.TAB_MEETING_MODE
        self.history_sidebar.set_meeting_mode(meeting_mode)
        panel_name = "Past Meetings" if meeting_mode else "History"
        self.history_edge_tab.set_panel_name(panel_name)
        if hasattr(self, "sidebar_action"):
            self.sidebar_action.setText(panel_name)

        self._schedule_history_sidebar_refresh()

        if prev_index != index:
            if (
                prev_index != TabbedContentWidget.TAB_MEETING_MODE
                and meeting_mode
            ):
                if not self._compact_mode and self.isVisible():
                    self._transcription_tab_height = self.height()
                    self._schedule_meeting_mode_height_sync()
            elif (
                prev_index == TabbedContentWidget.TAB_MEETING_MODE
                and not meeting_mode
            ):
                if not self._compact_mode and self.isVisible():
                    target = (
                        self._transcription_tab_height
                        or config.MAIN_WINDOW_DEFAULT_HEIGHT
                    )
                    self._animate_resize(self.width(), target)
            elif meeting_mode:
                self._schedule_meeting_mode_height_sync()
        elif meeting_mode:
            self._schedule_meeting_mode_height_sync()

        if meeting_mode:
            self._schedule_meeting_mode_intro()

    def _schedule_meeting_mode_intro(self) -> None:
        """Queue the first-visit Meeting Mode overview after the tab is up."""
        if self._meeting_intro_scheduled:
            return
        if self.tabbed_content.current_index() != (
            TabbedContentWidget.TAB_MEETING_MODE
        ):
            return
        if self.tabbed_content.meeting_tab_is_locked():
            return
        if resolve_meeting_mode_intro_seen():
            return
        self._meeting_intro_scheduled = True
        QTimer.singleShot(0, self._maybe_show_meeting_mode_intro)

    def _maybe_show_meeting_mode_intro(self) -> None:
        self._meeting_intro_scheduled = False
        if not self.isVisible():
            return
        if self.tabbed_content.current_index() != (
            TabbedContentWidget.TAB_MEETING_MODE
        ):
            return
        if self.tabbed_content.meeting_tab_is_locked():
            return
        if resolve_meeting_mode_intro_seen():
            return
        # Tray / hotkey start switches to this tab after capture is live.
        if self.meeting_mode_tab.is_meeting_active:
            return
        from ui_qt.dialogs.meeting_intro_dialog import (
            maybe_show_meeting_mode_intro,
        )

        maybe_show_meeting_mode_intro(self)

    def _schedule_history_sidebar_refresh(self) -> None:
        """Defer visible sidebar refreshes so tab clicks stay responsive."""
        if not self.history_sidebar.is_expanded:
            return

        self._tab_history_refresh_timer.start(75)

    def _refresh_history_sidebar_if_expanded(self) -> None:
        if self.history_sidebar.is_expanded:
            self.history_sidebar.refresh()

    def _on_quick_record_toggled(self, is_recording: bool):
        """Forward a record/stop request; chrome follows recording_state_changed."""
        self.record_toggled.emit(is_recording)

    def _on_quick_record_canceled(self):
        self.is_recording = False
        self.compact_controller.set_recording_state(False)
        self.compact_controller.set_status("Ready to record")
        if not self.meeting_mode_tab.is_meeting_active:
            self.tabbed_content.set_recording_state(False, -1)

        self.record_canceled.emit()

    def _on_model_changed(self, model_name: str):
        self.current_model = model_name

        # Sync the other tabs without re-emitting the signal
        for tab in self.transcription_tabs:
            if tab.current_backend() != model_name:
                tab.set_backend(model_name)

        self._apply_local_engine_visibility(model_name)

        self.model_changed.emit(model_name)

    def _apply_local_engine_visibility(self, model_name: str):
        is_local = config.MODEL_VALUE_MAP.get(model_name) == "local_whisper"
        for tab in self.transcription_tabs:
            tab.set_local_engine_visible(is_local)

    def _on_engine_settings_changed(self):
        """Keep both tabs' engine panels in sync, then notify listeners.

        The emitting widget has already persisted the three keys to settings, so
        both panels reload from that canonical source (signals blocked inside
        ``load_from_settings``). This avoids depending on ``sender()`` identity
        and guarantees the two tabs always agree. ``whisper_engine_changed`` then
        triggers the controller's background reload.
        """
        for tab in self.transcription_tabs:
            tab.local_engine.load_from_settings()
        self.whisper_engine_changed.emit()

    def _on_upload_file_transcribe(
        self, audio_path: str, duration_seconds: float = 0.0
    ):
        self.upload_file_requested.emit(audio_path, duration_seconds)

    def _update_recording_state(self):
        self.quick_record_tab.is_recording = self.is_recording
        self.quick_record_tab._update_recording_state()
        self.compact_controller.set_recording_state(self.is_recording)
        self.compact_controller.set_status(
            "Recording in progress..." if self.is_recording else "Ready to record"
        )

        if self.is_recording:
            self.tabbed_content.set_recording_state(True, TabbedContentWidget.TAB_QUICK_RECORD)
        elif not self.meeting_mode_tab.is_meeting_active:
            self.tabbed_content.set_recording_state(False, -1)

    def set_status(self, status_text: str):
        self.quick_record_tab.set_status(status_text)
        self.compact_controller.set_status(status_text)

    def set_device_info(self, device_info: str, ready: Optional[bool] = None):
        for tab in self.transcription_tabs:
            tab.set_device_info(device_info, ready)

    def set_transcript(self, text: str, raw=None):
        self.quick_record_tab.set_transcript(text, raw=raw)

    def append_transcription(self, text: str):
        self.quick_record_tab.append_transcription(text)

    def clear_transcription(self):
        self.quick_record_tab.clear_transcription()

    def set_partial_transcription(self, text: str, is_final: bool):
        self.quick_record_tab.set_partial_transcription(text, is_final)

    def clear_partial_transcription(self):
        self.quick_record_tab.clear_partial_transcription()

    def set_transcription_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int
    ):
        self.quick_record_tab.set_transcription_stats(
            transcription_time, audio_duration, file_size
        )

    def clear_transcription_stats(self):
        self.quick_record_tab.clear_transcription_stats()

    def _on_transcription_collapsed(self, collapsed: bool, delta: int):
        """Reclaim/restore window height when the transcription card toggles.

        Keeps both tabs in the same collapsed state, then animates the window
        height by the freed (or restored) body height so the change feels smooth.

        Args:
            collapsed: True if the card was just collapsed, False if expanded.
            delta: The body height that was hidden/shown, in pixels.
        """
        source = self.sender()
        for tab in self.transcription_tabs:
            if tab is not source:
                tab.set_transcription_collapsed(collapsed)

        current_height = self.height()
        if collapsed:
            if delta <= 0:
                return
            # Shrink by the body height the card gave up, clamped to the floor.
            # Record how much we ACTUALLY freed (the clamp may free less than
            # `delta`) so the matching expand restores precisely that amount.
            # Adding back the raw, elastic body height instead would overshoot
            # the original height and compound on every toggle — the runaway
            # "window keeps getting taller" bug.
            new_height = max(config.MAIN_WINDOW_MIN_HEIGHT, current_height - delta)
            self._collapse_freed_height = current_height - new_height
            self._transcription_tab_height = new_height
            self._animate_resize(self.width(), new_height)
        else:
            # Give back exactly what the matching collapse reclaimed. If we have
            # no tracked collapse this session (e.g. the app launched already
            # collapsed), grow once toward the default height instead.
            restore = self._collapse_freed_height
            self._collapse_freed_height = 0
            if restore > 0:
                target_height = current_height + restore
            elif current_height < config.MAIN_WINDOW_TRANSCRIPTION_EXPAND_HEIGHT:
                target_height = config.MAIN_WINDOW_TRANSCRIPTION_EXPAND_HEIGHT
            else:
                target_height = current_height
            self._transcription_tab_height = target_height
            self._animate_resize(self.width(), target_height)

    def _schedule_meeting_mode_height_sync(self) -> None:
        """Queue a Meeting Mode height check for the next event-loop pass.

        Deferring keeps the check off the state-update path and coalesces the
        bursts of updates the finalization pipeline sends.
        """
        self._meeting_height_timer.start(0)

    def _calculate_meeting_mode_window_height(self) -> int:
        """Calculate the total window height needed to display Meeting Mode content."""
        from PyQt6.QtCore import QEvent
        from PyQt6.QtWidgets import QApplication

        QApplication.sendPostedEvents(None, QEvent.Type.LayoutRequest)

        chrome = self.height() - self.tabbed_content.stack.height()
        if chrome <= 0:
            chrome = 154
        page_height = self._meeting_mode_page_needed_height()
        return max(
            config.MAIN_WINDOW_MIN_HEIGHT,
            min(chrome + page_height, self._max_usable_height()),
        )

    def _meeting_mode_page_needed_height(self) -> int:
        if hasattr(self.meeting_mode_tab, "content_height"):
            return self.meeting_mode_tab.content_height()
        return self.meeting_mode_tab.sizeHint().height()

    def _sync_meeting_mode_height(self) -> None:
        """Hold and smoothly adjust the window height for the Meeting Mode page.

        Its finalization card grows as the pipeline reports step rows, and
        switches back to idle once completed or dismissed. When Meeting Mode
        is selected, the window smoothly animates to fit the visible content
        and controls without scrolling when possible.
        """
        if (
            self._compact_mode
            or not self.isVisible()
            or self.tabbed_content.current_index() != TabbedContentWidget.TAB_MEETING_MODE
            or self._resizing
        ):
            return

        target_height = self._calculate_meeting_mode_window_height()
        if abs(target_height - self.height()) > 1:
            self._animate_resize(self.width(), target_height)

    def _max_usable_height(self) -> int:
        from PyQt6.QtWidgets import QApplication

        screen = (
            QApplication.screenAt(self.geometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return UNLIMITED_HEIGHT
        return screen.availableGeometry().height()

    def _on_stats_visibility_changed(self, visible: bool):
        stats_height = 60 if visible else 0
        current_height = self.height()

        if visible:
            new_height = current_height + stats_height
        else:
            new_height = max(
                config.MAIN_WINDOW_MIN_HEIGHT,
                current_height - stats_height,
            )

        self._transcription_tab_height = new_height
        self._animate_resize(self.width(), new_height)

    def open_settings(self):
        logger.info("Opening settings dialog")
        self.settings_requested.emit()

    def open_model_manager(self):
        logger.info("Opening model manager")
        self.model_manager_requested.emit("ondemand")

    def open_downloads(self):
        logger.info("Opening downloads")
        self.model_manager_requested.emit("downloads")

    def open_hotkey_settings(self):
        logger.info("Opening hotkey settings")
        self.hotkeys_requested.emit()

    def check_for_updates(self):
        logger.info("Check for updates requested")
        self.check_for_updates_requested.emit()

    def show_about(self):
        logger.info("Showing about dialog")
        self.about_requested.emit()

    def minimize_to_tray(self):
        logger.info("Minimizing to tray")
        self.hide()

    def toggle_compact_mode(self) -> None:
        self.set_compact_mode(not self._compact_mode)

    def set_compact_mode(self, compact: bool, persist: bool = True) -> None:
        """Apply compact or full main-window mode.

        Args:
            compact: Whether to show the compact recording controller.
            persist: Whether to save the selected mode to settings.
        """
        if compact == self._compact_mode:
            return

        if (
            hasattr(self, "_resize_animation")
            and self._resize_animation.state() == QPropertyAnimation.State.Running
        ):
            self._resize_animation.stop()

        if compact:
            if self.title_bar._is_maximized:
                self.title_bar._toggle_maximize()
            elif self.isMaximized():
                self.showNormal()

            self._full_geometry = QRect(self.geometry())
            self._save_geometry()
            self._compact_mode = True

            self.tabbed_content.hide()
            self.compact_controller.show()
            self.history_edge_tab.hide()
            self.history_sidebar.hide()
            self.title_bar.title_label.hide()
            self.title_bar.maximize_btn.hide()
            self.models_button.hide()
            self.compact_button.setText("Full Size")

            self.setMinimumSize(0, 0)
            self.setMaximumSize(UNLIMITED_HEIGHT, UNLIMITED_HEIGHT)
            self.setFixedSize(
                config.MAIN_WINDOW_COMPACT_WIDTH,
                config.MAIN_WINDOW_COMPACT_HEIGHT,
            )
            self._restore_compact_geometry()
        else:
            self._save_compact_geometry()
            self._compact_mode = False

            self.setMinimumSize(
                config.MAIN_WINDOW_MIN_WIDTH,
                config.MAIN_WINDOW_MIN_HEIGHT,
            )
            self.setMaximumSize(config.MAIN_WINDOW_MAX_WIDTH, UNLIMITED_HEIGHT)
            self.compact_controller.hide()
            self.tabbed_content.show()
            self.history_edge_tab.show()
            self.history_sidebar.show()
            self.title_bar.title_label.show()
            self.title_bar.maximize_btn.show()
            self.models_button.show()
            self.compact_button.setText("Compact")

            if self._full_geometry is not None:
                self.setGeometry(self._full_geometry)
            else:
                self._restore_window_geometry()

            self._schedule_meeting_mode_height_sync()

        if persist:
            try:
                settings_manager.save_setting(SettingsKey.COMPACT_MODE, compact)
            except Exception as e:
                logger.warning(f"Failed to save compact mode: {e}")

    def _restore_compact_mode(self) -> None:
        try:
            if settings_manager.get(SettingsKey.COMPACT_MODE, False) is True:
                self.set_compact_mode(True, persist=False)
        except Exception as e:
            logger.warning(f"Failed to restore compact mode: {e}")

    def _save_compact_geometry(self) -> None:
        geo = self.geometry()
        try:
            settings_manager.save_setting(
                SettingsKey.COMPACT_WINDOW_GEOMETRY,
                {"x": geo.x(), "y": geo.y()},
            )
        except Exception as e:
            logger.warning(f"Failed to save compact window geometry: {e}")

    def _restore_compact_geometry(self) -> None:
        x = self.x()
        y = self.y()
        try:
            geo = settings_manager.get(SettingsKey.COMPACT_WINDOW_GEOMETRY)
            if isinstance(geo, dict) and {"x", "y"}.issubset(geo):
                x = int(geo["x"])
                y = int(geo["y"])
        except (TypeError, ValueError) as e:
            logger.warning(f"Invalid compact window geometry: {e}")

        from PyQt6.QtWidgets import QApplication

        screen = QApplication.screenAt(self.geometry().center()) or QApplication.primaryScreen()
        if screen:
            available = screen.availableGeometry()
            x = min(max(x, available.left()), available.right() - self.width() + 1)
            y = min(max(y, available.top()), available.bottom() - self.height() + 1)
        self.move(x, y)

    def toggle_tray_visibility(self):
        if self.isVisible() and not self.isMinimized():
            self.minimize_to_tray()
            return

        self.restore_from_tray()

    def restore_from_tray(self):
        """Reliably bring the window back from the tray / hidden state.

        macOS needs the full clear-minimized + show + raise + activate
        sequence: once an app has no visible windows it is deactivated, so a
        bare showNormal() can leave the window hidden behind other apps (or not
        appear at all). The sequence is harmless on Windows, which restores fine
        from showNormal() alone.
        """
        logger.info("Restoring window from tray")
        # Drop any minimized bit and mark the window active before showing.
        self.setWindowState(
            (self.windowState() & ~Qt.WindowState.WindowMinimized)
            | Qt.WindowState.WindowActive
        )
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_application(self):
        logger.info("Quitting application")
        self._save_geometry()
        self._force_quit = True
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().quit()

    def toggle_history(self):
        logger.info("Toggling contextual sidebar")

        if self._compact_mode:
            self.set_compact_mode(False)
            if self.history_sidebar.is_expanded:
                return

        will_be_expanded = not self.history_sidebar.is_expanded
        self.history_edge_tab.set_expanded(will_be_expanded)

        # A running height animation would fight the per-frame lockstep resize.
        if (
            hasattr(self, '_resize_animation')
            and self._resize_animation.state() == QPropertyAnimation.State.Running
        ):
            self._resize_animation.stop()

        # Capture the width of everything except the sidebar so each animation
        # frame can hold the main content area at a constant width. Works
        # mid-animation too: the sidebar's current width is subtracted out.
        self._sidebar_base_width = self.width() - self.history_sidebar.width()
        self._collapsed_width = max(self.minimumWidth(), self._sidebar_base_width)

        # The sidebar's single animation drives the window width via
        # width_animated -> _on_sidebar_width_animated.
        self.history_sidebar.toggle()

    def _on_sidebar_width_animated(self, sidebar_width: int):
        """Resize the window in lockstep with the sidebar width animation.

        Args:
            sidebar_width: Current animated width of the history sidebar.
        """
        base = getattr(self, '_sidebar_base_width', None)
        if base is None:
            return

        target_width = min(self.maximumWidth(), base + sidebar_width)
        geo = self.geometry()
        self.setGeometry(
            self._clamp_geometry(geo.x(), geo.y(), target_width, geo.height())
        )

    def _available_screen_rect(self) -> QRect:
        """Return the available geometry of the screen that contains this window."""
        from PyQt6.QtWidgets import QApplication

        screen = (
            QApplication.screenAt(self.frameGeometry().center())
            or QApplication.primaryScreen()
        )
        if screen is None:
            return QRect(0, 0, 1280, 800)
        return screen.availableGeometry()

    def _clamp_geometry(self, x: int, y: int, width: int, height: int) -> QRect:
        """Clamp a candidate window rect so it stays fully on the current screen.

        Args:
            x: Proposed left edge.
            y: Proposed top edge.
            width: Proposed width.
            height: Proposed height.

        Returns:
            A ``QRect`` that fits inside the available screen area.
        """
        avail = self._available_screen_rect()
        max_width = min(self.maximumWidth(), avail.width())
        max_height = avail.height()
        width = max(self.minimumWidth(), min(width, max_width))
        height = max(self.minimumHeight(), min(height, max_height))
        max_x = avail.x() + avail.width() - width
        max_y = avail.y() + avail.height() - height
        x = min(max(x, avail.x()), max(avail.x(), max_x))
        y = min(max(y, avail.y()), max(avail.y(), max_y))
        return QRect(x, y, width, height)

    def _center_on_screen(self) -> None:
        """Place the window in the center of the available screen area."""
        avail = self._available_screen_rect()
        self.setGeometry(
            self._clamp_geometry(
                avail.x() + (avail.width() - self.width()) // 2,
                avail.y() + (avail.height() - self.height()) // 2,
                self.width(),
                self.height(),
            )
        )

    def _on_sidebar_animation_finished(self) -> None:
        if (
            self.tabbed_content.current_index()
            != TabbedContentWidget.TAB_MEETING_MODE
        ):
            return
        self.meeting_mode_tab.updateGeometry()
        self.meeting_mode_tab.scroll_area.widget().updateGeometry()
        self.meeting_mode_tab.scroll_area.viewport().update()
        self._schedule_meeting_mode_height_sync()

    def _animate_resize(self, target_width: int, target_height: int):
        """Animate window resize.

        Args:
            target_width: Target window width.
            target_height: Target window height.
        """
        if not hasattr(self, '_resize_animation'):
            self._resize_animation = QPropertyAnimation(self, b"geometry")
            self._resize_animation.setDuration(SECTION_COLLAPSE_DURATION_MS)
            self._resize_animation.setEasingCurve(SECTION_COLLAPSE_EASING)

        current_geo = self.geometry()
        target_geo = self._clamp_geometry(
            current_geo.x(), current_geo.y(), target_width, target_height
        )

        # Continue smoothly from the current frame when interrupting a resize.
        if self._resize_animation.state() == QPropertyAnimation.State.Running:
            current_geo = self._resize_animation.currentValue()

        self._resize_animation.stop()
        self._resize_animation.setDuration(SECTION_COLLAPSE_DURATION_MS)
        self._resize_animation.setEasingCurve(SECTION_COLLAPSE_EASING)
        self._resize_animation.setStartValue(current_geo)
        self._resize_animation.setEndValue(target_geo)
        self._resize_animation.start()

    def refresh_history(self):
        self.history_sidebar.refresh()

    def refresh_past_meetings(self) -> None:
        if self.tabbed_content.current_index() == TabbedContentWidget.TAB_MEETING_MODE:
            self.history_sidebar.refresh()

    def _on_history_entry_selected(self, entry_id: str):
        entry = history_manager.get_entry_by_id(entry_id)
        if not entry:
            return

        dialog = HistoryEntryDialog(entry, parent=self)
        dialog.copied.connect(self._on_history_entry_copied_from_dialog)
        dialog.retranscribe_requested.connect(self._on_retranscribe_requested)
        dialog.delete_requested.connect(self._on_history_entry_delete_requested)
        dialog.exec()
        logger.info(f"Opened history entry dialog: {entry_id[:8]}...")

    def _on_history_entry_copied_from_dialog(self):
        self.set_status("Copied to clipboard")
        QTimer.singleShot(2000, lambda: self.set_status("Ready to record"))
        if self.on_show_copied_animation:
            self.on_show_copied_animation()

    def _on_history_entry_delete_requested(self, entry_id: str):
        if history_manager.delete_entry(entry_id):
            self.refresh_history()
            self._on_history_entry_deleted(entry_id)
            logger.info(f"Deleted history entry from dialog: {entry_id[:8]}...")

    def _on_history_entry_copied(self, entry_id: str):
        self.set_status("Copied to clipboard")
        QTimer.singleShot(2000, lambda: self.set_status("Ready to record"))

    def _on_history_entry_deleted(self, entry_id: str):
        self.set_status("Entry deleted")
        QTimer.singleShot(2000, lambda: self.set_status("Ready to record"))

    def _on_retranscribe_requested(self, audio_path: str):
        logger.info("Re-transcribe requested: %s", audio_path)
        self.retranscribe_requested.emit(audio_path)

    def closeEvent(self, event):
        logger.info("Main window closing")
        self.tabbed_content.flush_pending_tab_selection()
        if self._force_quit:
            logger.info("Force quit - closing application")
            event.accept()
            return

        try:
            settings = settings_manager.load_all_settings()
            minimize_tray = settings.get(SettingsKey.MINIMIZE_TRAY, True)  # Default to True
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            minimize_tray = True  # Default to True on error

        if minimize_tray:
            event.ignore()
            try:
                self.hide()
                logger.info("Window hidden to system tray")
            except Exception as e:
                logger.debug(f"Error hiding window: {e}")
                event.accept()
        else:
            event.accept()

    def update_hotkeys(
        self,
        record_key: str,
        cancel_key: str,
        enable_disable_key: str = "",
        minimize_key: str = "",
    ):
        """
        Update the hotkey display on buttons.

        Args:
            record_key: The key for recording
            cancel_key: The key for canceling
            enable_disable_key: The key for enabling/disabling STT
            minimize_key: The key for minimizing to the system tray
        """
        self.quick_record_tab.update_hotkeys(record_key, cancel_key, enable_disable_key)
        self.compact_controller.update_hotkeys(record_key, cancel_key)
        self.tray_button.set_hotkey(minimize_key)

    def _get_resize_edge(self, pos) -> tuple:
        """Determine which edge(s) the cursor is near.

        Args:
            pos: QPoint position relative to window.

        Returns:
            Tuple of (horizontal_edge, vertical_edge) where each is:
            -1 for left/top, 0 for none, 1 for right/bottom.
        """
        if self._compact_mode:
            return (0, 0)

        rect = self.rect()
        margin = self._resize_margin

        horizontal = 0  # -1 = left, 0 = none, 1 = right
        vertical = 0    # -1 = top, 0 = none, 1 = bottom

        if pos.x() <= margin:
            horizontal = -1
        elif pos.x() >= rect.width() - margin:
            horizontal = 1

        if pos.y() <= margin:
            vertical = -1
        elif pos.y() >= rect.height() - margin:
            vertical = 1

        return (horizontal, vertical)

    def _update_cursor_for_edge(self, edge: tuple):
        from PyQt6.QtGui import QCursor

        h, v = edge

        if h == 0 and v == 0:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif h != 0 and v == 0:
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif h == 0 and v != 0:
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif (h == -1 and v == -1) or (h == 1 and v == 1):
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:  # (h == -1 and v == 1) or (h == 1 and v == -1)
            self.setCursor(Qt.CursorShape.SizeBDiagCursor)

    def _begin_resize(self, edge: tuple, global_pos) -> None:
        self._resizing = True
        self._resize_edge = edge
        self._resize_start_pos = global_pos
        self._resize_start_geometry = self.geometry()

    def _apply_resize_delta(self, global_pos) -> None:
        if not self._resizing or not self._resize_edge or not self._resize_start_geometry:
            return

        delta = global_pos - self._resize_start_pos
        geo = self._resize_start_geometry
        h, v = self._resize_edge

        new_x = geo.x()
        new_y = geo.y()
        new_width = geo.width()
        new_height = geo.height()

        if h == -1:
            new_width = max(self.minimumWidth(), geo.width() - delta.x())
            new_x = geo.x() + geo.width() - new_width
        elif h == 1:
            new_width = min(self.maximumWidth(), max(self.minimumWidth(), geo.width() + delta.x()))

        if v == -1:
            new_height = max(self.minimumHeight(), geo.height() - delta.y())
            new_y = geo.y() + geo.height() - new_height
        elif v == 1:
            new_height = max(self.minimumHeight(), geo.height() + delta.y())

        self.setGeometry(new_x, new_y, new_width, new_height)

    def _finish_resize(self) -> None:
        if not self._resizing:
            return
        self._resizing = False
        self._resize_edge = None
        self._resize_start_pos = None
        self._resize_start_geometry = None
        self._schedule_geometry_save()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self._get_resize_edge(event.position().toPoint())
            if edge != (0, 0):
                self._begin_resize(edge, event.globalPosition().toPoint())
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resizing and self._resize_edge:
            self._apply_resize_delta(event.globalPosition().toPoint())
            event.accept()
            return

        edge = self._get_resize_edge(event.position().toPoint())
        self._update_cursor_for_edge(edge)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._resizing:
            self._finish_resize()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def _schedule_geometry_save(self):
        if not self._initial_show_complete:
            return
        if self._geometry_save_timer is None:
            self._geometry_save_timer = QTimer(self)
            self._geometry_save_timer.setSingleShot(True)
            self._geometry_save_timer.timeout.connect(self._save_geometry)

        self._geometry_save_timer.stop()
        self._geometry_save_timer.start(500)

    def _save_geometry(self):
        if self.isMaximized() or self.isMinimized():
            return

        if self._compact_mode:
            self._save_compact_geometry()
            return

        geo = self.geometry()
        width = geo.width()
        history_expanded = (
            hasattr(self, "history_sidebar") and self.history_sidebar.is_expanded
        )
        if history_expanded:
            width = max(self.minimumWidth(), width - self._sidebar_width)
        self._collapsed_width = width

        saved_height = (
            self._transcription_tab_height
            if hasattr(self, "tabbed_content")
            and self.tabbed_content.current_index()
            == TabbedContentWidget.TAB_MEETING_MODE
            and getattr(self, "_transcription_tab_height", 0) > 0
            else geo.height()
        )

        try:
            settings_manager.save_setting(
                SettingsKey.WINDOW_GEOMETRY,
                {
                    'x': geo.x(),
                    'y': geo.y(),
                    'width': width,
                    'height': saved_height,
                    'format': self._geometry_format,
                    'history_expanded': history_expanded,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to save window geometry: {e}")

    def _restore_window_geometry(self):
        try:
            geo = settings_manager.get(SettingsKey.WINDOW_GEOMETRY)
            if isinstance(geo, dict) and {'x', 'y', 'width', 'height'}.issubset(geo.keys()):
                from PyQt6.QtWidgets import QApplication
                from PyQt6.QtCore import QRect

                screen = QApplication.primaryScreen()
                if screen:
                    screen_geo = screen.availableGeometry()
                    saved_rect = QRect(geo['x'], geo['y'], geo['width'], geo['height'])
                    if screen_geo.intersects(saved_rect):
                        raw_width = geo['width']
                        migrated_expanded_width = False
                        legacy_expanded_width = (
                            config.MAIN_WINDOW_DEFAULT_WIDTH
                            + config.MAIN_WINDOW_HISTORY_SIDEBAR_WIDTH
                            - config.MAIN_WINDOW_HISTORY_EDGE_TAB_WIDTH
                        )
                        if (
                            geo.get('format') != self._geometry_format
                            and raw_width >= legacy_expanded_width
                        ):
                            raw_width -= config.MAIN_WINDOW_HISTORY_SIDEBAR_WIDTH
                            migrated_expanded_width = True

                        width = max(self.minimumWidth(), min(raw_width, self.maximumWidth()))
                        max_height = screen_geo.height()

                        # The transcript starts collapsed, so geometry saved while it
                        # was expanded must not leave its now-hidden body as blank
                        # vertical space. Apply this independently of window width
                        # (users can resize the main workspace horizontally).
                        transcript_collapsed = (
                            hasattr(self, "transcription_tabs")
                            and all(
                                tab.is_transcription_collapsed()
                                for tab in self.transcription_tabs
                            )
                        )
                        if transcript_collapsed:
                            max_height = min(
                                max_height,
                                config.MAIN_WINDOW_COLLAPSED_RESTORE_MAX_HEIGHT,
                            )

                        # Normalize narrow and legacy sidebar-width restores.
                        if width <= config.MAIN_WINDOW_DEFAULT_WIDTH or migrated_expanded_width:
                            width = config.MAIN_WINDOW_DEFAULT_WIDTH
                        height = max(self.minimumHeight(), min(geo['height'], max_height))
                        self._collapsed_width = width
                        self._transcription_tab_height = height
                        restore_width = width
                        if (
                            hasattr(self, "history_sidebar")
                            and self.history_sidebar.is_expanded
                        ):
                            restore_width = min(
                                self.maximumWidth(),
                                width + self._sidebar_width,
                            )
                        clamped = self._clamp_geometry(
                            geo["x"], geo["y"], restore_width, height
                        )
                        self.setGeometry(clamped)
                        logger.info(f"Restored window geometry: {geo}")
                        return

            logger.debug("No valid saved geometry, using default")
            self._center_on_screen()
        except Exception as e:
            logger.warning(f"Failed to restore window geometry: {e}")
            self._center_on_screen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Update transcription tab height on manual resize when not in meeting mode
        if (
            not self._compact_mode
            and hasattr(self, "tabbed_content")
            and self.tabbed_content.current_index()
            != TabbedContentWidget.TAB_MEETING_MODE
            and not (
                hasattr(self, "_resize_animation")
                and self._resize_animation.state()
                == QPropertyAnimation.State.Running
            )
        ):
            self._transcription_tab_height = event.size().height()

        # A narrower window rewraps the Meeting Mode text, changing how much
        # height that page needs. Height-only changes cannot alter the wrap.
        if event.oldSize().width() != event.size().width():
            self._schedule_meeting_mode_height_sync()
        if not self._resizing:
            self._schedule_geometry_save()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._schedule_geometry_save()

    def showEvent(self, event):
        super().showEvent(event)

        # Skip geometry restoration on initial show (already handled in __init__)
        # This prevents interference with Qt's initial layout calculation
        if not self._initial_show_complete:
            self._initial_show_complete = True
            self._schedule_meeting_mode_height_sync()
            self._schedule_meeting_mode_intro()
            return

        if not self.isMaximized():
            if self._compact_mode:
                self._restore_compact_geometry()
            else:
                self._restore_window_geometry()

        self._schedule_meeting_mode_height_sync()

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.MouseMove and not self._resizing:
            if hasattr(event, 'globalPosition'):
                global_pos = event.globalPosition().toPoint()
                local_pos = self.mapFromGlobal(global_pos)

                if self.rect().contains(local_pos):
                    edge = self._get_resize_edge(local_pos)
                    self._update_cursor_for_edge(edge)

        return super().eventFilter(obj, event)
