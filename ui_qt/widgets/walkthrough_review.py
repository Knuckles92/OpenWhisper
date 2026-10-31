"""One walkthrough as a storyboard: each screenshot beside the words that led to it.

The Walkthrough tab shows the newest walkthrough here, or the one picked in
the side panel; Saved Walkthroughs shows whichever its list selects. A row is
a section: its screenshot, then its words, edited in place, with a switch to
leave the screenshot out. Dragging a row's grip (or Alt+Up/Down in its words)
moves the section, words and screenshot together. A walkthrough an agent wrote
up shows the write-up the same way, cut at its screenshots, with a switch to
the words as spoken. Save rebuilds the documents off the UI thread; copying
and opening wait until edits are saved.
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from pathlib import Path

from PyQt6.QtCore import QMimeData, QPoint, QPointF, QRectF, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import (
    QDesktopServices,
    QDrag,
    QImageReader,
    QPainter,
    QPainterPath,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from services.settings import (
    WalkthroughCopyFormat,
    resolve_walkthrough_write_up_instructions,
    settings_manager,
)
from services.walkthrough import library
from services.walkthrough.document import clock
from services.walkthrough.write_up import DEFAULT_INSTRUCTIONS
from ui_qt.utils.file_reveal import reveal_in_file_manager
from ui_qt.utils.icons import design_icon
from ui_qt.utils.palette import token_color
from ui_qt.widgets.context_menu import context_menu
from ui_qt.widgets.reflow import Reflow
from ui_qt.widgets.segmented_bar import SegmentedBar
from ui_qt.widgets.settings_switch import SettingsSwitch
from ui_qt.widgets.walkthrough_ready import FileChip

#: The largest a screenshot shows beside its words, in logical pixels; it
#: grows toward this with the room its row has.
PREVIEW_SIZE = QSize(360, 225)
#: The narrowest a screenshot gets, so a narrow window keeps it beside its words.
PREVIEW_MIN_WIDTH = 180
#: How a row's width splits between its screenshot and its words.
PREVIEW_STRETCH, WORDS_STRETCH = 3, 4
SPOKEN_HINT = ("Fix any words, drag a row to move a screenshot and its words, or switch a "
               "screenshot off to leave it out. Original screenshots are kept.")
WRITE_UP_HINT = ("Fix the write-up's words or drag a row to move a screenshot. "
                 "Saving rebuilds its PDF and Word document.")
#: The two versions of a written-up walkthrough, in the switch's order.
VERSIONS = (("AI write-up", "What the agent wrote: what's copied when it finishes"),
            ("As spoken", "Your words as you said them, a section per screenshot"))
VERSION_BAR_WIDTH = 250
_ROW_MIME = "application/x-openwhisper-walkthrough-row"
_COPIED = {
    WalkthroughCopyFormat.PDF: "PDF copied.",
    WalkthroughCopyFormat.AGENT: "Markdown copied.",
    WalkthroughCopyFormat.WORD: "Word document copied.",
}

_STYLE = """
    QWidget#walkthroughReview {
        background: transparent;
    }
    QLabel#wlTitle {
        color: @text-heading;
        background: transparent;
        font-size: 20px;
        font-weight: 700;
    }
    QLabel#wlMeta {
        color: @text-secondary;
        background: transparent;
        font-size: 13px;
    }
    QLabel#wlNotice {
        color: @text-muted;
        background: transparent;
        font-size: 12px;
    }
    QLabel#wlPill {
        color: @accent-soft;
        background-color: rgba(@accent-rgb, 0.12);
        border-radius: 8px;
        padding: 1px 8px;
        font-size: 11px;
        font-weight: 600;
    }
    QLabel#wlPill[tone="warning"] {
        color: @warning-text;
        background-color: rgba(@warning-rgb, 0.16);
    }
    QLabel#wlPill[tone="plain"] {
        color: @text-secondary;
        background-color: rgba(@overlay-rgb, 0.07);
    }
    QToolButton#wlIconButton {
        background: transparent;
        border: none;
        border-radius: 8px;
        padding: 6px;
    }
    QToolButton#wlIconButton:hover, QToolButton#wlIconButton:pressed {
        background-color: rgba(@overlay-rgb, 0.07);
    }
    QToolButton#wlIconButton::menu-indicator {
        image: none;
        width: 0px;
    }
    QPushButton#wlPrimary {
        background-color: @accent;
        color: @on-accent;
        border: 1px solid @accent;
        border-radius: 14px;
        padding: 6px 16px;
        font-size: 13px;
        font-weight: 600;
    }
    QPushButton#wlPrimary:hover {
        background-color: @accent-hover;
        border-color: @accent-hover;
    }
    QPushButton#wlPrimary:pressed {
        background-color: @accent-pressed;
        border-color: @accent-pressed;
    }
    QPushButton#wlPrimary:disabled {
        border-color: transparent;
        background-color: rgba(@accent-rgb, 0.35);
        color: rgba(@on-accent-rgb, 0.7);
    }
    QPushButton#wlSecondary {
        background-color: @surface;
        color: @text;
        border: 1px solid rgba(@overlay-rgb, 0.1);
        border-radius: 14px;
        padding: 6px 14px;
        font-size: 13px;
        font-weight: 600;
    }
    QPushButton#wlSecondary:hover {
        background-color: @surface-hover;
    }
    QPushButton#wlSecondary:disabled, QPushButton#wlQuiet:disabled {
        color: @text-muted;
    }
    QPushButton#wlQuiet {
        background: transparent;
        color: @text-secondary;
        border: 1px solid transparent;
        border-radius: 14px;
        padding: 6px 12px;
        font-size: 13px;
        font-weight: 600;
    }
    QPushButton#wlQuiet:hover {
        background-color: rgba(@overlay-rgb, 0.06);
        color: @text;
    }
    QScrollArea#wlScroll, QScrollArea#wlScroll > QWidget, QWidget#wlBody {
        background: transparent;
        border: none;
    }
    QFrame#wlSection {
        background-color: @surface;
        border: 1px solid rgba(@overlay-rgb, 0.06);
        border-radius: 14px;
    }
    QFrame#wlDropMarker {
        background-color: @accent;
        border: none;
        border-radius: 1px;
    }
    QLabel#wlStamp, QLabel#wlInclude, QWidget#wlPreview, QLabel#wlNoShot,
    QWidget#wlActionGroup {
        background: transparent;
    }
    QLabel#wlStep {
        color: @on-accent;
        background-color: @accent;
        border-radius: 11px;
        font-size: 11px;
        font-weight: 700;
    }
    QLabel#wlStamp, QLabel#wlInclude {
        color: @text-secondary;
        font-size: 12px;
    }
    QLabel#wlNoShot {
        color: @text-muted;
        font-size: 12px;
        border: 1px dashed rgba(@overlay-rgb, 0.14);
        border-radius: 8px;
    }
    QLabel#wlNote {
        color: @purple-text;
        background-color: rgba(@purple-rgb, 0.1);
        border: 1px solid rgba(@purple-rgb, 0.28);
        border-radius: 8px;
        padding: 6px 8px;
        font-size: 12px;
    }
    QTextEdit#wlEditor {
        color: @text;
        background-color: rgba(@overlay-rgb, 0.035);
        border: 1px solid transparent;
        border-radius: 10px;
        padding: 6px 8px;
        font-size: 14px;
        selection-background-color: rgba(@accent-rgb, 0.35);
    }
    QTextEdit#wlEditor:hover {
        border: 1px solid rgba(@overlay-rgb, 0.08);
    }
    QTextEdit#wlEditor:focus {
        background-color: @bg;
        border: 1px solid rgba(@accent-rgb, 0.55);
    }
    QFrame#wlBanner {
        background-color: rgba(@warning-rgb, 0.1);
        border: 1px solid rgba(@warning-rgb, 0.28);
        border-radius: 14px;
    }
    QLabel#wlBannerTitle, QLabel#wlEmptyTitle {
        color: @text-heading;
        background: transparent;
        font-size: 15px;
        font-weight: 700;
    }
    QLabel#wlBannerBody, QLabel#wlEmptyBody {
        color: @text-secondary;
        background: transparent;
        font-size: 13px;
    }
    QLabel#wlEmptyTitle {
        background: transparent;
        font-size: 18px;
    }
    QFrame#wlSaveBar {
        background-color: @surface;
        border: 1px solid rgba(@accent-rgb, 0.35);
        border-radius: 16px;
    }
    QLabel#wlSaveText {
        color: @text;
        background: transparent;
        font-size: 13px;
    }
