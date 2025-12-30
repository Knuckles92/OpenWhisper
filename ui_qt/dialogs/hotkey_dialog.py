"""
Modern Hotkey Configuration Dialog for PyQt6 UI.
"""
import logging
import keyboard
from typing import Optional, Callable, Dict
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QFrame
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QThread
from PyQt6.QtGui import QFont, QKeySequence, QMouseEvent

from config import config
from ui_qt.widgets import PrimaryButton, ModernButton, HeaderCard


class ClickableLineEdit(QLineEdit):
    """QLineEdit that emits a clicked signal when clicked."""
    clicked = pyqtSignal()
    
    def mousePressEvent(self, event: QMouseEvent):
        """Emit clicked signal on mouse press."""
        self.clicked.emit()
        super().mousePressEvent(event)


class HotkeyCaptureThread(QThread):
    """Thread to capture a single hotkey without blocking UI."""
    captured = pyqtSignal(str)

    def run(self):
        """Run the capture."""
        try:
            # read_hotkey blocks until a hotkey is pressed
            # suppress=False to let the key event pass through if needed,
            # but here we probably want to consume it or just read it.
            # Using suppress=True might block other apps, but for configuration it's okay.
            events = []
            queue = keyboard._queue.Queue()
            fn = lambda e: queue.put(e) or e.event_type == keyboard.KEY_DOWN
            hooked = keyboard.hook(fn, suppress=False)
            while True:
                event = queue.get()
                events.append(event)
                if event.event_type == keyboard.KEY_UP:
                    keyboard.unhook(hooked)
                    names = [(e.name if not e.is_keypad else f"kp_{e.name}") for e in events]
                    self.captured.emit(keyboard.get_hotkey_name(names))
                    break
                                
        except Exception as e:
            logging.error(f"Error capturing hotkey: {e}")


