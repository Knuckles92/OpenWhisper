"""
Modern hotkey display widget with keyboard key styling.
"""
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QLabel, QGraphicsOpacityEffect
from PyQt6.QtCore import Qt, QPropertyAnimation, QEasingCurve, pyqtProperty
from PyQt6.QtGui import QFont, QColor


class HotkeyKey(QLabel):
    """A single hotkey key styled like a keyboard key with state-aware glow."""
    
    def __init__(self, text: str):
        super().__init__(text)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(24)
        self.setMinimumWidth(28)
        
        # State colors for border glow
        self._border_color = "#48484a"  # Default idle color
        self._state = "idle"
        
        # Setup opacity effect for semi-transparency
        self.opacity_effect = QGraphicsOpacityEffect(self)
        self.opacity_effect.setOpacity(0.90)
        self.setGraphicsEffect(self.opacity_effect)
        
        self._update_style()
    
    def _update_style(self):
        """Update the stylesheet with current border color."""
        self.setStyleSheet(f"""
            QLabel {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #3a3a3c, stop:1 #2c2c2e);
                color: #f5f5f7;
                border: 1px solid {self._border_color};
                border-radius: 6px;
                padding: 3px 8px;
                font-family: "Segoe UI", "SF Pro Display", sans-serif;
                font-size: 12px;
                font-weight: 600;
            }}
            QLabel:hover {{
                border: 1px solid #0a84ff;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 #4a4a4c, stop:1 #3c3c3e);
            }}
        """)
    
    def set_state(self, state: str):
        """
        Set the visual state of the key.
        
        Args:
            state: One of 'idle', 'recording', 'processing', 'canceling'
        """
        self._state = state
        
        # Map states to border colors
        color_map = {
            'idle': '#48484a',
            'recording': '#30d158',
            'processing': '#0a84ff',
            'canceling': '#ff453a'
        }
        
        target_color = color_map.get(state, '#48484a')
        
        # Animate color transition
        self._animate_border_color(target_color)
    
    def _animate_border_color(self, target_color: str):
        """Animate border color change smoothly."""
        self._border_color = target_color
        self._update_style()


class HotkeyLabel(QLabel):
    """Label describing what the hotkey does."""
    
    def __init__(self, text: str):
        super().__init__(text)
        self.setStyleSheet("""
            QLabel {
                color: #98989d;
                font-size: 11px;
                font-weight: 500;
                padding: 0 4px;
                background: transparent;
            }
        """)


class HotkeyDisplay(QWidget):
    """Modern hotkey display widget with keyboard key styling."""
    
    def __init__(self):
        super().__init__()
        self._hotkey_widgets = []
        self._setup_ui()
    
    def _setup_ui(self):
        """Setup the UI with styled hotkey buttons."""
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        
        # Keyboard icon instead of text label
        icon_label = QLabel("⌨")
        icon_label.setStyleSheet("""
            QLabel {
                color: #636366;
                font-size: 16px;
                padding-right: 4px;
                background: transparent;
            }
        """)
        layout.addWidget(icon_label)
        
        # Record hotkey
        record_key = HotkeyKey("*")
        self._hotkey_widgets.append(record_key)
        layout.addWidget(record_key)
        
        record_label = HotkeyLabel("rec")
        layout.addWidget(record_label)
        
        # Subtle spacer
        spacer1 = QLabel("·")
        spacer1.setStyleSheet("color: #3a3a3c; font-size: 12px; padding: 0 2px; background: transparent;")
        layout.addWidget(spacer1)
        
        # Cancel hotkey
        cancel_key = HotkeyKey("-")
        self._hotkey_widgets.append(cancel_key)
        layout.addWidget(cancel_key)
        
        cancel_label = HotkeyLabel("cancel")
        layout.addWidget(cancel_label)
        
        # Subtle spacer
        spacer2 = QLabel("·")
        spacer2.setStyleSheet("color: #3a3a3c; font-size: 12px; padding: 0 2px; background: transparent;")
        layout.addWidget(spacer2)
        
        # Enable/Disable hotkey
        enable_key = HotkeyKey("Ctrl+Alt+*")
        self._hotkey_widgets.append(enable_key)
        layout.addWidget(enable_key)
        
        enable_label = HotkeyLabel("toggle")
        layout.addWidget(enable_label)
        
        # Set transparent background for the widget itself
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("QWidget { background: transparent; }")
    
    def set_state(self, state: str):
        """
        Set the visual state of all hotkey buttons.
        
        Args:
            state: One of 'idle', 'recording', 'processing', 'canceling'
        """
        for widget in self._hotkey_widgets:
            if isinstance(widget, HotkeyKey):
                widget.set_state(state)
    
    def update_hotkeys(self, record_key: str, cancel_key: str, enable_disable_key: str = "Ctrl+Alt+*"):
        """
        Update the hotkey display with new keys with fade animation.
        
        Args:
            record_key: The key for recording
            cancel_key: The key for canceling
            enable_disable_key: The key for enabling/disabling STT
        """
        # Get the key widgets (indices 1, 4, and 7 in the layout)
        layout = self.layout()
        if layout.count() >= 8:
            record_widget = layout.itemAt(1).widget()
            cancel_widget = layout.itemAt(4).widget()
            enable_widget = layout.itemAt(7).widget()
            
            # Apply fade animation before updating text
            widgets_to_update = [
                (record_widget, record_key),
                (cancel_widget, cancel_key),
                (enable_widget, enable_disable_key.replace('ctrl', 'Ctrl').replace('alt', 'Alt').replace('shift', 'Shift'))
            ]
            
            for widget, new_text in widgets_to_update:
                if isinstance(widget, HotkeyKey):
                    self._fade_update_text(widget, new_text)
    
    def _fade_update_text(self, widget: HotkeyKey, new_text: str):
        """Fade out, update text, and fade in."""
        # Create fade out animation
        fade_out = QPropertyAnimation(widget.opacity_effect, b"opacity")
        fade_out.setDuration(150)
        fade_out.setStartValue(0.90)
        fade_out.setEndValue(0.30)
        fade_out.setEasingCurve(QEasingCurve.Type.OutCubic)
        
        # Create fade in animation
        fade_in = QPropertyAnimation(widget.opacity_effect, b"opacity")
        fade_in.setDuration(150)
        fade_in.setStartValue(0.30)
        fade_in.setEndValue(0.90)
        fade_in.setEasingCurve(QEasingCurve.Type.InCubic)
        
        # Update text when fade out finishes
        fade_out.finished.connect(lambda: widget.setText(new_text))
        fade_out.finished.connect(fade_in.start)
        
        # Start animation
        fade_out.start()