"""


def _started(folder: Path, document=None) -> datetime | None:
    if document is not None:
        return document.started_at
    try:
        return datetime.fromisoformat(library.read_json(folder / library.DRAFT_NAME)["started_at"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        return datetime.strptime(folder.name[:19], "%Y-%m-%d_%H-%M-%S")
    except ValueError:
        return None


def _day(when: datetime, today: date) -> str:
    days = (today - when.date()).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if when.year == today.year:
        return f"{when:%a, %b} {when.day}"
    return f"{when:%b} {when.day}, {when.year}"


def _time(when: datetime) -> str:
    return when.strftime("%I:%M %p").lstrip("0")


def _shots(count: int) -> str:
    return f"{count} screenshot{'' if count == 1 else 's'}"


def _preview(path: Path, ratio: float) -> QPixmap | None:
    """The screenshot decoded at the largest size it shows."""
    reader = QImageReader(str(path))
    size = reader.size()
    if not size.isValid():
        return None
    shown = size.scaled(PREVIEW_SIZE, Qt.AspectRatioMode.KeepAspectRatio) if (
        size.width() > PREVIEW_SIZE.width() or size.height() > PREVIEW_SIZE.height()) else size
    reader.setScaledSize(shown * ratio)
    image = reader.read()
    if image.isNull():
        return None
    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def _pill(text: str, tone: str = "") -> QLabel:
    pill = QLabel(text)
    pill.setObjectName("wlPill")
    if tone:
        pill.setProperty("tone", tone)
    return pill


class _Editor(QTextEdit):
    """A row's words: plain text that grows with them instead of scrolling."""

    #: Alt+Up (-1) or Alt+Down (+1): move this row.
    nudged = pyqtSignal(int)

    def __init__(self, text: str, number: int):
        super().__init__()
        self.setObjectName("wlEditor")
        self.setAcceptRichText(False)
        self.setTabChangesFocus(True)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setPlaceholderText("Nothing was said here")
        self.setPlainText(text)
        self.setAccessibleName(f"Section {number} transcript")
        self.document().documentLayout().documentSizeChanged.connect(self._fit)

    def _fit(self, *_):
        margins = self.contentsMargins()
        height = round(self.document().size().height()) + margins.top() + margins.bottom() + 2
        self.setFixedHeight(max(46, height))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def keyPressEvent(self, event):
        if (event.modifiers() == Qt.KeyboardModifier.AltModifier
                and event.key() in (Qt.Key.Key_Up, Qt.Key.Key_Down)):
            self.nudged.emit(-1 if event.key() == Qt.Key.Key_Up else 1)
            event.accept()
            return
        super().keyPressEvent(event)


