"""
Modern Hotkey Configuration Dialog for PyQt6 UI.

Hotkey capture is platform-specific: the pynput backend (macOS/Linux) tracks
held modifiers and finalizes on the first non-modifier press, while the Windows
``keyboard`` backend reads the event stream until key-up. Both capture threads
expose the same ``captured``/``failed`` signals and ``stop()`` method so the
dialog body is platform-agnostic.
"""
import logging
from typing import Optional, Callable, Dict

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QWidget
)
from PyQt6.QtCore import pyqtSignal, QThread
from PyQt6.QtGui import QMouseEvent

from config import config
from services.hotkey_manager import USE_PYNPUT_BACKEND, format_hotkey_display
from ui_qt.widgets import PrimaryButton, Button

if USE_PYNPUT_BACKEND:
    from services.hotkey_manager import (
        modifier_of,
        key_to_name,
        format_hotkey,
        get_listener_class,
    )
else:
    import keyboard

logger = logging.getLogger(__name__)

if USE_PYNPUT_BACKEND:
    HOTKEY_CAPTURE_FAILURE_MESSAGE = (
        "Could not capture hotkey. Enable Accessibility and Input Monitoring "
        "permissions for OpenWhisper in macOS System Settings, then try again."
    )
else:
    HOTKEY_CAPTURE_FAILURE_MESSAGE = "Could not capture hotkey. Please try again."


class ClickableLineEdit(QLineEdit):
    """QLineEdit that emits a clicked signal when clicked."""
    clicked = pyqtSignal()

    def mousePressEvent(self, event: QMouseEvent):
        """Emit clicked signal on mouse press."""
        self.clicked.emit()
        super().mousePressEvent(event)


if USE_PYNPUT_BACKEND:

    class HotkeyCaptureThread(QThread):
        """Capture a single hotkey via a pynput listener (macOS/Linux).

        Modifiers (cmd/ctrl/alt/shift) are tracked as they are held, and the
        first non-modifier key press finalizes the combo. The emitted string
        uses the same canonical format the matcher parses, so any captured
        hotkey round-trips exactly.
        """
        captured = pyqtSignal(str)
        failed = pyqtSignal(str)

        def __init__(self, parent=None):
            super().__init__(parent)
            self._listener = None
            self._canceled = False

        def run(self):
            """Run the capture."""
            self._canceled = False
            pressed_modifiers = set()
            result = {"hotkey": None}

            def on_press(key):
                modifier = modifier_of(key)
                if modifier is not None:
                    pressed_modifiers.add(modifier)
                    return True

                name = key_to_name(key)
                if name is None:
                    return True

                result["hotkey"] = format_hotkey(frozenset(pressed_modifiers), name)
                return False  # Stop the listener once a main key is captured.

            def on_release(key):
                modifier = modifier_of(key)
                if modifier is not None:
                    pressed_modifiers.discard(modifier)
                return True

            try:
                # Use the macOS-safe listener (skips keycode_context / HIToolbox
                # on a background thread — stock pynput SIGTRAPs on macOS 26+).
                listener_class = get_listener_class()
                self._listener = listener_class(
                    on_press=on_press,
                    on_release=on_release,
                    suppress=False,
                )
                self._listener.start()
                self._listener.join()
                if result["hotkey"]:
                    self.captured.emit(result["hotkey"])
                elif not self._canceled:
                    logger.error("Hotkey capture listener stopped without capturing a key")
                    self.failed.emit(HOTKEY_CAPTURE_FAILURE_MESSAGE)
            except Exception as e:
                if not self._canceled:
                    logger.error(f"Error capturing hotkey: {e}")
                    self.failed.emit(HOTKEY_CAPTURE_FAILURE_MESSAGE)

        def stop(self):
            """Stop the underlying listener (cancels an in-progress capture)."""
            self._canceled = True
            listener = self._listener
            if listener is not None:
                try:
                    listener.stop()
                except Exception:
                    pass

