"""
Modern PyQt6 Main Window.
Main application window with recording controls and transcription display.
"""
import logging
from typing import Optional, Callable
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QComboBox, QTextEdit, QFrame, QPushButton
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QIcon, QPixmap

from config import config
from settings import settings_manager
from ui_qt.loading_screen_qt import ModernLoadingScreen


class CustomTitleBar(QFrame):
    """Custom title bar for frameless window with integrated menu."""

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

        # Menu bar (File, View, Help) - LEFT
        from PyQt6.QtWidgets import QMenuBar, QMenu
        self.menu_bar = QMenuBar()
        self.menu_bar.setStyleSheet("""
            QMenuBar {
                background-color: transparent;
                color: #c0c0d0;
                font-size: 12px;
                border: none;
                spacing: 0px;
            }
            QMenuBar::item {
                background-color: transparent;
                padding: 8px 10px 4px 10px;
            }
            QMenuBar::item:selected {
                background-color: #5a5a5c;
                color: #ffffff;
            }
            QMenuBar::item:pressed {
                background-color: #6a6a6c;
            }
            QMenu {
                background-color: #3a3a3c;
                color: #e0e0ff;
                border: 1px solid #5a5a5c;
            }
            QMenu::item {
                padding: 6px 24px;
            }
            QMenu::item:selected {
                background-color: #6366f1;
            }
            QMenu::separator {
                height: 1px;
                background-color: #5a5a5c;
                margin: 4px 8px;
            }
        """)
        layout.addWidget(self.menu_bar)

        layout.addStretch()

        # App title - CENTER
        self.title_label = QLabel("OpenWhisper")
        self.title_label.setStyleSheet("""
            QLabel {
                background-color: transparent;
                color: #e0e0ff;
                font-size: 13px;
                font-weight: 600;
                font-family: 'Segoe UI', sans-serif;
            }
        """)
        layout.addWidget(self.title_label)

        layout.addStretch()

        # Window buttons
        button_style = """
            QPushButton {
                background-color: transparent;
                border: none;
                color: #808098;
                font-size: 14px;
                font-family: 'Segoe UI', sans-serif;
            }
            QPushButton:hover {
                background-color: #3d3d5c;
                color: #ffffff;
            }
        """
        close_button_style = """
            QPushButton {
                background-color: transparent;
                border: none;
                color: #808098;
                font-size: 14px;
                font-family: 'Segoe UI', sans-serif;
            }
            QPushButton:hover {
                background-color: #e81123;
                color: #ffffff;
            }
        """

        self.minimize_btn = QPushButton("─")
        self.minimize_btn.setFixedSize(46, 32)
        self.minimize_btn.setStyleSheet(button_style)
        self.minimize_btn.clicked.connect(self._minimize)

        self.maximize_btn = QPushButton("□")
        self.maximize_btn.setFixedSize(46, 32)
        self.maximize_btn.setStyleSheet(button_style)
        self.maximize_btn.clicked.connect(self._toggle_maximize)

        self.close_btn = QPushButton("✕")
        self.close_btn.setFixedSize(46, 32)
        self.close_btn.setStyleSheet(close_button_style)
        self.close_btn.clicked.connect(self._close)

        layout.addWidget(self.minimize_btn)
        layout.addWidget(self.maximize_btn)
        layout.addWidget(self.close_btn)

        # Match transcription box and dropdown background
        self.setStyleSheet("""
            #customTitleBar {
                background-color: #2c2c2e;
                border-bottom: 1px solid #3a3a3c;
            }
        """)

    def _minimize(self):
        if self.parent_window:
            self.parent_window.showMinimized()

    def _toggle_maximize(self):
        if self.parent_window:
            if self._is_maximized:
                # Restore to saved geometry
                if self._normal_geometry:
                    self.parent_window.setGeometry(self._normal_geometry)
                self.maximize_btn.setText("□")
            else:
                # Save current geometry before maximizing
                self._normal_geometry = self.parent_window.geometry()
                self.parent_window.showMaximized()
                self.maximize_btn.setText("❐")
            self._is_maximized = not self._is_maximized

    def _close(self):
        if self.parent_window:
            self.parent_window.close()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_position = event.globalPosition().toPoint() - self.parent_window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_position:
            self.parent_window.move(event.globalPosition().toPoint() - self._drag_position)
            event.accept()

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._toggle_maximize()