class _Grip(QWidget):
    """Six dots that drag their row to another place in the storyboard."""

    def __init__(self, row: _Row):
        super().__init__(row)
        self.row = row
        self._press: QPoint | None = None
        self.setFixedWidth(16)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setToolTip("Drag to move this screenshot and its words (or Alt+Up/Down in the words)")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(token_color("text-muted"))
        x, y = self.width() / 2, self.height() / 2
        for dx in (-3.0, 3.0):
            for dy in (-6.0, 0.0, 6.0):
                painter.drawEllipse(QPointF(x + dx, y + dy), 1.6, 1.6)
        painter.end()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = event.position().toPoint()

    def mouseMoveEvent(self, event):
        if self._press is None:
            return
        if (event.position().toPoint() - self._press).manhattanLength() < QApplication.startDragDistance():
            return
        self._press = None
        data = QMimeData()
        data.setData(_ROW_MIME, b"")
        drag = QDrag(self)
        drag.setMimeData(data)
        picture = self.row.grab()
        drag.setPixmap(picture.scaledToWidth(min(picture.width(), 420),
                                             Qt.TransformationMode.SmoothTransformation))
        drag.exec(Qt.DropAction.MoveAction)

    def mouseReleaseEvent(self, event):
        self._press = None


class _Preview(QWidget):
    """A row's screenshot, as large as its row has room for; clicking it opens the full picture.

    ``picture`` is decoded once at the largest size it shows and drawn
    smaller as the row narrows, faded while the screenshot is left out.
    """

    def __init__(self, image: Path, picture: QPixmap, included: bool):
        super().__init__()
        self.image = image
        self._picture = picture
        self._included = included
        self.setObjectName("wlPreview")
        self.setAccessibleName(f"Preview of {image.name}")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Open the full screenshot")
        size = picture.deviceIndependentSize()
        self._ratio = size.height() / size.width()
        # Never drawn larger than decoded: a small region stays its own size.
        self._widest = round(size.width())
        self.setMinimumWidth(min(PREVIEW_MIN_WIDTH, self._widest))
        self.setMaximumWidth(self._widest)
        policy = QSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    @property
    def included(self) -> bool:
        return self._included

    def set_included(self, on: bool) -> None:
        """Fade the screenshot while it's left out, so the next save can bring it back."""
        self._included = on
        self.update()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return round(min(width, self._widest) * self._ratio)

    def sizeHint(self) -> QSize:
        # The row shares out the rest by stretch, so every row's screenshot is as wide.
        return self.minimumSizeHint()

    def minimumSizeHint(self) -> QSize:
        width = self.minimumWidth()
        return QSize(width, self.heightForWidth(width))

    def paintEvent(self, event):
        width = min(self.width(), self._widest)
        shown = QRectF(0, 0, width, self.heightForWidth(width))
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing
                               | QPainter.RenderHint.SmoothPixmapTransform)
        corners = QPainterPath()
        corners.addRoundedRect(shown, 8, 8)
        painter.setClipPath(corners)
        if not self._included:
            painter.setOpacity(0.28)
        painter.drawPixmap(shown, self._picture, QRectF(self._picture.rect()))
        painter.end()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.image)))


