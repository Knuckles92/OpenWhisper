"""Left navigation rail: Home, then chapters whose pages report their value.

Used by Settings instead of a tab bar. A chapter is a heading row, and only
the chapter holding the current page shows its pages, so the rail stays short
enough to read at a glance. Each page still shows its current assignment
under its name. Keyboard steps walk every page in order and open chapters as
they go, so a heading is never the current item.
"""
from typing import Dict, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QTransform
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ui_qt.utils.icons import tabler_pixmap
from ui_qt.utils.restyle import set_style_property

_KEY_ROLE = Qt.ItemDataRole.UserRole
_CHAPTER_ROLE = Qt.ItemDataRole.UserRole + 1
#: Keyboard steps a Page Up or Page Down moves.
_PAGE_STEP = 5


class _RailItem(QWidget):
    """Name plus current-value row shown inside a rail item."""

    #: Fallback caption width before the list has laid out its rows.
    VALUE_WIDTH = 178

    def __init__(self, name: str, icon: Optional[QIcon], parent=None, *, nested: bool = False):
        super().__init__(parent)
        self.setObjectName("modelManagerRailItem")
        # A plain QWidget subclass does not paint a stylesheet background
        # without this, so the selected pill would be invisible.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setProperty("nested", nested)
        self._value = ""

        row = QHBoxLayout(self)
        # A chapter's page lines its name up with the chapter's name, where a
        # top-level page has its icon; both leave the value 47 px of margins.
        row.setContentsMargins(37 if nested else 10, 6 if nested else 7, 10, 6 if nested else 7)
        row.setSpacing(9)

        self.icon_label = QLabel()
        self.icon_label.setObjectName("modelManagerRailIcon")
        self.icon_label.setFixedSize(18, 18)
        if icon is not None and not icon.isNull():
            self.icon_label.setPixmap(icon.pixmap(16, 16))
        row.addWidget(self.icon_label, alignment=Qt.AlignmentFlag.AlignTop)
        self.icon_label.setVisible(not nested)

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
        # Shown with its first value, so a page without one stays one line.
        self.value_label.setVisible(False)
        row.addLayout(copy, stretch=1)

        self.set_selected(False)

    def value(self) -> str:
        """Return the full current value, before eliding."""
        return self._value

    def set_value(self, text: str) -> None:
        """Show an elided current value beneath the destination name."""
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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._value:
            self.set_value(self._value)

    def set_selected(self, selected: bool) -> None:
        """Repaint for selection.

        An item widget covers the view's own selection painting, so the state
        has to live on this widget for the stylesheet to see it.
        """
        for widget in (self, self.name_label, self.value_label):
            set_style_property(widget, "selected", selected)


class _ChapterRow(QWidget):
    """A chapter heading: its icon, its name, and whether its pages show."""

    def __init__(self, title: str, icon: Optional[QIcon], parent=None):
        super().__init__(parent)
        self.setObjectName("settingsRailChapter")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(9)
        self.icon_label = QLabel()
        self.icon_label.setObjectName("modelManagerRailIcon")
        self.icon_label.setFixedSize(18, 18)
        if icon is not None and not icon.isNull():
            self.icon_label.setPixmap(icon.pixmap(16, 16))
        row.addWidget(self.icon_label)
        self.name_label = QLabel(title)
        self.name_label.setObjectName("settingsRailChapterName")
        row.addWidget(self.name_label, stretch=1)
        self.chevron = QLabel()
        self.chevron.setObjectName("settingsRailChevron")
        self.chevron.setFixedSize(12, 12)
        row.addWidget(self.chevron)
        self.set_open(False)

    def set_open(self, is_open: bool) -> None:
        chevron = tabler_pixmap("chevron-down-slate.svg", 12)
        if not is_open:
            chevron = chevron.transformed(QTransform().rotate(-90))
        self.chevron.setPixmap(chevron)
        for widget in (self, self.name_label):
            set_style_property(widget, "open", is_open)


