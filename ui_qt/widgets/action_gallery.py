"""Add action: a gallery of starting points, each a card you can click."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout, QWidget

from services.actions.model import AGENT_ACTION_ID, PRESETS, ActionKind, Preset
from ui_qt.utils.font_scale import current_ui_font_scale
from ui_qt.utils.icons import design_icon
from ui_qt.widgets.action_fields import hint_label, page_link, tint_icon
from ui_qt.widgets.setting_tile import TileBase

#: A link of your own, for anything the named presets don't cover.
OWN_LINK = Preset(
    "own-link", "Your own link", ActionKind.OPEN_LINK, {"template": ""},
    "Open any web address or app link with your words in it.", "world-blue.svg",
)
#: Tiles sit two to a row from this page width (at 100% font scale).
TWO_COLUMNS_FROM = 600


def gallery_presets() -> tuple[Preset, ...]:
    """The presets to offer; the agent hotkey has its own card above."""
    presets = [preset for preset in PRESETS if preset.id != AGENT_ACTION_ID]
    links = [index for index, preset in enumerate(presets) if preset.kind == ActionKind.OPEN_LINK]
    presets.insert(links[-1] + 1 if links else len(presets), OWN_LINK)
    return tuple(presets)


class PresetTile(TileBase):
    """One starting point; click, Enter or Space picks it."""

    clicked = pyqtSignal(object)

    def __init__(self, preset: Preset, parent=None):
        super().__init__(preset.name, preset.description, design_icon(preset.icon), parent)
        self.preset = preset
        self.setProperty("kind", "toggle")
        self.setProperty("tileId", "actionPreset")
        tint_icon(self, preset.icon)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(preset.name)
        self.setAccessibleDescription(preset.description)
        if preset.note:
            note = hint_label(preset.note)
            note.setProperty("tone", "warning")
            self.add_body(note)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            self.clicked.emit(self.preset)
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            event.accept()
            self.clicked.emit(self.preset)
            return
        super().keyPressEvent(event)


class PresetGallery(QWidget):
    """Choose what a new action starts as; the editor opens with it filled in."""

    chosen = pyqtSignal(object)
    cancelled = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("tileId", "actionGallery")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        header = QWidget()
        header.setProperty("tileId", "actionRow")
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        title = QLabel("Choose a starting point")
        title.setObjectName("actionSectionLabel")
        row.addWidget(title)
        row.addStretch(1)
        self.cancel_link = page_link("Cancel", "Close without adding an action")
        self.cancel_link.clicked.connect(self.cancelled)
        row.addWidget(self.cancel_link)
        layout.addWidget(header)
        self.grid_box = QWidget()
        self.grid_box.setProperty("tileId", "actionRow")
        self.grid = QGridLayout(self.grid_box)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
        layout.addWidget(self.grid_box)
        self.tiles = [PresetTile(preset) for preset in gallery_presets()]
        for tile in self.tiles:
            tile.clicked.connect(self.chosen)
        self._columns = 0
        self._reflow_timer = QTimer(self)
        self._reflow_timer.setSingleShot(True)
        self._reflow_timer.timeout.connect(self._arrange)
        self._arrange()

    def tile(self, preset_id: str) -> PresetTile:
        return next(tile for tile in self.tiles if tile.preset.id == preset_id)

    def _arrange(self) -> None:
        columns = 2 if self.width() >= round(TWO_COLUMNS_FROM * current_ui_font_scale()) else 1
        if columns == self._columns:
            return
        self._columns = columns
        for tile in self.tiles:
            self.grid.removeWidget(tile)
        for index, tile in enumerate(self.tiles):
            self.grid.addWidget(tile, index // columns, index % columns)
        for column in range(2):
            self.grid.setColumnStretch(column, 1 if column < columns else 0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._arrange()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange) and hasattr(
                self, "_reflow_timer"):
            self._columns = 0
            self._reflow_timer.start(0)