class _Row(QFrame):
    """One section: its screenshot beside the words that led to it.

    ``index`` is the saved section (or write-up part) this row shows, so a
    reordered storyboard saves in its new order.
    """

    def __init__(self, index: int, number: int, text: str, image: Path | None, *,
                 included: bool, stamp: str, can_omit: bool, line: str = "",
                 notes: tuple[str, ...] = ()):
        super().__init__()
        self.setObjectName("wlSection")
        self.index = index
        self.line = line
        self.preview: _Preview | None = None
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 12, 14, 12)
        row.setSpacing(10)
        self.grip = _Grip(self)
        row.addWidget(self.grip)
        row.addWidget(self._picture(image, included), PREVIEW_STRETCH, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(8)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.step = QLabel(str(number))
        self.step.setObjectName("wlStep")
        self.step.setFixedSize(22, 22)
        self.step.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.step)
        if stamp:
            label = QLabel(stamp)
            label.setObjectName("wlStamp")
            top.addWidget(label)
        top.addStretch(1)
        self.check = SettingsSwitch()
        self.check.setChecked(included)
        self.check.setAccessibleName(f"Include {image.name}" if image else "No screenshot")
        if image and can_omit:
            include = QLabel("Include screenshot")
            include.setObjectName("wlInclude")
            top.addWidget(include)
            top.addWidget(self.check)
        else:
            self.check.setEnabled(False)
            self.check.hide()
        column.addLayout(top)
        self.editor = _Editor(text, number)
        column.addWidget(self.editor)
        for note in notes:
            # Held for the agent: shown so Retry's input is visible, never edited.
            label = QLabel(f"Note for the agent · not in the document\n{note}")
            label.setObjectName("wlNote")
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setWordWrap(True)
            column.addWidget(label)
        column.addStretch(1)
        row.addLayout(column, WORDS_STRETCH)

    def _picture(self, image: Path | None, included: bool) -> QWidget:
        shown = _preview(image, self.devicePixelRatioF()) if image else None
        if shown is not None:
            self.preview = _Preview(image, shown, included)
            return self.preview
        # As wide as the screenshots in the other rows, so the words line up.
        empty = QLabel("No screenshot" if image is None else f"{image.name} is missing")
        empty.setObjectName("wlNoShot")
        empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty.setWordWrap(True)
        empty.setMinimumWidth(PREVIEW_MIN_WIDTH)
        empty.setMaximumWidth(PREVIEW_SIZE.width())
        empty.setFixedHeight(64)
        return empty

    def set_number(self, number: int) -> None:
        self.step.setText(str(number))
        self.editor.setAccessibleName(f"Section {number} transcript")

    def show_included(self, on: bool) -> None:
        if self.preview is not None:
            self.preview.set_included(on)


class _Board(QWidget):
    """The rows in order; a row dropped on it moves to where it lands."""

    #: A row moved from one place to another (row positions, not saved indexes).
    moved = pyqtSignal(int, int)

    def __init__(self):
        super().__init__()
        self.setObjectName("wlBody")
        self.setAcceptDrops(True)
        self.rows: list[_Row] = []
        self.column = QVBoxLayout(self)
        self.column.setContentsMargins(0, 0, 6, 12)
        self.column.setSpacing(12)
        self._marker = QFrame(self)
        self._marker.setObjectName("wlDropMarker")
        self._marker.setFixedHeight(3)
        self._marker.hide()

    def _dragged(self, event) -> _Row | None:
        source = event.source()
        if (event.mimeData().hasFormat(_ROW_MIME) and isinstance(source, _Grip)
                and source.row in self.rows):
            return source.row
        return None

    def _slot(self, y: float) -> int:
        """Where a row dropped at ``y`` goes: the number of rows above it."""
        return sum(1 for row in self.rows if row.geometry().center().y() < y)

    def dragEnterEvent(self, event):
        if self._dragged(event) is not None:
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if self._dragged(event) is None:
            return
        event.acceptProposedAction()
        slot = self._slot(event.position().y())
        gap = self.column.spacing() / 2
        if slot < len(self.rows):
            y = self.rows[slot].geometry().top() - gap
        else:
            y = self.rows[-1].geometry().bottom() + gap
        self._marker.setGeometry(0, round(y) - 1, self.width() - 6, 3)
        self._marker.show()
        self._marker.raise_()

    def dragLeaveEvent(self, event):
        self._marker.hide()

    def dropEvent(self, event):
        self._marker.hide()
        row = self._dragged(event)
        if row is None:
            return
        event.acceptProposedAction()
        start = self.rows.index(row)
        slot = self._slot(event.position().y())
        end = slot - 1 if slot > start else slot
        if end != start:
            self.moved.emit(start, end)