else:

    class HotkeyCaptureThread(QThread):
        """Capture a single hotkey via the Windows ``keyboard`` library."""
        captured = pyqtSignal(str)
        failed = pyqtSignal(str)

        def run(self):
            """Run the capture."""
            try:
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
                self.failed.emit(HOTKEY_CAPTURE_FAILURE_MESSAGE)

        def stop(self):
            """Stop the capture by unhooking and terminating the thread."""
            try:
                keyboard.unhook_all()
            except Exception:
                pass
            self.terminate()


class HotkeyDialog(QDialog):
    """Modern hotkey configuration dialog."""

    hotkeys_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        """Initialize hotkey dialog."""
        super().__init__(parent)
        self.setWindowTitle("Hotkey Configuration")
        self.setMinimumSize(500, 500)

        # State
        self.current_hotkeys: Dict[str, str] = {}
        self.capturing = None
        self.capture_thread: Optional[HotkeyCaptureThread] = None
        self.current_input_field: Optional[ClickableLineEdit] = None

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
        title.setObjectName("headerLabel")
        layout.addWidget(title)

        # Instructions
        if USE_PYNPUT_BACKEND:
            instructions_text = (
                "Click on a field to record a new hotkey.\n"
                "Hold your modifiers (⌘ ⌃ ⌥ ⇧) and press a key.\n"
                "Tip: Control+Option combos avoid clashing with system shortcuts."
            )
        else:
            instructions_text = (
                "Click on a field to record a new hotkey.\n"
                "Press the desired key combination.\n"
                "Note: Numpad keys (kp 1, kp *, etc.) are distinct from regular keys."
            )
        instructions = QLabel(instructions_text)
        instructions.setObjectName("infoLabel")
        layout.addWidget(instructions)

        layout.addSpacing(12)

        # Record toggle hotkey
        record_label = QLabel("Record Toggle:")
        layout.addWidget(record_label)

        self.record_input = self._create_hotkey_input()
        self.record_input.clicked.connect(lambda: self._start_capture("record_toggle", self.record_input))
        layout.addWidget(self.record_input)

        layout.addSpacing(12)

        # Cancel hotkey
        cancel_label = QLabel("Cancel Recording:")
        layout.addWidget(cancel_label)

        self.cancel_input = self._create_hotkey_input()
        self.cancel_input.clicked.connect(lambda: self._start_capture("cancel", self.cancel_input))
        layout.addWidget(self.cancel_input)

        layout.addSpacing(12)

        # Enable/Disable hotkey
        enable_label = QLabel("Enable/Disable:")
        layout.addWidget(enable_label)

        self.enable_input = self._create_hotkey_input()
        self.enable_input.clicked.connect(lambda: self._start_capture("enable_disable", self.enable_input))
        layout.addWidget(self.enable_input)

        layout.addSpacing(12)

        # Minimize-to-tray hotkey
        minimize_label = QLabel("Minimize to Tray:")
        layout.addWidget(minimize_label)

        self.minimize_input = self._create_hotkey_input()
        self.minimize_input.clicked.connect(lambda: self._start_capture("minimize_tray", self.minimize_input))
        layout.addWidget(self.minimize_input)

        layout.addSpacing(16)

        # Reset button
        reset_btn = Button("Reset to Defaults")
        reset_btn.setMaximumWidth(200)
        reset_btn.clicked.connect(self._reset_to_defaults)
        layout.addWidget(reset_btn)

        layout.addStretch()

        # Button layout
        button_layout = QHBoxLayout()
        button_layout.setSpacing(8)

        button_layout.addStretch()

        cancel_btn = Button("Cancel")
        cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(cancel_btn)

        save_btn = PrimaryButton("Save Hotkeys")
        save_btn.clicked.connect(self._save_hotkeys)
        button_layout.addWidget(save_btn)

        layout.addLayout(button_layout)

    def _create_hotkey_input(self) -> ClickableLineEdit:
        """Create a hotkey input field styled by the app-wide theme."""
        input_field = ClickableLineEdit()
        input_field.setObjectName("hotkeyInput")
        input_field.setReadOnly(True)
        input_field.setMinimumHeight(36)
        input_field.setPlaceholderText("Click to set hotkey")
        return input_field

    @staticmethod
    def _set_capturing_state(input_field: QWidget, capturing: bool):
        """Toggle the QSS ``capturing`` property and re-polish the widget."""
        input_field.setProperty("capturing", capturing)
        input_field.style().unpolish(input_field)
        input_field.style().polish(input_field)

    def _start_capture(self, hotkey_type: str, input_field: ClickableLineEdit):
        """Start capturing a hotkey."""
        try:
            # If already capturing, stop previous capture
            if self.capture_thread and self.capture_thread.isRunning():
                self.capture_thread.stop()
                self.capture_thread.wait(1000)  # Wait with timeout
                self._reset_input_styles()

            self.capturing = hotkey_type
            self.current_input_field = input_field

            input_field.setText("Press keys...")
            self._set_capturing_state(input_field, True)

            logger.info(f"Capturing hotkey for: {hotkey_type}")

            # Start capture thread
            self.capture_thread = HotkeyCaptureThread()
            self.capture_thread.captured.connect(self._on_hotkey_captured)
            self.capture_thread.failed.connect(self._on_hotkey_capture_failed)
            self.capture_thread.start()
        except Exception as e:
            logger.error(f"Failed to start hotkey capture: {e}")
            self._reset_input_styles()
            self._update_displays()
            self.capturing = None
            self.current_input_field = None
            QMessageBox.warning(self, "Hotkey Capture Failed", HOTKEY_CAPTURE_FAILURE_MESSAGE)

    def _on_hotkey_captured(self, hotkey: str):
        """Handle captured hotkey."""
        if not self.capturing or not self.current_input_field:
            return

        logger.info(f"Captured hotkey: {hotkey}")

        # Update state
        self.current_hotkeys[self.capturing] = hotkey
        self.current_input_field.setText(format_hotkey_display(hotkey))

        # Reset UI
        self._reset_input_styles()
        self.capturing = None
        self.current_input_field = None

    def _on_hotkey_capture_failed(self, message: str):
        """Handle hotkey capture failures."""
        logger.warning(message)
        self._reset_input_styles()
        self._update_displays()
        self.capturing = None
        self.current_input_field = None
        QMessageBox.warning(self, "Hotkey Capture Failed", message)

    def _reset_input_styles(self):
        """Reset all input fields to the default (non-capturing) style."""
        for input_field in (self.record_input, self.cancel_input,
                            self.enable_input, self.minimize_input):
            self._set_capturing_state(input_field, False)

    def _reset_to_defaults(self):
        """Reset hotkeys to default values."""
        self.current_hotkeys = config.DEFAULT_HOTKEYS.copy()
        self._update_displays()
        logger.info("Hotkeys reset to defaults")

    def _load_hotkeys(self):
        """Load current hotkey settings."""
        self.current_hotkeys = config.DEFAULT_HOTKEYS.copy()
        # Load saved overrides on top of the platform defaults.
        try:
            from services.settings import settings_manager
            saved_hotkeys = settings_manager.load_hotkey_settings()
            self.current_hotkeys.update(saved_hotkeys)
        except ImportError:
            pass

        self._update_displays()

    def _update_displays(self):
        """Update the input field displays."""
        defaults = config.DEFAULT_HOTKEYS
        self.record_input.setText(
            format_hotkey_display(self.current_hotkeys.get("record_toggle", defaults["record_toggle"]))
        )
        self.cancel_input.setText(
            format_hotkey_display(self.current_hotkeys.get("cancel", defaults["cancel"]))
        )
        self.enable_input.setText(
            format_hotkey_display(self.current_hotkeys.get("enable_disable", defaults["enable_disable"]))
        )
        self.minimize_input.setText(
            format_hotkey_display(self.current_hotkeys.get("minimize_tray", defaults["minimize_tray"]))
        )

    def _save_hotkeys(self):
        """Save hotkey settings."""
        if self.on_hotkeys_save:
            self.on_hotkeys_save(self.current_hotkeys)

        self.hotkeys_changed.emit(self.current_hotkeys)
        logger.info("Hotkeys saved")
        self.accept()

    def closeEvent(self, event):
        """Handle close event."""
        try:
            if self.capture_thread and self.capture_thread.isRunning():
                self.capture_thread.stop()
                self.capture_thread.wait(1000)  # Wait with timeout
        except Exception as e:
            logger.debug(f"Error stopping capture thread: {e}")
        finally:
            super().closeEvent(event)