from ui_qt.widgets import (
    HeaderCard, Card, PrimaryButton, DangerButton,
    SuccessButton, WarningButton, ControlPanel, ModernButton,
    HistorySidebar, HistoryEdgeTab, TranscriptionStatsWidget
)
from history_manager import history_manager


class ModernMainWindow(QMainWindow):
    """Modern PyQt6 main window with clean, professional design."""

    # Signals for application events
    record_toggled = pyqtSignal(bool)
    model_changed = pyqtSignal(str)
    transcription_ready = pyqtSignal(str)
    settings_requested = pyqtSignal()
    hotkeys_requested = pyqtSignal()
    overlay_toggle_requested = pyqtSignal()
    about_requested = pyqtSignal()
    history_toggle_requested = pyqtSignal()
    retranscribe_requested = pyqtSignal(str)  # Emits audio file path
    upload_audio_requested = pyqtSignal()  # Request to upload audio file
    test_overlay_requested = pyqtSignal(str)  # Emits overlay state to test

    def __init__(self):
        """Initialize the main window."""
        super().__init__()
        self.logger = logging.getLogger(__name__)
        self.setWindowTitle("OpenWhisper")

        # Frameless window with custom title bar
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        self.setMinimumSize(500, 600)
        self.setMaximumWidth(1280)  # Increased to accommodate sidebar
        self.resize(604, 700)  # Initial size: base_width + edge_tab_width (580 + 24)

        # State
        self.is_recording = False
        self.current_model = config.MODEL_CHOICES[0]
        self.test_loading_screen_instance = None  # Keep reference to prevent GC
        self._force_quit = False  # Flag to bypass minimize to tray on close
        
        # Window sizing for sidebar toggle
        self._base_width = 580  # Optimal width without sidebar
        self._sidebar_width = 380  # HistorySidebar.EXPANDED_WIDTH
        self._edge_tab_width = 24  # HistoryEdgeTab width

        # Callbacks (will be set by controller)
        self.on_record_start: Optional[Callable] = None
        self.on_record_stop: Optional[Callable] = None
        self.on_record_cancel: Optional[Callable] = None
        self.on_model_changed: Optional[Callable] = None
        self.on_retranscribe: Optional[Callable] = None
        self.on_show_copied_animation: Optional[Callable] = None

        # Setup UI
        self._setup_ui()
        self._setup_menu()
        self._connect_signals()
        self._load_saved_settings()

    def _setup_ui(self):
        """Setup the user interface."""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        # Outer layout for title bar + content
        outer_layout = QVBoxLayout(central_widget)
        outer_layout.setContentsMargins(0, 0, 0, 0)
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

        # Content Container (Centered)
        content_container = QWidget()
        content_container.setObjectName("contentContainer")
        content_layout = QVBoxLayout(content_container)
        content_layout.setContentsMargins(24, 24, 24, 24) # Reduced margins
        content_layout.setSpacing(16) # Reduced spacing for compactness
        content_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter)

        # Wrapper to center the content container horizontally
        center_wrapper = QHBoxLayout()
        center_wrapper.addStretch()
        center_wrapper.addWidget(content_container, stretch=1)
        center_wrapper.addStretch()
        
        # Limit max width of content
        content_container.setMaximumWidth(700) # Slightly narrower for cleaner look
        content_container.setMinimumWidth(500)

        main_area_layout.addLayout(center_wrapper)

        # Model selection card
        model_card = Card()
        # Layout margins handled by Card class

        model_label = QLabel("Transcription Model")
        model_label.setObjectName("headerLabel")
        model_label.setFont(QFont("Segoe UI", 13)) # Adjusted font size
        model_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.model_combo = QComboBox()
        self.model_combo.addItems(config.MODEL_CHOICES)
        self.model_combo.setMinimumHeight(40) # Slightly reduced height
        self.model_combo.setFont(QFont("Segoe UI", 12))

        # Device info label (shows CUDA/CPU status)
        self.device_info_label = QLabel("")
        self.device_info_label.setObjectName("deviceInfoLabel")
        self.device_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.device_info_label.setFont(QFont("Segoe UI", 10))
        self.device_info_label.setStyleSheet("color: #8888aa; margin-top: 4px;")
        self.device_info_label.hide()  # Hidden until device info is set

        model_card.layout.addWidget(model_label)
        model_card.layout.addWidget(self.model_combo)
        model_card.layout.addWidget(self.device_info_label)

        content_layout.addWidget(model_card)

        # Status label
        self.status_label = QLabel("Ready to record")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFont(QFont("Segoe UI", 13))
        content_layout.addWidget(self.status_label)

        # Control buttons
        control_panel = ControlPanel()
        control_panel.layout.setSpacing(12) # Reduced spacing

        self.record_button = SuccessButton("Start Recording")
        self.cancel_button = WarningButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.stop_button = DangerButton("Stop")
        self.stop_button.setEnabled(False)

        control_panel.layout.addStretch()
        control_panel.layout.addWidget(self.record_button)
        control_panel.layout.addWidget(self.stop_button)
        control_panel.layout.addWidget(self.cancel_button)
        control_panel.layout.addStretch()

        content_layout.addWidget(control_panel)

        # Transcription display card
        transcription_card = HeaderCard("Transcription")

        self.transcription_text = QTextEdit()
        self.transcription_text.setReadOnly(True)
        self.transcription_text.setMinimumHeight(250) # Adjusted height
        self.transcription_text.setFont(QFont("Segoe UI", 13))
        self.transcription_text.setPlaceholderText(
            "Transcription will appear here...\n"
            "Start recording to begin."
        )

        transcription_card.layout.addWidget(self.transcription_text)

        content_layout.addWidget(transcription_card)

        # Transcription statistics display (hidden by default)
        self.stats_widget = TranscriptionStatsWidget()
        self.stats_widget.visibility_changed.connect(self._on_stats_visibility_changed)
        content_layout.addWidget(self.stats_widget)

        content_layout.addStretch() # Push everything up

        # Add main area to root layout
        root_layout.addWidget(main_area, stretch=1)

        # History edge tab (always visible toggle button)
        self.history_edge_tab = HistoryEdgeTab()
        self.history_edge_tab.clicked.connect(self.toggle_history)
        root_layout.addWidget(self.history_edge_tab)

        # History sidebar (right side)
        self.history_sidebar = HistorySidebar()
        self.history_sidebar.entry_selected.connect(self._on_history_entry_selected)
        self.history_sidebar.entry_copied.connect(self._on_history_entry_copied)
        self.history_sidebar.entry_deleted.connect(self._on_history_entry_deleted)
        self.history_sidebar.retranscribe_requested.connect(self._on_retranscribe_requested)
        root_layout.addWidget(self.history_sidebar)

    def _setup_menu(self):
        """Setup the menu bar in the custom title bar."""
        # Hide the QMainWindow's built-in menu bar
        self.menuBar().hide()

        # Use the custom title bar's menu bar
        menubar = self.title_bar.menu_bar

        # File menu
        file_menu = menubar.addMenu("File")
        file_menu.addAction("Upload Audio File...", self.upload_audio_file)
        file_menu.addSeparator()
        file_menu.addAction("Settings", self.open_settings)
        file_menu.addAction("Hotkeys", self.open_hotkey_settings)
        file_menu.addSeparator()
        file_menu.addAction("Minimize to Tray", self.minimize_to_tray)
        file_menu.addAction("Exit", self.quit_application)

        # View menu
        view_menu = menubar.addMenu("View")
        view_menu.addAction("History", self.toggle_history)
        view_menu.addSeparator()
        view_menu.addAction("Show Overlay", self.toggle_overlay)
        view_menu.addAction("Show Loading Screen", self.test_loading_screen)
        view_menu.addSeparator()

        # Test Overlays submenu
        test_overlays_menu = view_menu.addMenu("Test Overlays")
        test_overlays_menu.addAction("Recording", lambda: self.test_overlay("recording"))
        test_overlays_menu.addAction("Processing", lambda: self.test_overlay("processing"))
        test_overlays_menu.addAction("Transcribing", lambda: self.test_overlay("transcribing"))
        test_overlays_menu.addAction("Canceling", lambda: self.test_overlay("canceling"))
        test_overlays_menu.addSeparator()
        test_overlays_menu.addAction("STT Enable", lambda: self.test_overlay("stt_enable"))
        test_overlays_menu.addAction("STT Disable", lambda: self.test_overlay("stt_disable"))
        test_overlays_menu.addAction("Copied", lambda: self.test_overlay("copied"))
        test_overlays_menu.addSeparator()
        test_overlays_menu.addAction("Large File Splitting (Amber)", lambda: self.test_overlay("large_file_splitting"))
        test_overlays_menu.addAction("Large File Processing (Cyan)", lambda: self.test_overlay("large_file_processing"))

        # Help menu
        help_menu = menubar.addMenu("Help")
        help_menu.addAction("About", self.show_about)

    def _connect_signals(self):
        """Connect button signals to slots."""
        self.record_button.clicked.connect(self._on_record_clicked)
        self.stop_button.clicked.connect(self._on_stop_clicked)
        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)

    def _load_saved_settings(self):
        """Load saved settings and apply to UI."""
        try:
            # Load the saved model selection
            saved_model = settings_manager.load_model_selection()
            
            # Find the display name for the saved model
            for display_name, internal_value in config.MODEL_VALUE_MAP.items():
                if internal_value == saved_model:
                    index = self.model_combo.findText(display_name)
                    if index >= 0:
                        # Block signals temporarily to avoid triggering on_model_changed
                        self.model_combo.blockSignals(True)
                        self.model_combo.setCurrentIndex(index)
                        self.current_model = display_name
                        self.model_combo.blockSignals(False)
                        self.logger.info(f"Loaded saved model selection: {display_name}")
                    break
        except Exception as e:
            self.logger.error(f"Failed to load saved settings: {e}")
            # Use default (already set)

    def _on_record_clicked(self):
        """Handle record button click."""
        self.is_recording = True
        self._update_recording_state()

        if self.on_record_start:
            self.on_record_start()

        self.record_toggled.emit(True)

    def _on_stop_clicked(self):
        """Handle stop button click."""
        self.is_recording = False
        self._update_recording_state()

        if self.on_record_stop:
            self.on_record_stop()

        self.record_toggled.emit(False)

    def _on_cancel_clicked(self):
        """Handle cancel button click."""
        self.is_recording = False
        self._update_recording_state()

        if self.on_record_cancel:
            self.on_record_cancel()

    def _on_model_changed(self, model_name: str):
        """Handle model selection change."""
        self.current_model = model_name
        if self.on_model_changed:
            self.on_model_changed(model_name)
        self.model_changed.emit(model_name)

    def _update_recording_state(self):
        """Update button states based on recording status."""
        if self.is_recording:
            self.record_button.setEnabled(False)
            self.record_button.setText("Recording...")
            self.stop_button.setEnabled(True)
            self.cancel_button.setEnabled(True)
            self.model_combo.setEnabled(False)
            self.status_label.setText("Recording in progress...")
        else:
            self.record_button.setEnabled(True)
            self.record_button.setText("Start Recording")
            self.stop_button.setEnabled(False)
            self.cancel_button.setEnabled(False)
            self.model_combo.setEnabled(True)
            self.status_label.setText("Ready to record")

    def set_status(self, status_text: str):
        """Update the status label."""
        self.status_label.setText(status_text)

    def set_device_info(self, device_info: str):
        """Set the device info label (e.g., 'cuda (float16)').

        Args:
            device_info: Device information string to display.
        """
        if device_info:
            self.device_info_label.setText(device_info)
            self.device_info_label.show()
        else:
            self.device_info_label.hide()

    def set_transcription(self, text: str):
        """Set the transcription text."""
        self.transcription_text.setText(text)

    def append_transcription(self, text: str):
        """Append text to the transcription."""
        cursor = self.transcription_text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcription_text.setTextCursor(cursor)
        self.transcription_text.insertPlainText(text)

    def clear_transcription(self):
        """Clear the transcription text."""
        self.transcription_text.clear()

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
        self.stats_widget.set_stats(transcription_time, audio_duration, file_size)

    def clear_transcription_stats(self):
        """Clear and hide the transcription statistics display."""
        self.stats_widget.clear()

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
            new_height = max(600, current_height - stats_height)  # Don't go below min height

        # Animate the height change
        self._animate_resize(self.width(), new_height)

    def get_model_value(self) -> str:
        """Get the model value key."""
        return config.MODEL_VALUE_MAP.get(self.current_model, "local_whisper")

    def open_settings(self):
        """Open settings dialog."""
        self.logger.info("Opening settings dialog")
        self.settings_requested.emit()

    def open_hotkey_settings(self):
        """Open hotkey settings dialog."""
        self.logger.info("Opening hotkey settings")
        self.hotkeys_requested.emit()

    def upload_audio_file(self):
        """Request to upload an audio file for transcription."""
        self.logger.info("Upload audio file requested")
        self.upload_audio_requested.emit()

    def toggle_overlay(self):
        """Toggle the overlay visibility."""
        self.logger.info("Toggling overlay")
        self.overlay_toggle_requested.emit()

    def test_overlay(self, state: str):
        """Test a specific overlay state."""
        self.logger.info(f"Testing overlay state: {state}")
        self.test_overlay_requested.emit(state)

    def test_loading_screen(self):
        """Show the loading screen for testing purposes."""
        self.logger.info("Testing loading screen")
        
        if self.test_loading_screen_instance:
            self.test_loading_screen_instance.destroy()
            self.test_loading_screen_instance = None
            
        self.test_loading_screen_instance = ModernLoadingScreen()
        self.test_loading_screen_instance.show()
        
        # Simulate some activity
        QTimer.singleShot(1000, lambda: self.test_loading_screen_instance.update_status("Loading resources..."))
        QTimer.singleShot(2000, lambda: self.test_loading_screen_instance.update_progress("Connecting to services..."))
        QTimer.singleShot(3000, lambda: self.test_loading_screen_instance.update_status("Almost ready..."))
        
        # Auto close after 5 seconds
        QTimer.singleShot(5000, lambda: self.test_loading_screen_instance.destroy())
        
        # Allow click to close
        original_mouse_press = self.test_loading_screen_instance.mousePressEvent
        
        def close_on_click(event):
            self.test_loading_screen_instance.destroy()
            self.test_loading_screen_instance = None
            
        self.test_loading_screen_instance.mousePressEvent = close_on_click

    def show_about(self):
        """Show about dialog."""
        self.logger.info("Showing about dialog")
        self.about_requested.emit()

    def minimize_to_tray(self):
        """Minimize the window to the system tray."""
        self.logger.info("Minimizing to tray")
        self.hide()

    def quit_application(self):
        """Quit the application completely (bypasses minimize to tray)."""
        self.logger.info("Quitting application")
        self._force_quit = True
        from PyQt6.QtWidgets import QApplication
        QApplication.instance().quit()

    def toggle_history(self):
        """Toggle the history sidebar visibility."""
        self.logger.info("Toggling history sidebar")
        
        # Update the edge tab arrow direction immediately for instant visual feedback
        will_be_expanded = not self.history_sidebar.is_expanded
        self.history_edge_tab.set_expanded(will_be_expanded)
        
        # Start sidebar animation immediately
        self.history_sidebar.toggle()
        
        # Resize window immediately to match sidebar animation
        self._resize_for_sidebar(will_be_expanded)
        
        self.history_toggle_requested.emit()
    
    def _resize_for_sidebar(self, expanded: bool):
        """Resize window when sidebar is toggled.
        
        Args:
            expanded: True if sidebar is being expanded, False if collapsed.
        """
        current_height = self.height()
        
        if expanded:
            # Expand window to fit sidebar
            new_width = self._base_width + self._sidebar_width + self._edge_tab_width
        else:
            # Collapse window back to base size
            new_width = self._base_width + self._edge_tab_width
        
        # Animate the resize for smooth transition
        self._animate_resize(new_width, current_height)
    
    def _animate_resize(self, target_width: int, target_height: int):
        """Animate window resize.
        
        Args:
            target_width: Target window width.
            target_height: Target window height.
        """
        from PyQt6.QtCore import QPropertyAnimation, QEasingCurve, QRect
        
        # Create animation for geometry
        if not hasattr(self, '_resize_animation'):
            self._resize_animation = QPropertyAnimation(self, b"geometry")
            self._resize_animation.setDuration(250)
            self._resize_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        
        current_geo = self.geometry()
        target_geo = QRect(current_geo.x(), current_geo.y(), target_width, target_height)
        
        self._resize_animation.stop()
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
            self.transcription_text.setText(entry.text)
            
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
                
                self.logger.info(f"Loaded and copied history entry: {entry_id[:8]}...")
            except Exception as e:
                self.logger.error(f"Failed to copy to clipboard: {e}")
                self.logger.info(f"Loaded history entry: {entry_id[:8]}...")

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

    def _on_retranscribe_requested(self, audio_file_path: str):
        """Handle re-transcription request."""
        self.logger.info(f"Re-transcribe requested: {audio_file_path}")
        self.retranscribe_requested.emit(audio_file_path)
        if self.on_retranscribe:
            self.on_retranscribe(audio_file_path)

    def closeEvent(self, event):
        """Handle window close event."""
        self.logger.info("Main window closing")
        try:
            if self.test_loading_screen_instance:
                self.test_loading_screen_instance.destroy()
        except Exception as e:
            self.logger.debug(f"Error destroying loading screen: {e}")
        
        # If force quit is set, close immediately
        if self._force_quit:
            self.logger.info("Force quit - closing application")
            event.accept()
            return
        
        # Check if minimize to tray is enabled (default: True)
        try:
            settings = settings_manager.load_all_settings()
            minimize_tray = settings.get('minimize_tray', True)  # Default to True
        except Exception as e:
            self.logger.error(f"Failed to load settings: {e}")
            minimize_tray = True  # Default to True on error
        
        if minimize_tray:
            # Hide window instead of closing (X button behavior)
            event.ignore()
            try:
                self.hide()
                self.logger.info("Window hidden to system tray")
            except Exception as e:
                self.logger.debug(f"Error hiding window: {e}")
                # If hiding fails, accept the close event
                event.accept()
        else:
            # Close normally
            event.accept()

    def update_hotkeys(self, record_key: str, cancel_key: str, enable_disable_key: str = "Ctrl+Alt+*"):
        """
        Update the hotkey display on buttons.
        
        Args:
            record_key: The key for recording
            cancel_key: The key for canceling
            enable_disable_key: The key for enabling/disabling STT
        """
        self.record_button.set_hotkey(record_key)
        self.cancel_button.set_hotkey(cancel_key)
        self.stop_button.set_hotkey(record_key) # Stop uses same key as record usually