class WalkthroughReview(QWidget):
    """Review, fix, reorder, copy and open one walkthrough."""

    _finished = pyqtSignal(object, str)
    #: Saved, written up again, recovered or deleted: the folder to show now,
    #: or None when it is gone.
    changed = pyqtSignal(object)
    #: Work started or ended off the UI thread; hosts hold their own controls meanwhile.
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent=None, *, copy_files=None, recover=None,
                 acquire=None, release=None, submit=None):
        super().__init__(parent)
        self.setObjectName("walkthroughReview")
        self.setStyleSheet(_STYLE)
        self.bind(copy_files=copy_files, recover=recover, acquire=acquire,
                  release=release, submit=submit)
        self._busy = False
        self._dirty = False
        self._folder: Path | None = None
        self._written = False
        #: Whether the words as spoken show, rather than the AI write-up.
        self._spoken = True
        self._cancel = threading.Event()
        self._enabled: list[bool] = []
        self.board: _Board | None = None
        self._finished.connect(self._done)
        # One button, moved into whichever draft's banner shows; parked on the
        # widget in between so a replaced body never takes it along.
        self.recover_button = self._button("Recover draft", "wlPrimary", self._recover_draft)
        self.recover_button.hide()
        column = QVBoxLayout(self)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)
        column.addLayout(self._header())
        column.addWidget(self._actions())
        status = QHBoxLayout()
        self.notice = QLabel(SPOKEN_HINT)
        self.notice.setObjectName("wlNotice")
        self.notice.setWordWrap(True)
        status.addWidget(self.notice, 1)
        self.cancel_button = self._button("Cancel", "wlQuiet", self.cancel)
        self.cancel_button.hide()
        status.addWidget(self.cancel_button)
        column.addLayout(status)
        column.addSpacing(6)
        self.scroll = QScrollArea()
        self.scroll.setObjectName("wlScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        column.addWidget(self.scroll, 1)
        column.addWidget(self._save_bar())
        self.actions = [self.save_button, self.save_redo_button, self.discard_button,
                        self.pdf_button, self.markdown_button, self.copy_word_button,
                        self.open_button, self.open_word_button, self.folder_button,
                        self.retry_button, self.style_button, self.prompt_button,
                        self.delete_button, self.recover_button,
                        self.version_bar]
        self.show_folder(None)

    def bind(self, *, copy_files=None, recover=None, acquire=None, release=None, submit=None):
        """What copying, recovery and the app's one-operation-at-a-time gate go through."""
        self._copy_files = copy_files
        self._recover = recover
        self._acquire = acquire or (lambda: None)
        self._release = release or (lambda: None)
        self._submit = submit

    # ---- building ----

    def _header(self) -> QHBoxLayout:
        head = QHBoxLayout()
        head.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = QLabel("")
        self.title.setObjectName("wlTitle")
        self.title.setWordWrap(True)
        titles.addWidget(self.title)
        meta = QHBoxLayout()
        meta.setSpacing(8)
        self.meta = QLabel("")
        self.meta.setObjectName("wlMeta")
        meta.addWidget(self.meta)
        self.kind = _pill("")
        meta.addWidget(self.kind)
        meta.addStretch(1)
        titles.addLayout(meta)
        head.addLayout(titles, 1)
        self.version_bar = SegmentedBar([(title, "") for title, _tip in VERSIONS], compact=True)
        for button, (_title, tip) in zip(self.version_bar.buttons, VERSIONS, strict=True):
            button.setToolTip(tip)
        # The bar asks for no width of its own beside the title; give it one row's worth.
        self.version_bar.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        self.version_bar.setFixedWidth(VERSION_BAR_WIDTH)
        self.version_bar.setAccessibleName("Which version of the walkthrough shows")
        self.version_bar.activated.connect(self._on_version_chosen)
        head.addWidget(self.version_bar, 0, Qt.AlignmentFlag.AlignTop)
        return head

    def _actions(self) -> Reflow:
        """Copy and the file on the left; Open and More beside them, or below once narrow."""
        self.pdf_button = self._button(
            "Copy PDF", "wlPrimary", lambda: self._copy(WalkthroughCopyFormat.PDF))
        self.pdf_button.setToolTip("The PDF file, to paste or attach anywhere, agents included")
        self.markdown_button = self._button(
            "Copy Markdown", "wlSecondary", lambda: self._copy(WalkthroughCopyFormat.AGENT))
        self.markdown_button.setIcon(design_icon("copy-gray.svg"))
        self.markdown_button.setToolTip("The words as Markdown text, with a file link to each "
                                        "screenshot, for pasting into a terminal agent")
        self.copy_word_button = self._button(
            "Copy Word", "wlSecondary", lambda: self._copy(WalkthroughCopyFormat.WORD))
        self.copy_word_button.setToolTip("A .docx you can edit")
        self.chip = FileChip(self)
        self.chip.clicked.connect(self._open)
        self.open_button = self._button("Open PDF", "wlSecondary", self._open)
        self.open_button.setIcon(design_icon("external-link-gray.svg"))
        self.more_button = self._icon_button("dots-gray.svg", "More")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = context_menu(self)
        self.open_word_button = self._menu_action("Open Word document", self._open_word)
        self.folder_button = self._menu_action("Show in folder", self._reveal)
        self.menu.addSeparator()
        self.retry_button = self._menu_action("Redo AI write-up", self._retry)
        # Filled as it opens, so styles saved anywhere since show.
        self.style_menu = context_menu(self.menu)
        self.style_menu.aboutToShow.connect(self._fill_style_menu)
        self.style_button = self.menu.addMenu(self.style_menu)
        self.prompt_button = self._menu_action("Redo with a new prompt…", self._retry_with_prompt)
        self.menu.addSeparator()
        self.delete_button = self._menu_action("Delete…", self._delete)
        self.more_button.setMenu(self.menu)
        self.copy_group = self._group(
            self.pdf_button, self.markdown_button, self.copy_word_button, self.chip)
        self.action_row = Reflow(self.copy_group, self._group(self.open_button, self.more_button))
        return self.action_row

    @staticmethod
    def _group(*widgets: QWidget) -> QWidget:
        group = QWidget()
        group.setObjectName("wlActionGroup")
        row = QHBoxLayout(group)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        for widget in widgets:
            row.addWidget(widget)
        row.addStretch(1)
        return group

    def _save_bar(self) -> QFrame:
        self.save_bar = QFrame()
        self.save_bar.setObjectName("wlSaveBar")
        row = QHBoxLayout(self.save_bar)
        row.setContentsMargins(16, 8, 8, 8)
        self.save_text = QLabel("")
        self.save_text.setObjectName("wlSaveText")
        self.save_text.setWordWrap(True)
        row.addWidget(self.save_text, 1)
        self.discard_button = self._button("Discard", "wlQuiet", self._discard)
        row.addWidget(self.discard_button)
        self.save_button = self._button("Save changes", "wlPrimary", self._save)
        row.addWidget(self.save_button)
        self.save_redo_button = self._button(
            "Save and redo write-up", "wlPrimary", lambda: self._save(redo=True))
        row.addWidget(self.save_redo_button)
        self.save_bar.hide()
        return self.save_bar

    def minimumSizeHint(self) -> QSize:
        # The row of actions ignores its own width so Open can drop below Copy,
        # but the copy buttons themselves never squash.
        return super().minimumSizeHint().expandedTo(
            QSize(self.copy_group.minimumSizeHint().width(), 0))

    def _button(self, text: str, name: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(name)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        # Enter belongs to the editors, never to whichever button is first.
        button.setAutoDefault(False)
        button.clicked.connect(slot)
        return button

    def _icon_button(self, icon: str, name: str) -> QToolButton:
        button = QToolButton()
        button.setObjectName("wlIconButton")
        button.setIcon(design_icon(icon))
        button.setIconSize(QSize(18, 18))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAccessibleName(name)
        button.setToolTip(name)
        return button

    def _menu_action(self, text: str, slot):
        action = self.menu.addAction(text)
        action.triggered.connect(slot)
        return action

    # ---- the walkthrough ----

    @property
    def folder(self) -> Path | None:
        return self._folder

    @property
    def rows(self) -> list[_Row]:
        return list(self.board.rows) if self.board is not None else []

    @property
    def _editors(self) -> list[_Editor]:
        return [row.editor for row in self.rows]

    @property
    def _checks(self) -> list[SettingsSwitch]:
        return [row.check for row in self.rows]

    def discard_changes(self) -> bool:
        """Whether there is nothing unsaved, or the user agrees to lose it."""
        return not self._dirty or QMessageBox.question(
            self, "Unsaved review", "Discard your unsaved review changes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def show_folder(self, folder, *, spoken: bool | None = None) -> None:
        """Show the walkthrough in ``folder`` (None: none), dropping unsaved edits.

        A written-up walkthrough opens on its write-up, which is what was
        copied, unless ``spoken`` asks for the words as spoken.
        """
        self._set_dirty(False)
        self._folder = Path(folder) if folder else None
        self._written = bool(self._folder and library.written_up(self._folder))
        self._spoken = not self._written or bool(spoken)
        self.recover_button.hide()
        self.recover_button.setParent(self)
        board = _Board()
        board.moved.connect(self._move)
        ready = False
        draft = False
        walkthrough = None
        if self._folder:
            draft = (self._folder / library.DRAFT_NAME).exists()
            if draft:
                board.column.addWidget(self._draft_banner())
            try:
                walkthrough = library.load_document(self._folder)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                if not draft:
                    board.column.addWidget(self._empty("This walkthrough can't be opened", str(exc)))
            if walkthrough is not None:
                try:
                    rows = (self._spoken_rows(walkthrough) if self._spoken
                            else self._write_up_rows())
                except (OSError, ValueError) as exc:
                    board.column.addWidget(self._empty("This walkthrough can't be opened", str(exc)))
                else:
                    ready = True
                    for row in rows:
                        self._add_row(board, row)
        else:
            board.column.addWidget(self._empty(
                "No walkthroughs yet",
                "Start one from the Walkthrough tab or the tray. Each one you finish is kept "
                "here, ready to review and copy again."))
        board.column.addStretch(1)
        self.board = board
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(board)
        self._show_header(walkthrough, draft)
        self._show_actions(ready, draft)

    def _spoken_rows(self, walkthrough) -> list[_Row]:
        rows = []
        for index, section in enumerate(walkthrough.sections):
            image = section.shot or section.omitted
            stamp = ""
            if section.kept_at_s is not None:
                stamp = (f"Screenshot at {clock(section.kept_at_s)}" if image
                         else f"Until {clock(section.kept_at_s)}")
            rows.append(_Row(index, index + 1, section.text, image,
                             included=section.shot is not None, stamp=stamp, can_omit=True,
                             notes=section.notes))
        return rows

    def _write_up_rows(self) -> list[_Row]:
        text = library.local_file(self._folder, "write-up.md").read_text(encoding="utf-8")
        rows = []
        for index, (words, line) in enumerate(library.write_up_parts(text)):
            image = library.write_up_shot(self._folder, line) if line else None
            rows.append(_Row(index, index + 1, words, image, included=True,
                             stamp="", can_omit=False, line=line))
        return rows

    def _add_row(self, board: _Board, row: _Row) -> None:
        board.rows.append(row)
        board.column.addWidget(row)
        row.editor.textChanged.connect(self._changed)
        row.editor.nudged.connect(lambda step, r=row: self._nudge(r, step))
        row.check.toggled.connect(row.show_included)
        row.check.toggled.connect(self._changed)

    def _show_header(self, walkthrough, draft: bool) -> None:
        folder = self._folder
        when = _started(folder, walkthrough) if folder else None
        has = folder is not None
        self.title.setText(f"{when:%A, %B} {when.day} · {_time(when)}" if when else (
            folder.name if has else ""))
        if walkthrough is not None:
            count = sum(1 for section in walkthrough.sections if section.shot)
            self.meta.setText(f"{_shots(count)} · {clock(walkthrough.duration_s)}")
        else:
            self.meta.setText("Not finished yet" if draft else "")
        # A written-up walkthrough says which version shows with its switch.
        self.kind.setText("Interrupted" if draft else "As spoken")
        self.kind.setProperty("tone", "warning" if draft else "plain")
        self.kind.style().unpolish(self.kind)
        self.kind.style().polish(self.kind)
        self.kind.setVisible(has and (draft or not self._written))
        self.version_bar.setVisible(self._written and walkthrough is not None)
        self.version_bar.setCurrentIndex(1 if self._spoken else 0)
        redo = "Redo" if self._written else "Write up"
        self.retry_button.setText("Redo AI write-up" if self._written else "Write up with AI")
        self.style_menu.setTitle(f"{redo} as")
        self.prompt_button.setText(f"{redo} with a new prompt…")
        self.title.setVisible(has)
        if not self._busy:
            self.notice.setText(SPOKEN_HINT if self._spoken else WRITE_UP_HINT)

    def _show_actions(self, ready: bool, draft: bool) -> None:
        folder = self._folder
        for button in (self.save_button, self.discard_button, self.pdf_button,
                       self.markdown_button, self.retry_button, self.style_button,
                       self.prompt_button, self.open_button):
            button.setEnabled(ready)
        # A draft has no document yet: nothing to copy, open or edit.
        for widget in (self.pdf_button, self.markdown_button, self.copy_word_button,
                       self.chip, self.open_button, self.notice):
            widget.setVisible(ready)
        # Walkthroughs from before Word documents have none until Review saves them.
        word = ready and self._result_docx().is_file()
        self.copy_word_button.setEnabled(word)
        self.open_word_button.setEnabled(word)
        if ready:
            self.chip.set_path(self._result_pdf())
        self.recover_button.setEnabled(bool(draft and self._recover and folder
                                            and (folder / library.AUDIO_NAME).is_file()))
        self.folder_button.setEnabled(folder is not None)
        self.delete_button.setEnabled(folder is not None)
        self.more_button.setVisible(folder is not None)

    def _result_pdf(self) -> Path:
        return self._folder / ("walkthrough.pdf" if self._spoken else "write-up.pdf")

    def _result_docx(self) -> Path:
        return self._result_pdf().with_suffix(".docx")

    def _draft_banner(self) -> QFrame:
        banner = QFrame()
        banner.setObjectName("wlBanner")
        row = QHBoxLayout(banner)
        row.setContentsMargins(16, 14, 14, 14)
        row.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(3)
        title = QLabel("This walkthrough was interrupted")
        title.setObjectName("wlBannerTitle")
        text.addWidget(title)
        has_audio = (self._folder / library.AUDIO_NAME).is_file()
        body = QLabel("Its audio and screenshots were saved. Recover it to transcribe them into a document."
                      if has_audio else
                      "Its screenshots are kept. The audio hasn't been recovered yet; "
                      "restarting OpenWhisper finishes that.")
        body.setObjectName("wlBannerBody")
        body.setWordWrap(True)
        text.addWidget(body)
        row.addLayout(text, 1)
        row.addWidget(self.recover_button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.recover_button.show()
        return banner

    def _empty(self, heading: str, text: str) -> QWidget:
        widget = QWidget()
        column = QVBoxLayout(widget)
        column.setContentsMargins(0, 60, 0, 0)
        column.setSpacing(6)
        title = QLabel(heading)
        title.setObjectName("wlEmptyTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(title)
        body = QLabel(text)
        body.setObjectName("wlEmptyBody")
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(body)
        column.addStretch(1)
        return widget

    # ---- editing ----

    def _changed(self, *_):
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        # Saving the words as spoken replaces the write-up that came from them.
        redo = dirty and self._spoken and self._written
        if redo:
            self.save_text.setText("Saving rebuilds the PDF. The AI write-up came from the "
                                   "old words, so it's redone or dropped.")
        else:
            self.save_text.setText("You have unsaved edits. Saving rebuilds the PDF.")
        self.save_button.setText("Save without write-up" if redo else "Save changes")
        self.save_button.setObjectName("wlSecondary" if redo else "wlPrimary")
        self.save_button.style().unpolish(self.save_button)
        self.save_button.style().polish(self.save_button)
        self.save_redo_button.setVisible(redo)
        self.save_bar.setVisible(dirty)

    def _discard(self):
        self.show_folder(self._folder, spoken=self._spoken)

    def _move(self, start: int, end: int) -> None:
        board = self.board
        # Rows sit together after any banner, from the first row's place on.
        base = board.column.indexOf(board.rows[0])
        row = board.rows.pop(start)
        board.rows.insert(end, row)
        board.column.removeWidget(row)
        board.column.insertWidget(base + end, row)
        for number, each in enumerate(board.rows, 1):
            each.set_number(number)
        self._changed()

    def _nudge(self, row: _Row, step: int) -> None:
        rows = self.board.rows
        start = rows.index(row)
        end = start + step
        if 0 <= end < len(rows):
            self._move(start, end)
            row.editor.setFocus()
            self.scroll.ensureWidgetVisible(row)

    def _on_version_chosen(self, index: int) -> None:
        spoken = index == 1
        if spoken == self._spoken:
            return
        if not self.discard_changes():
            self.version_bar.setCurrentIndex(1 if self._spoken else 0)
            return
        self.show_folder(self._folder, spoken=spoken)

    # ---- work off the UI thread ----

    def run(self, operation) -> None:
        """Run ``operation`` on a worker behind the app's gate, then show what it left."""
        if self._busy:
            return
        error = self._acquire()
        if error:
            self.notice.setText(error)
            return
        self._busy = True
        self.busy_changed.emit(True)
        self._enabled = [button.isEnabled() for button in self.actions]
        self._cancel = threading.Event()
        self.scroll.setEnabled(False)
        for button in self.actions:
            button.setEnabled(False)
        self.cancel_button.show()
        self.notice.setText("Working… Your saved documents stay as they are if this fails.")
        def work():
            result, error = None, ""
            try:
                if self._cancel.is_set():
                    raise RuntimeError("canceled")
                result = operation()
            except Exception as exc:
                error = str(exc)
            self._finished.emit(result, error)
        try:
            if self._submit is not None:
                self._submit(work)
            else:
                threading.Thread(target=work, name="walkthrough-review", daemon=True).start()
        except Exception as exc:
            self._done(None, str(exc))

    def _done(self, result, error):
        self._busy = False
        self._release()
        self.scroll.setEnabled(True)
        self.cancel_button.hide()
        self.busy_changed.emit(False)
        if error:
            # Preserve unsaved editor contents after a failed regeneration.
            for button, enabled in zip(self.actions, self._enabled, strict=True):
                button.setEnabled(enabled)
            self.notice.setText(f"Couldn't finish: {error}")
            return
        folder = getattr(result, "folder", self._folder)
        shown = Path(folder) if folder is not None and Path(folder).is_dir() else None
        self._set_dirty(False)
        # The host shows what to see next: this folder, or another once it's gone.
        self.changed.emit(shown)
        self.notice.setText("Saved. Copy it again whenever you're ready.")

    def cancel(self) -> None:
        """Stop the work in progress as soon as it safely can."""
        if self._busy:
            self._cancel.set()
            self.notice.setText("Stopping safely…")

    # ---- actions ----

    def _save(self, redo: bool = False):
        if not self._folder:
            return
        folder = self._folder
        if not self._spoken:
            parts = [(row.editor.toPlainText(), row.line) for row in self.rows]
            self.run(lambda: library.save_write_up(folder, library.join_write_up(parts)))
            return
        texts = [row.editor.toPlainText() for row in self.rows]
        keep = [row.check.isChecked() for row in self.rows]
        order = [row.index for row in self.rows]
        def save():
            result = library.save_review(folder, texts, keep, order)
            if redo:
                return library.retry_write_up(folder, cancel_event=self._cancel)
            return result
        self.run(save)

    def _copy(self, copy_format):
        if self._busy or not self._folder or not self._copy_files:
            return
        if self._dirty:
            self.notice.setText("Save your changes before copying.")
            return
        try:
            result = library.load_result(self._folder, spoken=self._spoken)
            files, text = result.clipboard(copy_format)
            ok = self._copy_files(files, text=text)
            if ok:
                self.notice.setText(_COPIED[copy_format])
            else:
                self.notice.setText("Copying failed. Drag the file instead, or show it in its folder.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.notice.setText(str(exc))

    def _retry(self):
        self._redo(None)

    def _redo(self, instructions: str | None):
        """Write it up again: with ``instructions``, or Settings' prompt when None."""
        if self._folder and not self._dirty:
            folder = self._folder
            self.run(lambda: library.retry_write_up(
                folder, instructions=instructions, cancel_event=self._cancel))
        elif self._dirty:
            self.notice.setText("Save your changes before writing it up.")

    def _fill_style_menu(self) -> None:
        from services.walkthrough.styles import load_styles

        self.style_menu.clear()
        for style in load_styles(settings_manager.load_all_settings()):
            action = self.style_menu.addAction(style.name)
            action.setToolTip(style.instructions)
            action.triggered.connect(
                lambda _checked=False, words=style.instructions: self._redo(words))
        self.style_menu.setToolTipsVisible(True)

    def _retry_with_prompt(self):
        from ui_qt.dialogs.write_up_prompt_dialog import WriteUpPromptDialog

        if self._dirty:
            self.notice.setText("Save your changes before writing it up.")
            return
        if not self._folder:
            return
        prompt = (resolve_walkthrough_write_up_instructions(settings_manager.load_all_settings())
                  or DEFAULT_INSTRUCTIONS)
        dialog = WriteUpPromptDialog(prompt, self, written=self._written)
        if dialog.exec():
            self._redo(dialog.prompt_text())

    def _recover_draft(self):
        if self._folder and self._recover:
            self._recover(str(self._folder))
            self.notice.setText("Recovering. It appears here once it's transcribed.")

    def _open(self):
        if self._folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._result_pdf())))

    def _open_word(self):
        if self._folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._result_docx())))

    def _reveal(self):
        if self._folder:
            reveal_in_file_manager(str(self._result_pdf()))

    def _delete(self):
        if self._folder and QMessageBox.question(
            self, "Delete walkthrough", "Permanently delete this walkthrough, its audio and screenshots?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            folder = self._folder
            self.run(lambda: library.delete_entry(folder))
