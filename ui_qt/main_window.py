"""
PyQt6 main window.
Main application window with recording controls and transcription display.
"""
import logging
import sys
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QTextEdit, QFrame, QPushButton
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QEvent, QPropertyAnimation, QRect
from PyQt6.QtGui import QFont, QIcon, QKeySequence, QPixmap

from config import config
from services.hotkey_manager import format_hotkey_display
from services.settings import SettingsKey, settings_manager
from ui_qt.utils.collapse_animation import (
    SECTION_COLLAPSE_DURATION_MS,
    SECTION_COLLAPSE_EASING,
    UNLIMITED_HEIGHT,
)

logger = logging.getLogger(__name__)


class CustomTitleBar(QFrame):
    """Custom title bar for frameless window with integrated menu."""

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
        self._normal_geometry = None  # Store geometry before maximizing
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
        """Create the integrated menu bar widget on the title bar."""
        from PyQt6.QtWidgets import QMenuBar
        self.menu_bar = QMenuBar()
        self.menu_bar.setStyleSheet(self._MENU_BAR_STYLE)
        layout.addWidget(self.menu_bar)

    def _build_title_label(self, layout: QHBoxLayout) -> None:
        """Create the centered application title label."""
        self.title_label = QLabel("OpenWhisper")
        self.title_label.setStyleSheet(self._TITLE_LABEL_STYLE)
        layout.addWidget(self.title_label)

    def _build_window_buttons(self, layout: QHBoxLayout) -> None:
        """Create the minimize/maximize/close window-control buttons."""
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
                # Restore to saved geometry
                if self._normal_geometry:
                    self.parent_window.setGeometry(self._normal_geometry)
                self.maximize_btn.setText("□")
                self.maximize_btn.setToolTip("Maximize")
            else:
                # Save current geometry before maximizing
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
    HeaderCard, Card, PrimaryButton, DangerButton,
    SuccessButton, WarningButton, ControlPanel, Button,
    HistorySidebar, HistoryEdgeTab, HotkeyHintFilter,
    TranscriptionStatsWidget,
    TabbedContentWidget, QuickRecordTab, UploadFileTab,
    CompactRecordController,
)
from services.history_manager import history_manager


