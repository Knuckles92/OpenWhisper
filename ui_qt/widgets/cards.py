"""
Card and container widgets for PyQt6 UI.
"""
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from ui_qt.widgets.collapsible_header import CollapsibleSectionToggle
from ui_qt.utils.collapse_animation import (
    UNLIMITED_HEIGHT,
    create_max_height_animation,
    run_max_height_animation,
)


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
    _EXPANDED_MAX_HEIGHT = UNLIMITED_HEIGHT

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

        self.title_label = None
        self.section_toggle = None

        if collapsible:
            self.section_toggle = CollapsibleSectionToggle(
                title,
                expanded=False,
                expand_tooltip="Show transcription output",
                collapse_tooltip="Hide transcription output",
            )
            self.section_toggle.toggled_expanded.connect(self._on_section_toggled)

            self.header_layout.addStretch()
            self.header_layout.addWidget(self.section_toggle)
            self.header_layout.addStretch()
        else:
            self.title_label = QLabel(title)
            self.title_label.setObjectName("headerLabel")
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

        if collapsible:
            self.content_widget = QWidget()
            self.content_layout = QVBoxLayout(self.content_widget)
            self.content_layout.setContentsMargins(0, 0, 0, 0)
            self.content_layout.setSpacing(12)
            self.layout.addWidget(self.content_widget)
            self.setMinimumHeight(0)

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
        if self.section_toggle is not None:
            self.section_toggle.set_title(title)
        elif self.title_label is not None:
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
        """Flip the collapsed state (header click target)."""
        self.set_collapsed(not self._collapsed)

    def _on_section_toggled(self, expanded: bool):
        """Handle user click on the shared section toggle."""
        self.set_collapsed(not expanded)

    def set_collapsed(self, collapsed: bool, emit: bool = True):
        """Collapse or expand the card body.

        Args:
            collapsed: True to hide the body, False to show it.
            emit: Whether to emit the ``toggled`` signal (False during initial
                state restore so it doesn't trigger a window resize).
        """
        if not self.collapsible or collapsed == self._collapsed:
            return

        if collapsed:
            self._content_height = max(
                self.content_widget.height(),
                self.content_widget.sizeHint().height(),
            )

        self._collapsed = collapsed

        if self.section_toggle is not None:
            self.section_toggle.set_expanded(not collapsed, emit=False)

        if emit:
            self.toggled.emit(collapsed)
            self._animate_content_visibility(collapsed)
        else:
            self._apply_collapsed_immediate(collapsed)

    def _apply_collapsed_immediate(self, collapsed: bool):
        """Apply collapsed state instantly (sync/initial setup)."""
        if hasattr(self, "_content_anim") and self._content_anim is not None:
            self._content_anim.stop()
        self.content_widget.setVisible(not collapsed)
        self.content_widget.setMaximumHeight(self._EXPANDED_MAX_HEIGHT)
        self._apply_card_size_policy(collapsed)

    def _content_animation(self):
        if not hasattr(self, "_content_anim") or self._content_anim is None:
            self._content_anim = create_max_height_animation(self.content_widget)
        return self._content_anim

    def _content_natural_height(self) -> int:
        """Natural height of the card body."""
        self.content_widget.setVisible(True)
        self.content_widget.adjustSize()
        return max(
            self.content_widget.sizeHint().height(),
            self.content_widget.minimumSizeHint().height(),
        )

    def _apply_card_size_policy(self, collapsed: bool):
        """Apply final card height clamps after animated transitions."""
        if collapsed:
            self.setMinimumHeight(0)
            self.layout.activate()
            self.setMaximumHeight(self.sizeHint().height())
        else:
            self.setMinimumHeight(0)
            self.setMaximumHeight(self._EXPANDED_MAX_HEIGHT)
            self.updateGeometry()

    def _animate_content_visibility(self, collapsed: bool):
        """Animate the body height in parallel with the window resize."""
        natural = self._content_natural_height()
        self.content_widget.setVisible(True)
        self.setMinimumHeight(0)
        self.setMaximumHeight(self._EXPANDED_MAX_HEIGHT)
        self.content_widget.setMinimumHeight(0)

        if collapsed:
            start = self.content_widget.height() or natural
            end = 0
        else:
            start = 0
            end = natural

        def on_finished():
            self.content_widget.setMaximumHeight(self._EXPANDED_MAX_HEIGHT)
            if collapsed:
                self.content_widget.setVisible(False)
            self._apply_card_size_policy(collapsed)

        run_max_height_animation(
            self._content_animation(),
            start=start,
            end=end,
            on_finished=on_finished,
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