class NavRail(QListWidget):
    """Home and chapters of destinations, each page with its value caption."""

    destination_changed = pyqtSignal(str)

    RAIL_WIDTH = 258

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("modelManagerRail")
        self.setMinimumWidth(220)
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # Per item, the range counts a closed chapter's hidden rows, and
        # scrolling to the last page of an open one falls short.
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.setUniformItemSizes(False)
        self.setSpacing(1)
        self._rows: Dict[str, _RailItem] = {}
        self._items: Dict[str, QListWidgetItem] = {}
        # title -> {"item", "row", "keys", "last"}; "last" is the page the
        # chapter reopens on.
        self._chapters: Dict[str, dict] = {}
        self._chapter_of: Dict[str, str] = {}
        # The chapter new destinations join; "" adds top-level pages.
        self._adding = ""
        self._open = ""
        self.currentItemChanged.connect(self._on_current_changed)

    def updateGeometries(self) -> None:
        super().updateGeometries()
        # QListView places item widgets before it narrows its contents to the
        # viewport, so a shrinking rail would leave rows under the scroll bar.
        self.updateEditorGeometries()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        # Layout refreshes already place rows at the scrollbar's current
        # offset. Use that absolute geometry as the only source of position;
        # Qt's relative viewport scroll would move the same rows a second time.
        # Zero deltas preserve QListView's scroll bookkeeping without moving
        # its child widgets. This small rail can repaint instead of pixel-scroll.
        super().scrollContentsBy(0, 0)
        self.updateEditorGeometries()
        self.viewport().update()

    def add_chapter(self, title: str, icon: Optional[QIcon] = None) -> None:
        """Start a chapter; destinations added next are its pages."""
        item = QListWidgetItem(self)
        # Enabled for the hover cursor, never selectable: a click opens the
        # chapter's page instead.
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setData(_CHAPTER_ROLE, title)
        row = _ChapterRow(title, icon)
        item.setSizeHint(QSize(0, row.sizeHint().height()))
        self.setItemWidget(item, row)
        self._chapters[title] = {"item": item, "row": row, "keys": [], "last": ""}
        self._adding = title

    def add_destination(
        self, key: str, name: str, icon: Optional[QIcon] = None
    ) -> None:
        item = QListWidgetItem(self)
        item.setData(_KEY_ROLE, key)
        chapter = self._adding
        row = _RailItem(name, icon, nested=bool(chapter))
        item.setSizeHint(QSize(0, row.sizeHint().height()))
        self.setItemWidget(item, row)
        self._rows[key] = row
        self._items[key] = item
        if chapter:
            self._chapters[chapter]["keys"].append(key)
            self._chapter_of[key] = chapter
            item.setHidden(chapter != self._open)

    def set_value(self, key: str, text: str) -> None:
        row = self._rows.get(key)
        if row is None:
            return
        shown = not row.value_label.isHidden()
        row.set_value(text)
        if shown == row.value_label.isHidden():
            # The row's nested layouts hear of the caption on the next event
            # loop pass; re-measure them now, or a closed chapter's pages
            # keep one line's height when it opens.
            row.layout().invalidate()
            row.layout().activate()
            self._items[key].setSizeHint(QSize(0, row.sizeHint().height()))

    def value(self, key: str) -> str:
        """Return the full value shown under a destination's name."""
        row = self._rows.get(key)
        return row.value() if row is not None else ""

    def name(self, key: str) -> str:
        """Return a destination's display name."""
        row = self._rows.get(key)
        return row.name_label.text() if row is not None else ""

    def chapter(self, key: str) -> str:
        """The chapter a destination belongs to, or "" for a top-level page."""
        return self._chapter_of.get(key, "")

    def chapters(self) -> tuple:
        return tuple(self._chapters)

    def chapter_keys(self, title: str) -> tuple:
        chapter = self._chapters.get(title)
        return tuple(chapter["keys"]) if chapter else ()

    def open_chapter(self) -> str:
        """The chapter whose pages are showing, or ""."""
        return self._open

    def select(self, key: str) -> None:
        item = self._items.get(key)
        if item is not None:
            self._show_chapter(self.chapter(key))
            self.setCurrentItem(item)

    def select_chapter(self, title: str) -> None:
        """Open a chapter on the page it was last showing, else its first."""
        chapter = self._chapters.get(title)
        if chapter and chapter["keys"]:
            self.select(chapter["last"] or chapter["keys"][0])

    def current_key(self) -> str:
        item = self.currentItem()
        if item is None:
            return ""
        return item.data(_KEY_ROLE) or ""

    def keys(self) -> tuple:
        return tuple(self._items)

    def _show_chapter(self, title: str) -> None:
        if title == self._open:
            return
        self._open = title
        for name, chapter in self._chapters.items():
            showing = name == title
            chapter["row"].set_open(showing)
            for key in chapter["keys"]:
                self._items[key].setHidden(not showing)

    def mousePressEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        title = item.data(_CHAPTER_ROLE) if item is not None else None
        if title:
            if event.button() == Qt.MouseButton.LeftButton:
                self.select_chapter(title)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        # Dragging would select whatever row it crosses, chapter headings too.
        if event.buttons():
            event.accept()
            return
        super().mouseMoveEvent(event)

    def moveCursor(self, cursorAction, modifiers):
        keys = self.keys()
        current = self.current_key()
        if not keys or current not in keys:
            return super().moveCursor(cursorAction, modifiers)
        index = keys.index(current)
        action = QAbstractItemView.CursorAction
        steps = {
            action.MoveDown: 1, action.MoveNext: 1,
            action.MoveUp: -1, action.MovePrevious: -1,
            action.MovePageDown: _PAGE_STEP, action.MovePageUp: -_PAGE_STEP,
            action.MoveHome: -len(keys), action.MoveEnd: len(keys),
        }
        if cursorAction not in steps:
            return super().moveCursor(cursorAction, modifiers)
        target = keys[max(0, min(len(keys) - 1, index + steps[cursorAction]))]
        # Show the target's chapter first: a hidden row can't be current.
        self._show_chapter(self.chapter(target))
        return self.indexFromItem(self._items[target])

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
        if not key:
            return
        title = self.chapter(key)
        if title:
            self._chapters[title]["last"] = key
        self._show_chapter(title)
        self.destination_changed.emit(key)
