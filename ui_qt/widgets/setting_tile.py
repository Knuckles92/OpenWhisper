"""Rounded tiles that pair one setting with its explanation."""
from typing import Optional

from PyQt6.QtCore import QEvent, Qt
from PyQt6.QtGui import QIcon, QMouseEvent
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui_qt.widgets.eliding_label import ElidingLabel
from ui_qt.widgets.wrapped_label import WrappedLabel


class TileBase(QFrame):
    """Icon, title, and description row with a trailing slot and a body.

    The trailing slot sits to the right of the text; the body is an indented
    column under it for controls the setting owns, so disabling the tile
    disables everything the setting depends on.
    """

    ICON_SIZE = 20

    def __init__(
        self,
        title: str,
        description: str,
        icon: Optional[QIcon] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("settingsTile")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        column = QVBoxLayout(self)
        column.setContentsMargins(14, 12, 14, 12)
        column.setSpacing(10)

        self._row = QHBoxLayout()
        self._row.setContentsMargins(0, 0, 0, 0)
        self._row.setSpacing(12)

        self.icon_label = QLabel()
        self.icon_label.setObjectName("settingsTileIcon")
        self.icon_label.setFixedSize(self.ICON_SIZE, self.ICON_SIZE)
        if icon is not None:
            self.icon_label.setPixmap(icon.pixmap(self.ICON_SIZE, self.ICON_SIZE))
        self._row.addWidget(self.icon_label, alignment=Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(3)
        # A plain QLabel reports its text width as a minimum, which lets one
        # long title widen its grid column and unbalance the row.
        self.title_label = ElidingLabel(title)
        self.title_label.setObjectName("settingsTileTitle")
        text.addWidget(self.title_label)
        self.description_label = WrappedLabel(description)
        self.description_label.setObjectName("settingsTileDescription")
        text.addWidget(self.description_label)
        # Pin the text to the top instead of adding a stretch: an expanding
        # spacer would make the tile itself expanding, and the page layout
        # would then pad every tile row with the spare height.
        self._row.addLayout(text, stretch=1)
        self._row.setAlignment(text, Qt.AlignmentFlag.AlignTop)
        column.addLayout(self._row)

        self.body = QWidget()
        self.body.setObjectName("settingsTileBody")
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(self.ICON_SIZE + 12, 0, 0, 0)
        self._body_layout.setSpacing(8)
        self.body.hide()
        column.addWidget(self.body)

    def set_description(self, text: str) -> None:
        self.description_label.setText(text)

    def add_trailing(self, widget: QWidget) -> None:
        self._row.addWidget(widget, alignment=Qt.AlignmentFlag.AlignVCenter)

    def add_body(self, widget: QWidget) -> None:
        self._body_layout.addWidget(widget)
        self.body.show()

    def add_body_layout(self, layout) -> None:
        self._body_layout.addLayout(layout)
        self.body.show()


class SettingTile(TileBase):
    """Tile for an on/off setting; the whole card toggles its checkbox.

    The ``checked`` dynamic property mirrors the checkbox so the theme can
    tint enabled tiles.
    """

    def __init__(
        self,
        title: str,
        description: str,
        icon: Optional[QIcon] = None,
        parent=None,
    ):
        super().__init__(title, description, icon, parent)
        self.setProperty("kind", "toggle")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.checkbox = QCheckBox()
        self.checkbox.setObjectName("settingsTileCheck")
        self.checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
        self.checkbox.toggled.connect(self._sync_checked_property)
        self.add_trailing(self.checkbox)
        self.setProperty("checked", False)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.checkbox.toggle()
            event.accept()
            return
        super().mousePressEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            cursor = (
                Qt.CursorShape.PointingHandCursor
                if self.isEnabled()
                else Qt.CursorShape.ArrowCursor
            )
            self.setCursor(cursor)

    def _sync_checked_property(self, checked: bool) -> None:
        self.setProperty("checked", bool(checked))
        style = self.style()
        style.unpolish(self)
        style.polish(self)


class FieldTile(TileBase):
    """Tile for a setting with a value control instead of a checkbox.

    ``compact`` places the control beside the text (spin boxes); otherwise it
    fills the body under the description (combos with long item text).
    """

    def __init__(
        self,
        title: str,
        description: str,
        control: QWidget,
        icon: Optional[QIcon] = None,
        compact: bool = False,
        parent=None,
    ):
        super().__init__(title, description, icon, parent)
        self.setProperty("kind", "field")
        self.control = control
        if compact:
            self.add_trailing(control)
        else:
            self.add_body(control)
