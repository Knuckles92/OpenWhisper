"""Left navigation rail whose items report the value they own.

Used by Settings instead of a tab bar: each destination shows its current
assignment underneath its name, so the whole configuration is readable without
navigating. Group headers are disabled items, which ``QAbstractItemView``
already skips during keyboard navigation.
"""
from typing import Dict, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

_KEY_ROLE = Qt.ItemDataRole.UserRole


class _RailItem(QWidget):
    """Name plus current-value row shown inside a rail item."""

    #: Fallback caption width before the list has laid out its rows.
    VALUE_WIDTH = 178

    def __init__(self, name: str, icon: Optional[QIcon], parent=None):
        super().__init__(parent)
        self.setObjectName("modelManagerRailItem")
        # A plain QWidget subclass does not paint a stylesheet background
        # without this, so the selected pill would be invisible.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._value = ""

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 7, 10, 7)
        row.setSpacing(9)

        self.icon_label = QLabel()
        self.icon_label.setObjectName("modelManagerRailIcon")
        self.icon_label.setFixedSize(18, 18)
        if icon is not None and not icon.isNull():
            self.icon_label.setPixmap(icon.pixmap(16, 16))
        row.addWidget(self.icon_label, alignment=Qt.AlignmentFlag.AlignTop)

        copy = QVBoxLayout()
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(2)
        self.name_label = QLabel(name)
        self.name_label.setObjectName("modelManagerRailName")
        self.value_label = QLabel("")
        self.value_label.setObjectName("modelManagerRailValue")
        self.value_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        copy.addWidget(self.name_label)
        copy.addWidget(self.value_label)
        row.addLayout(copy, stretch=1)

        self.set_selected(False)

    def value(self) -> str:
        """Return the full current value, before eliding."""
        return self._value

    def set_value(self, text: str) -> None:
        """Show an elided current value, with the full text as a tooltip."""
        self._value = text or ""
        metrics = self.value_label.fontMetrics()
        # Leave room for the row margins, icon, and spacing at any rail width.
        # A list item may briefly be wider than the viewport during resize.
        item_width = self.width()
        if self.parentWidget() is not None:
            item_width = min(item_width, self.parentWidget().width())
        available_width = item_width - 47 if item_width > 47 else self.VALUE_WIDTH
        self.value_label.setText(
            metrics.elidedText(
                self._value, Qt.TextElideMode.ElideRight, available_width
            )
        )
        self.value_label.setVisible(bool(self._value))
        self.setToolTip(self._value)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._value:
            self.set_value(self._value)

    def set_selected(self, selected: bool) -> None:
        """Repaint for selection.

        An item widget covers the view's own selection painting, so the state
        has to live on this widget for the stylesheet to see it.
        """
        self.setProperty("selected", selected)
        for widget in (self, self.name_label, self.value_label):
            widget.setProperty("selected", selected)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
            widget.update()


class NavRail(QListWidget):
    """Destination list with group headings and per-item value captions."""

    destination_changed = pyqtSignal(str)

    RAIL_WIDTH = 258

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("modelManagerRail")
        self.setMinimumWidth(220)
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setUniformItemSizes(False)
        self.setSpacing(1)
        self._rows: Dict[str, _RailItem] = {}
        self._items: Dict[str, QListWidgetItem] = {}
        self.currentItemChanged.connect(self._on_current_changed)

    def add_group(self, title: str) -> None:
        item = QListWidgetItem(self)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        # Qt stylesheets have no text-transform, so the eyebrow case is set here.
        label = QLabel(title.upper())
        label.setObjectName("modelManagerRailGroup")
        label.setContentsMargins(10, 10, 10, 3)
        item.setSizeHint(label.sizeHint())
        self.setItemWidget(item, label)

    def add_destination(
        self, key: str, name: str, icon: Optional[QIcon] = None
    ) -> None:
        item = QListWidgetItem(self)
        item.setData(_KEY_ROLE, key)
        row = _RailItem(name, icon)
        item.setSizeHint(QSize(0, row.sizeHint().height()))
        self.setItemWidget(item, row)
        self._rows[key] = row
        self._items[key] = item

    def set_value(self, key: str, text: str) -> None:
        row = self._rows.get(key)
        if row is not None:
            row.set_value(text)

    def value(self, key: str) -> str:
        """Return the full value shown under a destination's name."""
        row = self._rows.get(key)
        return row.value() if row is not None else ""

    def name(self, key: str) -> str:
        """Return a destination's display name."""
        row = self._rows.get(key)
        return row.name_label.text() if row is not None else ""

    def is_selected(self, key: str) -> bool:
        return bool(self._rows[key].property("selected")) if key in self._rows else False

    def select(self, key: str) -> None:
        item = self._items.get(key)
        if item is not None:
            self.setCurrentItem(item)

    def current_key(self) -> str:
        item = self.currentItem()
        if item is None:
            return ""
        return item.data(_KEY_ROLE) or ""

    def keys(self) -> tuple:
        return tuple(self._items)

    def _on_current_changed(
        self,
        current: Optional[QListWidgetItem],
        _previous: Optional[QListWidgetItem],
    ) -> None:
        for key, row in self._rows.items():
            row.set_selected(self._items[key] is current)
        if current is None:
            return
        key = current.data(_KEY_ROLE)
        if key:
            self.destination_changed.emit(key)