class HotkeyDialog(QDialog):
    """Modern hotkey configuration dialog."""

    hotkeys_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        """Initialize hotkey dialog."""
        super().__init__(parent)
        self.logger = logging.getLogger(__name__)
        self.setWindowTitle("Hotkey Configuration")
        self.setMinimumSize(500, 400)

        # State
        self.current_hotkeys: Dict[str, str] = {}
        self.capturing = None
        self.capture_thread: Optional[HotkeyCaptureThread] = None

        # Callbacks
        self.on_hotkeys_save: Optional[Callable] = None

        self._setup_ui()
        self._load_hotkeys()

    def _setup_ui(self):
        """Setup the user interface."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        # Header
        title = QLabel("Hotkey Configuration")
        title_font = QFont("Segoe UI", 14)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #e0e0ff;")
        layout.addWidget(title)

        # Instructions
        instructions = QLabel(
            "Click on a field to record a new hotkey.\n"
            "Press the desired key combination.\n"
            "Note: Numpad keys (kp 1, kp *, etc.) are distinct from regular keys."
        )
        instructions.setStyleSheet("color: #a0a0c0;")
        instructions.setFont(QFont("Segoe UI", 10))
        layout.addWidget(instructions)

        layout.addSpacing(12)

        # Record toggle hotkey
        record_label = QLabel("Record Toggle:")
        record_label.setStyleSheet("color: #e0e0ff;")
        record_label.setFont(QFont("Segoe UI", 11))
        layout.addWidget(record_label)

        self.record_input = self._create_hotkey_input()
        self.record_input.clicked.connect(lambda: self._start_capture("record_toggle", self.record_input))
        layout.addWidget(self.record_input)

        layout.addSpacing(12)

        # Cancel hotkey
        cancel_label = QLabel("Cancel Recording:")
        cancel_label.setStyleSheet("color: #e0e0ff;")
        cancel_label.setFont(QFont("Segoe UI", 11))
        layout.addWidget(cancel_label)

        self.cancel_input = self._create_hotkey_input()
        self.cancel_input.clicked.connect(lambda: self._start_capture("cancel", self.cancel_input))
        layout.addWidget(self.cancel_input)

        layout.addSpacing(12)

        # Enable/Disable hotkey
        enable_label = QLabel("Enable/Disable:")
        enable_label.setStyleSheet("color: #e0e0ff;")
        enable_label.setFont(QFont("Segoe UI", 11))
        layout.addWidget(enable_label)

        self.enable_input = self._create_hotkey_input()
        self.enable_input.clicked.connect(lambda: self._start_capture("enable_disable", self.enable_input))
        layout.addWidget(self.enable_input)

        layout.addSpacing(16)

        # Reset button
        reset_btn = ModernButton("Reset to Defaults")
        reset_btn.setMaximumWidth(200)
        reset_btn.clicked.connect(self._reset_to_defaults)
        layout.addWidget(reset_btn)

        layout.addStretch()

        # Button layout
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)

        button_layout.addStretch()

        cancel_btn = ModernButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        save_btn = PrimaryButton("Save Hotkeys")
        save_btn.clicked.connect(self._save_hotkeys)
        button_layout.addWidget(save_btn)

        layout.addLayout(button_layout)

        # Apply styling
        self.setStyleSheet("""
            HotkeyDialog {
                background-color: #1e1e2e;
                border-radius: 8px;
            }
        """)

    def _create_hotkey_input(self) -> ClickableLineEdit:
        """Create a hotkey input field."""
        input_field = ClickableLineEdit()
        input_field.setReadOnly(True)
        input_field.setMinimumHeight(36)
        input_field.setFont(QFont("Segoe UI", 10))
        input_field.setPlaceholderText("Click to set hotkey")
        input_field.setStyleSheet("""
            QLineEdit {
                background-color: #2d2d44;
                color: #00d4ff;
                border: 1px solid #404060;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: bold;
            }
            QLineEdit:focus {
                border: 2px solid #00d4ff;
            }
        """)
        # Store original style for reset
        input_field.setProperty("original_style", input_field.styleSheet())
        return input_field

    def _start_capture(self, hotkey_type: str, input_field: ClickableLineEdit):
        """Start capturing a hotkey."""
        try:
            # If already capturing, stop previous capture
            if self.capture_thread and self.capture_thread.isRunning():
                self.capture_thread.terminate()
                self.capture_thread.wait(1000)  # Wait with timeout
                self._reset_input_styles()

            self.capturing = hotkey_type
            self.current_input_field = input_field
            
            input_field.setText("Press keys...")
            input_field.setStyleSheet("""
                QLineEdit {
                    background-color: #6366f1;
                    color: #ffffff;
                    border: 2px solid #00d4ff;
                    border-radius: 6px;
                    padding: 6px 12px;
                    font-weight: bold;
                }
            """)

            self.logger.info(f"Capturing hotkey for: {hotkey_type}")
            
            # Start capture thread
            self.capture_thread = HotkeyCaptureThread()
            self.capture_thread.captured.connect(self._on_hotkey_captured)
            self.capture_thread.start()
        except Exception as e:
            self.logger.error(f"Failed to start hotkey capture: {e}")
            self._reset_input_styles()
            self.capturing = None

    def _on_hotkey_captured(self, hotkey: str):
        """Handle captured hotkey."""
        if not self.capturing or not self.current_input_field:
            return

        self.logger.info(f"Captured hotkey: {hotkey}")
        
        # Update state
        self.current_hotkeys[self.capturing] = hotkey
        self.current_input_field.setText(hotkey)
        
        # Reset UI
        self._reset_input_styles()
        self.capturing = None
        self.current_input_field = None

    def _reset_input_styles(self):
        """Reset all input fields to default style."""
        style = """
            QLineEdit {
                background-color: #2d2d44;
                color: #00d4ff;
                border: 1px solid #404060;
                border-radius: 6px;
                padding: 6px 12px;
                font-weight: bold;
            }
            QLineEdit:focus {
                border: 2px solid #00d4ff;
            }
        """
        self.record_input.setStyleSheet(style)
        self.cancel_input.setStyleSheet(style)
        self.enable_input.setStyleSheet(style)

    def _reset_to_defaults(self):
        """Reset hotkeys to default values."""
        self.current_hotkeys = config.DEFAULT_HOTKEYS.copy()
        self._update_displays()
        self.logger.info("Hotkeys reset to defaults")

    def _load_hotkeys(self):
        """Load current hotkey settings."""
        self.current_hotkeys = config.DEFAULT_HOTKEYS.copy()
        # Load from settings if available (passed via parent or config)
        # For now we use defaults as base, but in real app we should load from settings_manager
        # However, the dialog is initialized with defaults. 
        # Ideally we should pass current hotkeys to __init__ or load them here.
        # Let's try to load from settings_manager if possible, or rely on what's passed.
        # Since we don't have direct access to settings_manager here (to avoid circular imports if any),
        # we'll rely on the fact that we are setting them.
        # Actually, let's import settings_manager to be safe.
        try:
            from services.settings import settings_manager
            saved_hotkeys = settings_manager.load_hotkey_settings()
            self.current_hotkeys.update(saved_hotkeys)
        except ImportError:
            pass
            
        self._update_displays()

    def _update_displays(self):
        """Update the input field displays."""
        self.record_input.setText(self.current_hotkeys.get("record_toggle", "kp *"))
        self.cancel_input.setText(self.current_hotkeys.get("cancel", "kp -"))
        self.enable_input.setText(self.current_hotkeys.get("enable_disable", "ctrl+alt+kp *"))

    def _save_hotkeys(self):
        """Save hotkey settings."""
        if self.on_hotkeys_save:
            self.on_hotkeys_save(self.current_hotkeys)

        self.hotkeys_changed.emit(self.current_hotkeys)
        self.logger.info("Hotkeys saved")
        self.accept()

    def closeEvent(self, event):
        """Handle close event."""
        try:
            if self.capture_thread and self.capture_thread.isRunning():
                self.capture_thread.terminate()
                self.capture_thread.wait(1000)  # Wait with timeout
        except Exception as e:
            self.logger.debug(f"Error stopping capture thread: {e}")
        finally:
            super().closeEvent(event)
