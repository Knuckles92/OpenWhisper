"""
Card and container widgets for PyQt6 UI.
"""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont


class Card(QWidget):
    """Modern card container with rounded corners and border."""

    def __init__(self, parent=None):
        """Initialize card widget."""
        super().__init__(parent)
        self.setObjectName("card")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(16, 16, 16, 16) # Reduced from 20
        self.layout.setSpacing(12)
        self.setMinimumHeight(100)


class ControlPanel(QWidget):
    """Control panel with buttons and controls."""

    def __init__(self, parent=None):
        """Initialize control panel."""
        super().__init__(parent)
        self.setObjectName("controlPanel")
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(16, 16, 16, 16) # Reduced from 20
        self.layout.setSpacing(12)


class HeaderCard(Card):
    """Card with a header section."""

    def __init__(self, title: str = "", parent=None):
        """Initialize header card."""
        super().__init__(parent)
        # Stylesheet removed to allow QSS to control appearance

        # Create header
        self.header_layout = QHBoxLayout()
        self.header_layout.setContentsMargins(0, 0, 0, 0)
        self.header_layout.setSpacing(12)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("headerLabel")
        # Font size increased in QSS, removing hardcoded font here or ensuring it matches
        self.title_font = QFont("Segoe UI", 14)
        self.title_font.setBold(True)
        self.title_label.setFont(self.title_font)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.header_layout.addStretch()
        self.header_layout.addWidget(self.title_label)
        self.header_layout.addStretch()

        # Insert header at the beginning
        self.layout.insertLayout(0, self.header_layout)
        self.layout.insertSpacing(1, 12)

    def add_header_widget(self, widget):
        """Add a widget to the header."""
        self.header_layout.addWidget(widget)

    def set_title(self, title: str):
        """Set the header title."""
        self.title_label.setText(title)


class StatCard(Card):
    """Card for displaying statistics."""

    def __init__(self, label: str = "", value: str = "", parent=None):
        """Initialize stat card."""
        super().__init__(parent)

        self.label = QLabel(label)
        self.label.setObjectName("statusLabel")

        self.value = QLabel(value)
        self.value.setObjectName("accentLabel")
        self.value_font = QFont("Segoe UI", 24) # Increased size
        self.value_font.setBold(True)
        self.value.setFont(self.value_font)

        self.layout.addWidget(self.label)
        self.layout.addWidget(self.value)
        self.layout.addStretch()

    def set_value(self, value: str):
        """Update the stat value."""
        self.value.setText(value)
