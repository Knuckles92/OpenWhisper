"""
Card and container widgets for PyQt6 UI.
"""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QToolButton
from PyQt6.QtCore import Qt, pyqtSignal
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
    """Card with a header section.

    When ``collapsible`` is True, a chevron toggle is shown in the header and
    the body lives in a dedicated ``content_widget`` that hides on collapse,
    letting the card shrink to just its header to reclaim vertical space.
    """

    #: Emitted when the collapsed state changes (True == collapsed).
    toggled = pyqtSignal(bool)

    #: Qt's QWIDGETSIZE_MAX — restores an unconstrained max height on expand.
    _EXPANDED_MAX_HEIGHT = 16777215

    _TOGGLE_STYLE = (
        "QToolButton { color: #a0a0c0; border: none; font-size: 13px; "
        "padding: 0px; }"
        "QToolButton:hover { color: #c0c0ff; }"
    )

    def __init__(self, title: str = "", parent=None, collapsible: bool = False):
        """Initialize header card."""
        super().__init__(parent)
        # Stylesheet removed to allow QSS to control appearance

        self.collapsible = collapsible
        self._collapsed = False
        self._content_height = 0

        # Create header
        self.header_layout = QHBoxLayout()
        self.header_layout.setContentsMargins(0, 0, 0, 0)
        self.header_layout.setSpacing(8)

        self.title_label = QLabel(title)
        self.title_label.setObjectName("headerLabel")
        # Font size increased in QSS, removing hardcoded font here or ensuring it matches
        self.title_font = QFont("Segoe UI", 14)
        self.title_font.setBold(True)
        self.title_label.setFont(self.title_font)
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        if collapsible:
            self.toggle_button = QToolButton()
            self.toggle_button.setObjectName("cardCollapseButton")
            self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.toggle_button.setStyleSheet(self._TOGGLE_STYLE)
            self.toggle_button.setFixedWidth(20)
            self.toggle_button.clicked.connect(self.toggle_collapsed)

            # Spacer mirrors the toggle width so the title stays visually centred.
            self._header_spacer = QWidget()
            self._header_spacer.setFixedWidth(20)

            self.header_layout.addWidget(self.toggle_button)
            self.header_layout.addStretch()
            self.header_layout.addWidget(self.title_label)
            self.header_layout.addStretch()
            self.header_layout.addWidget(self._header_spacer)
        else:
            self.header_layout.addStretch()
            self.header_layout.addWidget(self.title_label)
            self.header_layout.addStretch()

        # Insert header at the beginning
        self.layout.insertLayout(0, self.header_layout)
        self.layout.insertSpacing(1, 12)

        if collapsible:
            self.content_widget = QWidget()
            self.content_layout = QVBoxLayout(self.content_widget)
            self.content_layout.setContentsMargins(0, 0, 0, 0)
            self.content_layout.setSpacing(12)
            self.layout.addWidget(self.content_widget)
            self._update_toggle_icon()

    def add_header_widget(self, widget):
        """Add a widget to the header."""
        self.header_layout.addWidget(widget)

    def add_content_widget(self, widget):
        """Add a widget to the card body (collapsible-aware)."""
        if self.collapsible:
            self.content_layout.addWidget(widget)
        else:
            self.layout.addWidget(widget)

    def set_title(self, title: str):
        """Set the header title."""
        self.title_label.setText(title)

    # ── Collapse support ───────────────────────────────────────────

    @property
    def is_collapsed(self) -> bool:
        """Whether the card body is currently collapsed."""
        return self._collapsed

    @property
    def content_height(self) -> int:
        """Height of the body captured at the last collapse (resize delta)."""
        return self._content_height

    def toggle_collapsed(self):
        """Flip the collapsed state (chevron click target)."""
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool, emit: bool = True):
        """Collapse or expand the card body.

        Args:
            collapsed: True to hide the body, False to show it.
            emit: Whether to emit the ``toggled`` signal (False during initial
                state restore so it doesn't trigger a window resize).
        """
        if not self.collapsible or collapsed == self._collapsed:
            return

        # Capture the body height before hiding so listeners can resize by it.
        if collapsed:
            self._content_height = (
                self.content_widget.height()
                or self.content_widget.sizeHint().height()
            )

        self._collapsed = collapsed
        self.content_widget.setVisible(not collapsed)

        if collapsed:
            # Drop the Card's 100px floor and clamp to the header height so the
            # card shrinks to just its title bar (reclaiming the body's space).
            self.setMinimumHeight(0)
            self.layout.activate()
            self.setMaximumHeight(self.sizeHint().height())
        else:
            self.setMinimumHeight(100)
            self.setMaximumHeight(self._EXPANDED_MAX_HEIGHT)

        self._update_toggle_icon()

        if emit:
            self.toggled.emit(collapsed)

    def _update_toggle_icon(self):
        """Update the chevron glyph and tooltip for the current state."""
        if not self.collapsible:
            return
        # ▾ expanded (points down), ▸ collapsed (points right)
        self.toggle_button.setText("▸" if self._collapsed else "▾")
        self.toggle_button.setToolTip(
            "Expand" if self._collapsed else "Collapse"
        )


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