class MainWindow(QMainWindow):
    """PyQt6 main window with clean, professional design."""

    # Window-local keyboard shortcuts. Distinct from the global hotkeys in
    # config.DEFAULT_HOTKEYS, which work even when the app is unfocused.
    UPLOAD_SHORTCUT = "Ctrl+O"
    HISTORY_SHORTCUT = "Ctrl+H"
    COMPACT_SHORTCUT = "Ctrl+Shift+C"
    QUIT_SHORTCUT = "Ctrl+Q"

    # Signals for application events
    record_toggled = pyqtSignal(bool)
    record_canceled = pyqtSignal()
    model_changed = pyqtSignal(str)
    whisper_engine_changed = pyqtSignal()  # Local engine (model/device/quant) changed
    transcription_ready = pyqtSignal(str)
    settings_requested = pyqtSignal()
    hotkeys_requested = pyqtSignal()
    about_requested = pyqtSignal()
    history_toggle_requested = pyqtSignal()
    retranscribe_requested = pyqtSignal(str)  # Emits audio file path
    upload_file_requested = pyqtSignal(str)  # audio_path from upload tab Transcribe button
    tab_changed = pyqtSignal(int)  # Emitted when tab selection changes

    def __init__(self):
        """Initialize the main window."""
        super().__init__()
        self.setWindowTitle("OpenWhisper")

        # Frameless window with custom title bar.
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

        # State
        self.is_recording = False
        self.current_model = config.MODEL_CHOICES[0]
        self._force_quit = False  # Flag to bypass minimize to tray on close
        self._initial_show_complete = False  # Track if initial show has completed
        self._compact_mode = False
        self._full_geometry = None

        # Window sizing for sidebar toggle
        self._collapsed_width = config.MAIN_WINDOW_DEFAULT_WIDTH
        self._sidebar_width = config.MAIN_WINDOW_HISTORY_SIDEBAR_WIDTH
        self._geometry_format = "collapsed_content_v1"

        # Height actually reclaimed by the last transcription collapse, so the
        # matching expand restores exactly that much (see _on_transcription_collapsed).
        self._collapse_freed_height = 0

        # Same tracking for the Engine Settings panel (independent of transcription).
        self._engine_collapse_freed_height = 0

        # Edge resize support for frameless window
        self._resize_margin = 8  # Pixels from edge to trigger resize
        self._resizing = False
        self._resize_edge = None  # Tuple of (horizontal, vertical) edge flags
        self._resize_start_pos = None
        self._resize_start_geometry = None

        # Geometry persistence
        self._geometry_save_timer = None
        self._tab_history_refresh_timer = QTimer(self)
        self._tab_history_refresh_timer.setSingleShot(True)
        self._tab_history_refresh_timer.timeout.connect(
            self._refresh_history_sidebar_if_expanded
        )

        # Callbacks (will be set by controller)
        self.on_show_copied_animation: Optional[Callable] = None

        # Setup UI
        self._setup_ui()
        self._setup_menu()
        self._connect_signals()
        self._load_saved_settings()
        self._restore_window_geometry()
        self._restore_compact_mode()

        # Enable mouse tracking for resize cursor updates
        self.setMouseTracking(True)
        # Install event filter on application to catch mouse moves from all widgets
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().installEventFilter(self)

    def _setup_ui(self):
        """Setup the user interface."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Subtle border to indicate resize areas on frameless window
        central_widget.setStyleSheet("""
            QWidget#centralWidget {
                border: 1px solid #3a3a3c;
            }
        """)
        central_widget.setObjectName("centralWidget")
        central_widget.setMouseTracking(True)

        # Outer layout for title bar + content
        outer_layout = QVBoxLayout(central_widget)
        outer_layout.setContentsMargins(1, 1, 1, 1)  # 1px margin for border visibility
        outer_layout.setSpacing(0)

        # Custom title bar
        self.title_bar = CustomTitleBar(self)
        outer_layout.addWidget(self.title_bar)

        # Container for main content + sidebar
        content_wrapper = QWidget()
        root_layout = QHBoxLayout(content_wrapper)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        outer_layout.addWidget(content_wrapper, stretch=1)

        # Main content area (left side)
        main_area = QWidget()
        main_area_layout = QVBoxLayout(main_area)
        main_area_layout.setContentsMargins(0, 0, 0, 0)
        main_area_layout.setSpacing(0)

        # Tabbed Content Widget (Quick Record)
        self.tabbed_content = TabbedContentWidget()
        self.quick_record_tab = QuickRecordTab()

        self.tabbed_content.add_tab(self.quick_record_tab, "Quick Record")

        self.upload_file_tab = UploadFileTab()
        self.tabbed_content.add_tab(self.upload_file_tab, "Upload File")

        # All transcription tabs; used to fan out shared state (model
        # selection, engine settings, collapse mirroring, device info).
        self.transcription_tabs = (self.quick_record_tab, self.upload_file_tab)

        # Sync the stack with the tab bar after all tabs are added
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

        # Connect tab changed signal to update sidebar and emit signal
        self.tabbed_content.tab_changed.connect(self._on_tab_changed)

        # Connect signals shared by all transcription tabs
        for tab in self.transcription_tabs:
            tab.model_changed.connect(self._on_model_changed)
            tab.engine_settings_changed.connect(self._on_engine_settings_changed)
            tab.engine_settings_collapsed.connect(self._on_engine_settings_collapsed)
            tab.transcription_collapsed.connect(self._on_transcription_collapsed)
            tab.stats_widget.visibility_changed.connect(self._on_stats_visibility_changed)

        # Connect tab-specific signals
        self.quick_record_tab.record_toggled.connect(self._on_quick_record_toggled)
        self.quick_record_tab.record_canceled.connect(self._on_quick_record_canceled)
        self.upload_file_tab.upload_requested.connect(self._on_upload_file_transcribe)

        main_area_layout.addWidget(self.tabbed_content)
        main_area_layout.addWidget(self.compact_controller)

        # Add main area to root layout
        root_layout.addWidget(main_area, stretch=1)

        # History edge tab (always visible toggle button)
        self.history_edge_tab = HistoryEdgeTab()
        self.history_edge_tab.set_shortcut_hint(self.HISTORY_SHORTCUT)
        self.history_edge_tab.clicked.connect(self.toggle_history)
        root_layout.addWidget(self.history_edge_tab)

        # History sidebar (right side)
        self.history_sidebar = HistorySidebar()
        self.history_sidebar.entry_selected.connect(self._on_history_entry_selected)
        self.history_sidebar.entry_copied.connect(self._on_history_entry_copied)
        self.history_sidebar.entry_deleted.connect(self._on_history_entry_deleted)
        self.history_sidebar.retranscribe_requested.connect(self._on_retranscribe_requested)
        self.history_sidebar.width_animated.connect(self._on_sidebar_width_animated)
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
        """Create the bottom footer bar containing window actions."""
        self.footer = QWidget()
        self.footer.setObjectName("footerBar")
        self.footer.setFixedHeight(48)
        self.footer.setStyleSheet(self._FOOTER_BAR_STYLE)

        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(16, 7, 16, 7)
        footer_layout.setSpacing(0)
        footer_layout.addStretch()

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
        """Setup the menu bar in the custom title bar."""
        # Hide the QMainWindow's built-in menu bar
        self.menuBar().hide()

        # Use the custom title bar's menu bar
        menubar = self.title_bar.menu_bar

        # File menu
        file_menu = menubar.addMenu("File")
        upload_action = file_menu.addAction("Upload Audio File...", self.upload_audio_file)
        upload_action.setShortcut(QKeySequence(self.UPLOAD_SHORTCUT))
        file_menu.addSeparator()
        file_menu.addAction("Settings", self.open_settings)
        file_menu.addAction("Hotkeys", self.open_hotkey_settings)
        file_menu.addSeparator()
        file_menu.addAction("Minimize to Tray", self.minimize_to_tray)
        quit_action = file_menu.addAction(
            "Quit" if sys.platform == "darwin" else "Exit", self.quit_application
        )
        quit_action.setShortcut(QKeySequence(self.QUIT_SHORTCUT))

        # View menu
        view_menu = menubar.addMenu("View")
        history_action = view_menu.addAction("History", self.toggle_history)
        history_action.setShortcut(QKeySequence(self.HISTORY_SHORTCUT))
        compact_action = view_menu.addAction("Compact Mode", self.toggle_compact_mode)
        compact_action.setShortcut(QKeySequence(self.COMPACT_SHORTCUT))

        # Help menu
        help_menu = menubar.addMenu("Help")
        help_menu.addAction("About", self.show_about)

    def _connect_signals(self):
        """Connect signals to slots."""
        # Note: Button signals are now handled by QuickRecordTab
        # Tab connections are set up in _setup_ui
        pass

    def _load_saved_settings(self):
        """Load saved settings and apply to UI."""
        try:
            saved_model = settings_manager.load_model_selection()
            for tab in self.transcription_tabs:
                tab.set_model_selection(saved_model)
            self.current_model = self.quick_record_tab.current_model
            self._apply_local_engine_visibility(self.current_model)
            logger.info(f"Loaded saved model selection: {saved_model}")
        except Exception as e:
            logger.error(f"Failed to load saved settings: {e}")
            # Use default (already set)

    def _on_tab_changed(self, index: int):
        """Handle tab selection change."""
        logger.debug(f"Tab changed to index {index}")

        if self._compact_mode and index != TabbedContentWidget.TAB_QUICK_RECORD:
            self.set_compact_mode(False)

        self._schedule_history_sidebar_refresh()

        # Emit signal for external listeners
        self.tab_changed.emit(index)

    def _schedule_history_sidebar_refresh(self) -> None:
        """Defer visible sidebar refreshes so tab clicks stay responsive."""
        if not self.history_sidebar.is_expanded:
            return

        self._tab_history_refresh_timer.start(75)

    def _refresh_history_sidebar_if_expanded(self) -> None:
        """Refresh history only when the sidebar is actually visible."""
        if self.history_sidebar.is_expanded:
            self.history_sidebar.refresh()

    def _on_quick_record_toggled(self, is_recording: bool):
        """Handle record toggle from Quick Record tab."""
        self.is_recording = is_recording
        self.compact_controller.set_recording_state(is_recording)
        self.compact_controller.set_status(
            "Recording in progress..." if is_recording else "Ready to record"
        )

        # Lock/unlock tabs during recording
        if is_recording:
            self.tabbed_content.set_recording_state(True, TabbedContentWidget.TAB_QUICK_RECORD)
        else:
            self.tabbed_content.set_recording_state(False, -1)

        self.record_toggled.emit(is_recording)

    def _on_quick_record_canceled(self):
        """Handle cancel from Quick Record tab."""
        self.is_recording = False
        self.compact_controller.set_recording_state(False)
        self.compact_controller.set_status("Ready to record")
        self.tabbed_content.set_recording_state(False, -1)

        self.record_canceled.emit()

    def _on_model_changed(self, model_name: str):
        """Handle model selection change from either tab and keep both in sync."""
        self.current_model = model_name

        # Sync the other tabs' combos without re-emitting the signal
        for tab in self.transcription_tabs:
            combo = tab.model_combo
            if combo.currentText() != model_name:
                combo.blockSignals(True)
                combo.setCurrentText(model_name)
                tab.current_model = model_name
                combo.blockSignals(False)

        self._apply_local_engine_visibility(model_name)

        self.model_changed.emit(model_name)

    def _apply_local_engine_visibility(self, model_name: str):
        """Show the local-engine panel only when Local Whisper is the backend.

        Args:
            model_name: The backend display name (e.g. "Local Whisper").
        """
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

    def _on_upload_file_transcribe(self, audio_path: str):
        """Handle Transcribe click from the Upload File tab."""
        self.upload_file_requested.emit(audio_path)

    def _update_recording_state(self):
        """Update UI states based on recording status."""
        # Delegate to quick record tab
        self.quick_record_tab.is_recording = self.is_recording
        self.quick_record_tab._update_recording_state()
        self.compact_controller.set_recording_state(self.is_recording)
        self.compact_controller.set_status(
            "Recording in progress..." if self.is_recording else "Ready to record"
        )

        # Lock/unlock tabs during recording
        if self.is_recording:
            self.tabbed_content.set_recording_state(True, TabbedContentWidget.TAB_QUICK_RECORD)
        else:
            self.tabbed_content.set_recording_state(False, -1)

    def set_status(self, status_text: str):
        """Update the status label on the active tab."""
        # Update the Quick Record tab status
        self.quick_record_tab.set_status(status_text)
        self.compact_controller.set_status(status_text)

    def set_device_info(self, device_info: str):
        """Set the resolved-engine readout on both tabs' Local engine panels.

        Args:
            device_info: Device information string to display.
        """
        for tab in self.transcription_tabs:
            tab.set_device_info(device_info)

    def set_transcript(self, text: str):
        """Set the transcription text."""
        self.quick_record_tab.set_transcript(text)

    def append_transcription(self, text: str):
        """Append text to the transcription."""
        self.quick_record_tab.append_transcription(text)

    def clear_transcription(self):
        """Clear the transcription text."""
        self.quick_record_tab.clear_transcription()

    def set_partial_transcription(self, text: str, is_final: bool):
        """Display partial transcription with visual indicator.

        Args:
            text: Partial transcription text
            is_final: Whether this chunk is finalized
        """
        self.quick_record_tab.set_partial_transcription(text, is_final)

    def clear_partial_transcription(self):
        """Clear partial transcription buffer."""
        self.quick_record_tab.clear_partial_transcription()

    def set_transcription_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int
    ):
        """Set the transcription statistics display.

        Args:
            transcription_time: Time taken to transcribe in seconds.
            audio_duration: Duration of the audio in seconds.
            file_size: Size of the audio file in bytes.
        """
        self.quick_record_tab.set_transcription_stats(
            transcription_time, audio_duration, file_size
        )

    def clear_transcription_stats(self):
        """Clear and hide the transcription statistics display."""
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
            self._animate_resize(self.width(), new_height)
        else:
            # Give back exactly what the matching collapse reclaimed. If we have
            # no tracked collapse this session (e.g. the app launched already
            # collapsed), grow once toward the default height instead.
            restore = self._collapse_freed_height
            self._collapse_freed_height = 0
            if restore > 0:
                self._animate_resize(self.width(), current_height + restore)
            elif current_height < config.MAIN_WINDOW_TRANSCRIPTION_EXPAND_HEIGHT:
                self._animate_resize(
                    self.width(), config.MAIN_WINDOW_TRANSCRIPTION_EXPAND_HEIGHT
                )

    def _on_engine_settings_collapsed(self, collapsed: bool, delta: int):
        """Reclaim/restore window height when the Engine Settings panel toggles.

        Keeps both tabs in the same collapsed state, then animates the window
        height by the freed (or restored) body height so the change feels smooth.

        Args:
            collapsed: True if the panel was just collapsed, False if expanded.
            delta: The body height that was hidden/shown, in pixels.
        """
        source = self.sender()
        for tab in self.transcription_tabs:
            if tab is not source:
                tab.set_engine_settings_collapsed(collapsed)

        current_height = self.height()
        if collapsed:
            if delta <= 0:
                return
            new_height = max(config.MAIN_WINDOW_MIN_HEIGHT, current_height - delta)
            self._engine_collapse_freed_height = current_height - new_height
            self._animate_resize(self.width(), new_height)
        else:
            restore = self._engine_collapse_freed_height
            self._engine_collapse_freed_height = 0
            if restore > 0:
                self._animate_resize(self.width(), current_height + restore)
            elif delta > 0:
                self._animate_resize(self.width(), current_height + delta)

    def _on_stats_visibility_changed(self, visible: bool):
        """Handle stats widget visibility change and adjust window height.

        Args:
            visible: True if stats are now visible, False if hidden.
        """
        # Get the stats widget height (approximately 60px when visible)
        stats_height = 60 if visible else 0
        current_height = self.height()

        if visible:
            # Expand window to fit stats
            new_height = current_height + stats_height
        else:
            # Shrink window when stats hidden
            new_height = max(
                config.MAIN_WINDOW_MIN_HEIGHT,
                current_height - stats_height,
            )

        # Animate the height change
        self._animate_resize(self.width(), new_height)

    def get_model_value(self) -> str:
        """Get the model value key."""
        return self.quick_record_tab.get_model_value()

    def open_settings(self):
        """Open settings dialog."""
        logger.info("Opening settings dialog")
        self.settings_requested.emit()

    def open_hotkey_settings(self):
        """Open hotkey settings dialog."""
        logger.info("Opening hotkey settings")
        self.hotkeys_requested.emit()

    def upload_audio_file(self):
        """Switch to the Upload File tab and open file browser."""
        logger.info("Upload audio file requested via menu")
        if self._compact_mode:
            self.set_compact_mode(False)
        self.tabbed_content.set_current_index(TabbedContentWidget.TAB_UPLOAD_FILE)
        self.upload_file_tab.open_file_browser()

    def switch_to_quick_record(self):
        """Switch to the Quick Record tab."""
        logger.info("Switching to Quick Record tab")
        self.tabbed_content.set_current_index(TabbedContentWidget.TAB_QUICK_RECORD)

    def show_about(self):
        """Show about dialog."""
        logger.info("Showing about dialog")
        self.about_requested.emit()

    def minimize_to_tray(self):
        """Minimize the window to the system tray."""
        logger.info("Minimizing to tray")
        self.hide()

    def toggle_compact_mode(self) -> None:
        """Toggle between the full workspace and compact recording controller."""
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
            self.compact_button.setText("Compact")

            if self._full_geometry is not None:
                self.setGeometry(self._full_geometry)
            else:
                self._restore_window_geometry()

        if persist:
            try:
                settings_manager.save_setting(SettingsKey.COMPACT_MODE, compact)
            except Exception as e:
                logger.warning(f"Failed to save compact mode: {e}")

    def _restore_compact_mode(self) -> None:
        """Restore the persisted compact/full mode selection."""
        try:
            if settings_manager.get(SettingsKey.COMPACT_MODE, False) is True:
                self.set_compact_mode(True, persist=False)
        except Exception as e:
            logger.warning(f"Failed to restore compact mode: {e}")

    def _save_compact_geometry(self) -> None:
        """Persist the compact controller position separately from full geometry."""
        geo = self.geometry()
        try:
            settings_manager.save_setting(
                SettingsKey.COMPACT_WINDOW_GEOMETRY,
                {"x": geo.x(), "y": geo.y()},
            )
        except Exception as e:
            logger.warning(f"Failed to save compact window geometry: {e}")

    def _restore_compact_geometry(self) -> None:
        """Restore and clamp the compact controller position to the screen."""
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
        """Toggle between hidden-to-tray and visible foreground states."""
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
        """Quit the application completely (bypasses minimize to tray)."""
        logger.info("Quitting application")
        self._save_geometry()
        self._force_quit = True
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().quit()

    def toggle_history(self):
        """Toggle the history sidebar visibility."""
        logger.info("Toggling history sidebar")

        if self._compact_mode:
            self.set_compact_mode(False)
            if self.history_sidebar.is_expanded:
                return

        # Update the edge tab arrow direction immediately for instant visual feedback
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

        self.history_toggle_requested.emit()

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
        self.setGeometry(geo.x(), geo.y(), target_width, geo.height())

    def _animate_resize(self, target_width: int, target_height: int):
        """Animate window resize.

        Args:
            target_width: Target window width.
            target_height: Target window height.
        """
        from PyQt6.QtCore import QRect

        if not hasattr(self, '_resize_animation'):
            self._resize_animation = QPropertyAnimation(self, b"geometry")
            self._resize_animation.setDuration(SECTION_COLLAPSE_DURATION_MS)
            self._resize_animation.setEasingCurve(SECTION_COLLAPSE_EASING)

        current_geo = self.geometry()
        target_geo = QRect(current_geo.x(), current_geo.y(), target_width, target_height)

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
        """Refresh the history sidebar content."""
        self.history_sidebar.refresh()

    def _on_history_entry_selected(self, entry_id: str):
        """Handle history entry selection - show full transcription and copy to clipboard."""
        entry = history_manager.get_entry_by_id(entry_id)
        if entry:
            self.quick_record_tab.set_transcript(entry.text)

            # Copy to clipboard
            try:
                from PyQt6.QtWidgets import QApplication
                clipboard = QApplication.clipboard()
                clipboard.setText(entry.text)
                self.set_status("Copied to clipboard")
                QTimer.singleShot(2000, lambda: self.set_status("Ready to record"))

                # Show the copied animation
                if self.on_show_copied_animation:
                    self.on_show_copied_animation()

                logger.info(f"Loaded and copied history entry: {entry_id[:8]}...")
            except Exception as e:
                logger.error(f"Failed to copy to clipboard: {e}")
                logger.info(f"Loaded history entry: {entry_id[:8]}...")

    def _on_history_entry_copied(self, entry_id: str):
        """Handle history entry copied notification."""
        self.set_status("Copied to clipboard")
        # Auto-clear status after delay
        QTimer.singleShot(2000, lambda: self.set_status("Ready to record"))

    def _on_history_entry_deleted(self, entry_id: str):
        """Handle history entry deleted notification."""
        self.set_status("Entry deleted")
        # Auto-clear status after delay
        QTimer.singleShot(2000, lambda: self.set_status("Ready to record"))

    def _on_retranscribe_requested(self, audio_path: str):
        """Handle re-transcription request."""
        logger.info(f"Re-transcribe requested: {audio_path}")
        self.retranscribe_requested.emit(audio_path)

    def closeEvent(self, event):
        """Handle window close event."""
        logger.info("Main window closing")
        self.tabbed_content.flush_pending_tab_selection()
        # If force quit is set, close immediately
        if self._force_quit:
            logger.info("Force quit - closing application")
            event.accept()
            return

        # Check if minimize to tray is enabled (default: True)
        try:
            settings = settings_manager.load_all_settings()
            minimize_tray = settings.get(SettingsKey.MINIMIZE_TRAY, True)  # Default to True
        except Exception as e:
            logger.error(f"Failed to load settings: {e}")
            minimize_tray = True  # Default to True on error

        if minimize_tray:
            # Hide window instead of closing (X button behavior)
            event.ignore()
            try:
                self.hide()
                logger.info("Window hidden to system tray")
            except Exception as e:
                logger.debug(f"Error hiding window: {e}")
                # If hiding fails, accept the close event
                event.accept()
        else:
            # Close normally
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

    # ==================== Edge Resize Support ====================

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
        """Update cursor shape based on edge.

        Args:
            edge: Tuple of (horizontal, vertical) edge flags.
        """
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
        """Start a resize operation from a given edge and global position."""
        self._resizing = True
        self._resize_edge = edge
        self._resize_start_pos = global_pos
        self._resize_start_geometry = self.geometry()

    def _apply_resize_delta(self, global_pos) -> None:
        """Apply resize based on the stored start geometry and a global cursor position."""
        if not self._resizing or not self._resize_edge or not self._resize_start_geometry:
            return

        delta = global_pos - self._resize_start_pos
        geo = self._resize_start_geometry
        h, v = self._resize_edge

        new_x = geo.x()
        new_y = geo.y()
        new_width = geo.width()
        new_height = geo.height()

        # Handle horizontal resize
        if h == -1:  # Left edge
            new_width = max(self.minimumWidth(), geo.width() - delta.x())
            new_x = geo.x() + geo.width() - new_width
        elif h == 1:  # Right edge
            new_width = min(self.maximumWidth(), max(self.minimumWidth(), geo.width() + delta.x()))

        # Handle vertical resize
        if v == -1:  # Top edge
            new_height = max(self.minimumHeight(), geo.height() - delta.y())
            new_y = geo.y() + geo.height() - new_height
        elif v == 1:  # Bottom edge
            new_height = max(self.minimumHeight(), geo.height() + delta.y())

        self.setGeometry(new_x, new_y, new_width, new_height)

    def _finish_resize(self) -> None:
        """Finish a resize operation and persist geometry."""
        if not self._resizing:
            return
        self._resizing = False
        self._resize_edge = None
        self._resize_start_pos = None
        self._resize_start_geometry = None
        self._schedule_geometry_save()

    def mousePressEvent(self, event):
        """Handle mouse press for edge resize."""
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self._get_resize_edge(event.position().toPoint())
            if edge != (0, 0):
                self._begin_resize(edge, event.globalPosition().toPoint())
                event.accept()
                return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        """Handle mouse move for resize cursor and resizing."""
        if self._resizing and self._resize_edge:
            self._apply_resize_delta(event.globalPosition().toPoint())
            event.accept()
            return

        # Update cursor based on edge proximity
        edge = self._get_resize_edge(event.position().toPoint())
        self._update_cursor_for_edge(edge)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        """Handle mouse release to end resize."""
        if event.button() == Qt.MouseButton.LeftButton and self._resizing:
            self._finish_resize()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    # ==================== Geometry Persistence ====================

    def _schedule_geometry_save(self):
        """Schedule geometry save with debounce to avoid excessive writes."""
        if self._geometry_save_timer is None:
            self._geometry_save_timer = QTimer(self)
            self._geometry_save_timer.setSingleShot(True)
            self._geometry_save_timer.timeout.connect(self._save_geometry)

        # Reset timer on each call (debounce)
        self._geometry_save_timer.stop()
        self._geometry_save_timer.start(500)  # Save 500ms after last change

    def _save_geometry(self):
        """Save current window geometry to settings."""
        if self.isMaximized() or self.isMinimized():
            return  # Don't save maximized/minimized state

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

        try:
            settings_manager.save_setting(
                SettingsKey.WINDOW_GEOMETRY,
                {
                    'x': geo.x(),
                    'y': geo.y(),
                    'width': width,
                    'height': geo.height(),
                    'format': self._geometry_format,
                    'history_expanded': history_expanded,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to save window geometry: {e}")

    def _restore_window_geometry(self):
        """Restore window geometry from settings."""
        try:
            geo = settings_manager.get(SettingsKey.WINDOW_GEOMETRY)
            if isinstance(geo, dict) and {'x', 'y', 'width', 'height'}.issubset(geo.keys()):
                # Validate geometry is within screen bounds
                from PyQt6.QtWidgets import QApplication
                from PyQt6.QtCore import QRect

                screen = QApplication.primaryScreen()
                if screen:
                    screen_geo = screen.availableGeometry()
                    # Check if saved position is at least partially on screen
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

                        # Ensure size constraints - both min and max for width and height
                        width = max(self.minimumWidth(), min(raw_width, self.maximumWidth()))
                        # Cap narrow/collapsed restores to the default height so stale
                        # geometry cannot make the app reopen as an overly tall strip.
                        max_height = screen_geo.height()
                        if width <= config.MAIN_WINDOW_DEFAULT_WIDTH or migrated_expanded_width:
                            width = config.MAIN_WINDOW_DEFAULT_WIDTH
                            max_height = min(
                                max_height,
                                config.MAIN_WINDOW_COLLAPSED_RESTORE_MAX_HEIGHT,
                            )
                        height = max(self.minimumHeight(), min(geo['height'], max_height))
                        self._collapsed_width = width
                        restore_width = width
                        if (
                            hasattr(self, "history_sidebar")
                            and self.history_sidebar.is_expanded
                        ):
                            restore_width = min(
                                self.maximumWidth(),
                                width + self._sidebar_width,
                            )
                        self.setGeometry(geo['x'], geo['y'], restore_width, height)
                        logger.info(f"Restored window geometry: {geo}")
                        return

            logger.debug("No valid saved geometry, using default")
        except Exception as e:
            logger.warning(f"Failed to restore window geometry: {e}")

    def resizeEvent(self, event):
        """Handle resize event to save geometry."""
        super().resizeEvent(event)
        if not self._resizing:  # Don't save during active drag resize (already handled)
            self._schedule_geometry_save()

    def moveEvent(self, event):
        """Handle move event to save geometry."""
        super().moveEvent(event)
        self._schedule_geometry_save()

    def showEvent(self, event):
        """Handle show event - restore geometry when showing from tray."""
        super().showEvent(event)

        # Skip geometry restoration on initial show (already handled in __init__)
        # This prevents interference with Qt's initial layout calculation
        if not self._initial_show_complete:
            self._initial_show_complete = True
            return

        # Re-apply saved geometry when restoring from tray (subsequent shows)
        if not self.isMaximized():
            if self._compact_mode:
                self._restore_compact_geometry()
            else:
                self._restore_window_geometry()

    def eventFilter(self, obj, event):
        """Filter events to update resize cursor when hovering near edges."""
        if event.type() == QEvent.Type.MouseMove and not self._resizing:
            # Check if event has position info and is within our window
            if hasattr(event, 'globalPosition'):
                global_pos = event.globalPosition().toPoint()
                local_pos = self.mapFromGlobal(global_pos)

                # Only update cursor if mouse is within window bounds
                if self.rect().contains(local_pos):
                    edge = self._get_resize_edge(local_pos)
                    self._update_cursor_for_edge(edge)

        return super().eventFilter(obj, event)
