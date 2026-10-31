"""Left navigation rail: Home, then chapters whose pages report their value.

Used by Settings instead of a tab bar. A chapter is a heading row that opens
to show its pages. Where you are reads at two levels: the top-level row you
are in (Home, or the current page's chapter) carries the selection pill, and
the current page is marked on its chapter's guide line. Chapters open and
close on their own, never because another one opened, so the row you click
stays under the pointer. Each page still shows its current assignment under
its name. Keyboard steps walk every page in order and open chapters as they
go, so a heading is never the current item.
"""
from typing import Dict, Optional, Set

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon, QTransform
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFrame,
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
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._value = ""

        row = QHBoxLayout(self)
        row.setSpacing(9)
        self.icon_label = QLabel(self)
        self.icon_label.setObjectName("modelManagerRailIcon")
        self.icon_label.setFixedSize(18, 18)
        copy = QVBoxLayout()
        copy.setSpacing(2)
        # A chapter's page lines its name up with the chapter's name.
        if nested:
            # The guide runs the full row height, under the chapter's icon,
            # so an open chapter's pages read as one connected group. The
            # selection pill sits beside it, on ``pill``, so a selected page
            # wears the same pill as Home.
            row.setContentsMargins(18, 0, 10, 0)
            row.setSpacing(6)
            self.guide = QFrame()
            self.guide.setObjectName("settingsRailGuide")
            self.guide.setFixedWidth(2)
            row.addWidget(self.guide)
            self.pill = QWidget(self)
            self.pill.setObjectName("settingsRailPagePill")
            self.pill.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
            self.pill.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
            pill_row = QHBoxLayout(self.pill)
            pill_row.setContentsMargins(10, 6, 10, 6)
            pill_row.setSpacing(0)
            pill_row.addLayout(copy, stretch=1)
            row.addWidget(self.pill, stretch=1)
            copy.setContentsMargins(0, 0, 0, 0)
            self._value_margins = 58
            self.icon_label.hide()
        else:
            self.guide = None
            self.pill = None
            self._value_margins = 47
            row.setContentsMargins(10, 8, 10, 8)
            if icon is not None and not icon.isNull():
                self.icon_label.setPixmap(icon.pixmap(16, 16))
            row.addWidget(self.icon_label, alignment=Qt.AlignmentFlag.AlignTop)
            copy.setContentsMargins(0, 0, 0, 0)
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
        if not nested:
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
        margins = self._value_margins
        available_width = item_width - margins if item_width > margins else self.VALUE_WIDTH
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
        widgets = (self, self.name_label, self.value_label, self.pill)
        for widget in filter(None, widgets):
            set_style_property(widget, "selected", selected)


class _ChapterRow(QWidget):
    """A chapter heading: its icon, its name, and whether its pages show.

    While the current page is one of its own, open or closed, its name is
    brighter, so the top level of the rail says where you are. Only the page
    itself wears the selection pill.
    """

    def __init__(self, title: str, icon: Optional[QIcon], parent=None):
        super().__init__(parent)
        self.setObjectName("settingsRailChapter")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 9, 10, 9)
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
        self.set_active(False)

    def set_open(self, is_open: bool) -> None:
        chevron = tabler_pixmap("chevron-down-slate.svg", 12)
        if not is_open:
            chevron = chevron.transformed(QTransform().rotate(-90))
        self.chevron.setPixmap(chevron)

    def set_active(self, active: bool) -> None:
        """Show whether the current page is one of this chapter's."""
        for widget in (self, self.name_label):
            set_style_property(widget, "active", active)


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
        # Rows space themselves, so an open chapter's guide line is unbroken.
        self.setSpacing(0)
        self._rows: Dict[str, _RailItem] = {}
        self._items: Dict[str, QListWidgetItem] = {}
        # title -> {"item", "row", "keys", "last"}; "last" is the page the
        # chapter reopens on.
        self._chapters: Dict[str, dict] = {}
        self._chapter_of: Dict[str, str] = {}
        # The chapter new destinations join; "" adds top-level pages.
        self._adding = ""
        self._expanded: Set[str] = set()
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
            item.setHidden(chapter not in self._expanded)

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
            if row.pill is not None:
                row.pill.layout().invalidate()
                row.pill.layout().activate()
                row.pill.updateGeometry()
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

    def active_chapter(self) -> str:
        """The current page's chapter, or "" on a top-level page."""
        return self.chapter(self.current_key())

    def is_expanded(self, title: str) -> bool:
        """Whether a chapter's pages are showing."""
        return title in self._expanded

    def set_expanded(self, title: str, expanded: bool) -> None:
        """Open or close one chapter, leaving every other one as it is."""
        chapter = self._chapters.get(title)
        if chapter is None or expanded == (title in self._expanded):
            return
        if expanded:
            self._expanded.add(title)
        else:
            self._expanded.discard(title)
        chapter["row"].set_open(expanded)
        for key in chapter["keys"]:
            self._items[key].setHidden(not expanded)

    def select(self, key: str) -> None:
        item = self._items.get(key)
        if item is not None:
            self.set_expanded(self.chapter(key), True)
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

    def _click_chapter(self, title: str) -> None:
        """Go to a chapter, or fold it away when it is already where you are.

        A closed chapter opens on its page. An open one you are elsewhere
        than takes you there, so a click on a heading never hides the pages
        you meant to reach; clicking the chapter you are in folds it, and its
        heading keeps the pill to say where you are.
        """
        if title == self.active_chapter() and self.is_expanded(title):
            self.set_expanded(title, False)
        else:
            self.select_chapter(title)

    def mousePressEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        title = item.data(_CHAPTER_ROLE) if item is not None else None
        if title:
            if event.button() == Qt.MouseButton.LeftButton:
                self._click_chapter(title)
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
        # Open the target's chapter first: a hidden row can't be current.
        self.set_expanded(self.chapter(target), True)
        return self.indexFromItem(self._items[target])

    def _on_current_changed(
        self,
        current: Optional[QListWidgetItem],
        _previous: Optional[QListWidgetItem],
    ) -> None:
        for key, row in self._rows.items():
            row.set_selected(self._items[key] is current)
        key = current.data(_KEY_ROLE) if current is not None else ""
        title = self.chapter(key) if key else ""
        for name, chapter in self._chapters.items():
            chapter["row"].set_active(name == title)
        if not key:
            return
        if title:
            self._chapters[title]["last"] = key
            self.set_expanded(title, True)
        self.destination_changed.emit(key)
