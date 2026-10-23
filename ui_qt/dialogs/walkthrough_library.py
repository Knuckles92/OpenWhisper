"""Persistent, keyboard-accessible review and recovery of local walkthroughs.

The left column lists every walkthrough, newest first; the right shows the
selected one as the document reads: each section's words, then the
screenshot it led to. Copy for agent, Copy PDF and Open PDF stay in reach; the rest
waits behind ``⋯``. Edits only offer Save once there is something to save.
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from pathlib import Path

from PyQt6.QtCore import QRectF, QSize, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QImageReader, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from services.walkthrough import library
from services.walkthrough.document import clock
from services.walkthrough.storage import captures_root
from ui_qt.utils.file_reveal import reveal_in_file_manager
from ui_qt.utils.icons import design_icon
from ui_qt.widgets.context_menu import context_menu
from ui_qt.widgets.settings_switch import SettingsSwitch

#: The largest a screenshot shows in review, in logical pixels.
PREVIEW_SIZE = QSize(600, 340)
#: Retention choices; another saved value is offered as itself.
KEEP_CHOICES = ((0, "Forever"), (7, "1 week"), (30, "30 days"), (90, "90 days"), (365, "1 year"))
HINT = "Fix any words, switch off screenshots to leave out, then save. Original screenshots are kept."

_STYLE = """
    QDialog#walkthroughLibraryDialog {
        background-color: @bg;
    }
    QFrame#wlSidebar {
        background-color: rgba(@surface-rgb, 0.45);
        border: none;
        border-right: 1px solid rgba(@overlay-rgb, 0.06);
    }
    QLabel#wlSidebarTitle {
        color: @text-heading;
        background: transparent;
        font-size: 20px;
        font-weight: 700;
    }
    QLabel#wlSidebarCount, QLabel#wlKeepLabel {
        color: @text-secondary;
        background: transparent;
        font-size: 12px;
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
    QListWidget#wlList {
        background: transparent;
        border: none;
        outline: none;
        padding: 0px;
    }
    QListWidget#wlList::item {
        background: transparent;
        border: 1px solid transparent;
        border-radius: 12px;
        margin: 1px 0px;
    }
    QListWidget#wlList::item:hover {
        background-color: rgba(@overlay-rgb, 0.04);
    }
    QListWidget#wlList::item:selected {
        background-color: @surface;
        border: 1px solid rgba(@overlay-rgb, 0.1);
    }
    QWidget#wlEntry, QLabel#wlEntryDay, QLabel#wlEntryTime, QLabel#wlEntryMeta {
        background: transparent;
    }
    QLabel#wlEntryDay {
        color: @text;
        font-size: 13px;
        font-weight: 600;
    }
    QLabel#wlEntryTime, QLabel#wlEntryMeta {
        color: @text-secondary;
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
    QComboBox#wlKeep {
        padding: 3px 10px;
        font-size: 12px;
        border-radius: 12px;
    }
    QLabel#wlTitle {
        color: @text-heading;
        background: transparent;
        font-size: 22px;
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
    QLabel#wlStamp, QLabel#wlShotName, QLabel#wlInclude, QLabel#wlPreview {
        background: transparent;
    }
    QLabel#wlStep {
        color: @on-accent;
        background-color: @accent;
        border-radius: 11px;
        font-size: 11px;
        font-weight: 700;
    }
    QLabel#wlStamp, QLabel#wlShotName {
        color: @text-secondary;
        font-size: 12px;
    }
    QLabel#wlShotName {
        font-weight: 600;
    }
    QLabel#wlInclude {
        color: @text-secondary;
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


def _preview(path: Path, ratio: float, faded: bool) -> QPixmap | None:
    """The screenshot decoded at the size it shows, with rounded corners."""
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
    pixmap = QPixmap(image.size())
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(pixmap.rect()), 8 * ratio, 8 * ratio)
    painter.setClipPath(clip)
    if faded:
        painter.setOpacity(0.28)
    painter.drawImage(0, 0, image)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


class _Editor(QTextEdit):
    """A section's words: plain text that grows with them instead of scrolling."""

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


class _Entry(QWidget):
    """One walkthrough in the list: when, how long, and what it holds."""

    def __init__(self, folder: Path, today: date):
        super().__init__()
        self.setObjectName("wlEntry")
        draft = (folder / library.DRAFT_NAME).exists()
        try:
            document = library.load_document(folder)
        except (OSError, ValueError, KeyError, TypeError):
            document = None
        when = _started(folder, document)
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 9, 12, 10)
        column.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(6)
        day = QLabel(_day(when, today) if when else folder.name)
        day.setObjectName("wlEntryDay")
        top.addWidget(day)
        top.addStretch(1)
        if when:
            stamp = QLabel(_time(when))
            stamp.setObjectName("wlEntryTime")
            top.addWidget(stamp)
        column.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        if document is not None:
            count = sum(1 for section in document.sections if section.shot)
            meta = f"{_shots(count)} · {clock(document.duration_s)}"
        else:
            meta = "Not finished" if draft else "Can't be opened"
        label = QLabel(meta)
        label.setObjectName("wlEntryMeta")
        bottom.addWidget(label)
        bottom.addStretch(1)
        if draft:
            bottom.addWidget(_pill("Interrupted", "warning"))
        elif (folder / "write-up.pdf").is_file():
            bottom.addWidget(_pill("AI write-up"))
        column.addLayout(bottom)
        self.summary = f"{day.text()} {_time(when) if when else ''}, {meta}" + (
            ", interrupted" if draft else "")


def _pill(text: str, tone: str = "") -> QLabel:
    pill = QLabel(text)
    pill.setObjectName("wlPill")
    if tone:
        pill.setProperty("tone", tone)
    return pill


class WalkthroughLibraryDialog(QDialog):
    _finished = pyqtSignal(object, str)

    def __init__(self, parent=None, *, root=None, copy_files=None, recover=None,
                 acquire=None, release=None, submit=None, today=date.today):
        super().__init__(parent)
        self.setObjectName("walkthroughLibraryDialog")
        self.setWindowTitle("Walkthroughs")
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        self.resize(1060, 760)
        self.setStyleSheet(_STYLE)
        self.root = Path(root) if root is not None else captures_root()
        self._copy_files = copy_files
        self._recover = recover
        self._acquire = acquire or (lambda: None)
        self._release = release or (lambda: None)
        self._submit = submit
        self._today = today
        self._busy = False
        self._dirty = False
        self._folder = None
        self._cancel = threading.Event()
        self._editors = []
        self._checks = []
        self._enabled = []
        self._finished.connect(self._done)
        # One button, moved into whichever draft's banner shows; parked on the
        # dialog in between so a replaced body never takes it along.
        self.recover_button = self._button("Recover draft", "wlPrimary", self._recover_draft)
        self.recover_button.hide()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._sidebar())
        layout.addLayout(self._detail(), 1)
        self.actions = [self.save_button, self.discard_button, self.copy_button, self.open_button,
                        self.pdf_button, self.folder_button, self.retry_button,
                        self.delete_button, self.recover_button]
        self.refresh()

    # ---- building ----

    def _sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("wlSidebar")
        sidebar.setFixedWidth(290)
        column = QVBoxLayout(sidebar)
        column.setContentsMargins(14, 18, 14, 14)
        column.setSpacing(10)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(0)
        title = QLabel("Walkthroughs")
        title.setObjectName("wlSidebarTitle")
        titles.addWidget(title)
        self.count_label = QLabel("")
        self.count_label.setObjectName("wlSidebarCount")
        titles.addWidget(self.count_label)
        head.addLayout(titles, 1)
        self.refresh_button = self._icon_button("refresh-blue.svg", "Refresh")
        self.refresh_button.clicked.connect(self._refresh_requested)
        head.addWidget(self.refresh_button, 0, Qt.AlignmentFlag.AlignTop)
        column.addLayout(head)
        self.list = QListWidget()
        self.list.setObjectName("wlList")
        self.list.setAccessibleName("Saved walkthroughs and interrupted drafts")
        self.list.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.list.currentItemChanged.connect(self._selected)
        column.addWidget(self.list, 1)
        keep = QHBoxLayout()
        keep.setSpacing(8)
        label = QLabel("Keep walkthroughs")
        label.setObjectName("wlKeepLabel")
        keep.addWidget(label)
        keep.addStretch(1)
        self.keep = QComboBox()
        self.keep.setObjectName("wlKeep")
        self.keep.setAccessibleName("How long to keep completed walkthroughs")
        self.keep.setToolTip("Completed walkthroughs older than this are deleted at startup. "
                             "Interrupted drafts are never deleted.")
        days = library.retention_days(self.root)
        for value, text in KEEP_CHOICES:
            self.keep.addItem(text, value)
        if self.keep.findData(days) < 0:
            self.keep.addItem(f"{days} days", days)
        self.keep.setCurrentIndex(self.keep.findData(days))
        self.keep.currentIndexChanged.connect(self._retention)
        keep.addWidget(self.keep)
        column.addLayout(keep)
        return sidebar

    def _detail(self) -> QVBoxLayout:
        column = QVBoxLayout()
        column.setContentsMargins(28, 22, 28, 18)
        column.setSpacing(6)
        head = QHBoxLayout()
        head.setSpacing(8)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.title = QLabel("")
        self.title.setObjectName("wlTitle")
        # Wraps instead of holding the dialog wide enough for three buttons beside it.
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
        self.copy_button = self._button("Copy for agent", "wlPrimary", lambda: self._copy(False))
        self.copy_button.setToolTip("The document's Markdown, with each screenshot's path")
        self.pdf_button = self._button("Copy PDF", "wlSecondary", lambda: self._copy(True))
        self.pdf_button.setIcon(design_icon("copy-gray.svg"))
        self.pdf_button.setToolTip("The PDF file, to paste or attach anywhere")
        self.open_button = self._button("Open PDF", "wlSecondary", self._open)
        self.open_button.setIcon(design_icon("external-link-gray.svg"))
        self.more_button = self._icon_button("dots-gray.svg", "More")
        self.more_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.menu = context_menu(self)
        self.folder_button = self._menu_action("Show in folder", self._reveal)
        self.menu.addSeparator()
        self.retry_button = self._menu_action("Redo AI write-up", self._retry)
        self.menu.addSeparator()
        self.delete_button = self._menu_action("Delete…", self._delete)
        self.more_button.setMenu(self.menu)
        for widget in (self.copy_button, self.pdf_button, self.open_button, self.more_button):
            head.addWidget(widget, 0, Qt.AlignmentFlag.AlignTop)
        column.addLayout(head)
        status = QHBoxLayout()
        self.notice = QLabel(HINT)
        self.notice.setObjectName("wlNotice")
        self.notice.setWordWrap(True)
        status.addWidget(self.notice, 1)
        self.cancel_button = self._button("Cancel", "wlQuiet", self._cancel_operation)
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
        return column

    def _save_bar(self) -> QFrame:
        self.save_bar = QFrame()
        self.save_bar.setObjectName("wlSaveBar")
        row = QHBoxLayout(self.save_bar)
        row.setContentsMargins(16, 8, 8, 8)
        text = QLabel("You have unsaved edits. Saving rebuilds the PDF.")
        text.setObjectName("wlSaveText")
        row.addWidget(text, 1)
        self.discard_button = self._button("Discard", "wlQuiet", self._discard)
        row.addWidget(self.discard_button)
        self.save_button = self._button("Save changes", "wlPrimary", self._save)
        row.addWidget(self.save_button)
        self.save_bar.hide()
        return self.save_bar

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

    # ---- the list ----

    def refresh(self, folder=None):
        self.list.blockSignals(True)
        self.list.clear()
        today = self._today()
        folders = library.entries(self.root)
        for path in folders:
            entry = _Entry(path, today)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            item.setData(Qt.ItemDataRole.AccessibleTextRole, entry.summary)
            item.setSizeHint(entry.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, entry)
            if folder is not None and path == Path(folder):
                self.list.setCurrentItem(item)
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self.count_label.setText(f"{len(folders)} saved" if folders else "None yet")
        self._selected(self.list.currentItem(), None)

    def select_folder(self, folder):
        if self._discard_changes():
            self._set_dirty(False)
            self.refresh(folder)

    def _refresh_requested(self):
        if self._discard_changes():
            self._set_dirty(False)
            self.refresh(self._folder)

    def _discard_changes(self):
        return not self._dirty or QMessageBox.question(
            self, "Unsaved review", "Discard your unsaved review changes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    # ---- the selected walkthrough ----

    def _selected(self, item, previous):
        if not self._discard_changes():
            self.list.blockSignals(True)
            self.list.setCurrentItem(previous)
            self.list.blockSignals(False)
            return
        self._set_dirty(False)
        self._folder = Path(item.data(Qt.ItemDataRole.UserRole)) if item else None
        self._editors, self._checks = [], []
        self.recover_button.hide()
        self.recover_button.setParent(self)
        body = QWidget()
        body.setObjectName("wlBody")
        column = QVBoxLayout(body)
        column.setContentsMargins(0, 0, 6, 12)
        column.setSpacing(12)
        ready = False
        draft = False
        if self._folder:
            draft = (self._folder / library.DRAFT_NAME).exists()
            if draft:
                column.addWidget(self._draft_banner())
            try:
                walkthrough = library.load_document(self._folder)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                walkthrough = None
                if not draft:
                    column.addWidget(self._empty("This walkthrough can't be opened", str(exc)))
            if walkthrough is not None:
                ready = True
                for number, section in enumerate(walkthrough.sections, 1):
                    column.addWidget(self._section(number, section))
            self._show_header(walkthrough, draft)
        else:
            column.addWidget(self._empty(
                "No walkthroughs yet",
                "Start one from Quick Record or the tray. Each one you finish is kept here, "
                "ready to review and copy again."))
            self._show_header(None, False)
        column.addStretch()
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(body)
        for button in (self.save_button, self.discard_button, self.copy_button, self.pdf_button,
                       self.retry_button, self.open_button):
            button.setEnabled(ready)
        self.recover_button.setEnabled(bool(draft and self._recover and self._folder
                                           and (self._folder / library.AUDIO_NAME).is_file()))
        self.folder_button.setEnabled(self._folder is not None)
        self.delete_button.setEnabled(self._folder is not None)
        self.more_button.setEnabled(self._folder is not None)

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
        written = has and (folder / "write-up.pdf").is_file()
        self.kind.setText("Interrupted" if draft else "AI write-up" if written else "As spoken")
        self.kind.setProperty("tone", "warning" if draft else "" if written else "plain")
        self.kind.style().unpolish(self.kind)
        self.kind.style().polish(self.kind)
        self.kind.setVisible(has)
        self.retry_button.setText("Redo AI write-up" if written else "Write up with AI")
        self.title.setVisible(has)
        self.more_button.setVisible(has)
        # A draft has no document yet: nothing to copy, open or edit.
        for widget in (self.copy_button, self.pdf_button, self.open_button, self.notice):
            widget.setVisible(walkthrough is not None)
        if not self._busy:
            self.notice.setText(HINT)

    def _section(self, number: int, section) -> QFrame:
        card = QFrame()
        card.setObjectName("wlSection")
        column = QVBoxLayout(card)
        column.setContentsMargins(16, 14, 16, 16)
        column.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(8)
        step = QLabel(str(number))
        step.setObjectName("wlStep")
        step.setFixedSize(22, 22)
        step.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(step)
        if section.kept_at_s is not None:
            stamp = QLabel(f"Screenshot at {clock(section.kept_at_s)}" if section.shot
                           else f"Until {clock(section.kept_at_s)}")
            stamp.setObjectName("wlStamp")
            top.addWidget(stamp)
        top.addStretch(1)
        check = SettingsSwitch()
        check.setChecked(section.shot is not None)
        check.setEnabled(section.shot is not None)
        check.setAccessibleName(f"Include {section.shot.name}" if section.shot else "No screenshot")
        check.toggled.connect(self._changed)
        self._checks.append(check)
        if section.shot:
            include = QLabel("Include screenshot")
            include.setObjectName("wlInclude")
            top.addWidget(include)
            top.addWidget(check)
        else:
            check.hide()
        column.addLayout(top)
        editor = _Editor(section.text, number)
        editor.textChanged.connect(self._changed)
        column.addWidget(editor)
        self._editors.append(editor)
        if section.shot:
            ratio = self.devicePixelRatioF()
            shown, faded = _preview(section.shot, ratio, False), _preview(section.shot, ratio, True)
            preview = QLabel()
            preview.setObjectName("wlPreview")
            preview.setAccessibleName(f"Preview of {section.shot.name}")
            preview.setAlignment(Qt.AlignmentFlag.AlignLeft)
            if shown is not None:
                preview.setPixmap(shown)
                check.toggled.connect(lambda on: preview.setPixmap(shown if on else faded))
            else:
                preview.setText(f"{section.shot.name} is missing")
                preview.setObjectName("wlStamp")
            column.addWidget(preview)
        return card

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
        column.setContentsMargins(0, 80, 0, 0)
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

    def _changed(self, *_):
        self._set_dirty(True)

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self.save_bar.setVisible(dirty)

    def _discard(self):
        self._set_dirty(False)
        self.refresh(self._folder)

    # ---- work off the UI thread ----

    def _run(self, operation):
        if self._busy:
            return
        error = self._acquire()
        if error:
            self.notice.setText(error)
            return
        self._busy = True
        self._enabled = [button.isEnabled() for button in self.actions]
        self.refresh_button.setEnabled(False)
        self._cancel = threading.Event()
        self.list.setEnabled(False)
        self.scroll.setEnabled(False)
        for button in self.actions + [self.keep]:
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
                threading.Thread(target=work, name="walkthrough-library", daemon=True).start()
        except Exception as exc:
            self._done(None, str(exc))

    def _done(self, result, error):
        self._busy = False
        self._release()
        self.list.setEnabled(True)
        self.scroll.setEnabled(True)
        self.keep.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.cancel_button.hide()
        folder = getattr(result, "folder", self._folder)
        if error:
            # Preserve unsaved editor contents after a failed regeneration.
            for button, enabled in zip(self.actions, self._enabled, strict=True):
                button.setEnabled(enabled)
        else:
            self._set_dirty(False)
            self.refresh(folder)
        self.notice.setText(f"Couldn't finish: {error}" if error else "Saved. Copy it again whenever you're ready.")

    def _cancel_operation(self):
        if self._busy:
            self._cancel.set()
            self.notice.setText("Stopping safely…")

    # ---- actions ----

    def _save(self):
        if self._folder:
            folder = self._folder
            texts = [editor.toPlainText() for editor in self._editors]
            keep = [check.isChecked() for check in self._checks]
            self._run(lambda: library.save_review(folder, texts, keep))

    def _copy(self, pdf_only):
        if self._busy or not self._folder or not self._copy_files:
            return
        if self._dirty:
            self.notice.setText("Save your changes before copying.")
            return
        try:
            result = library.load_result(self._folder)
            ok = self._copy_files([result.pdf] if pdf_only else [],
                                  text="" if pdf_only else result.clipboard_text)
            if ok:
                self.notice.setText("PDF copied." if pdf_only else "Copied. Paste it into your agent.")
            else:
                self.notice.setText("Copying failed. Show it in its folder and drag the PDF instead.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.notice.setText(str(exc))

    def _retry(self):
        if self._folder and not self._dirty:
            folder = self._folder
            self._run(lambda: library.retry_write_up(folder, cancel_event=self._cancel))
        elif self._dirty:
            self.notice.setText("Save your changes before writing it up.")

    def _recover_draft(self):
        if self._folder and self._recover:
            self._recover(str(self._folder))
            self.notice.setText("Recovering. It appears here once it's transcribed.")

    def _open(self):
        if self._folder:
            try:
                result = library.load_result(self._folder)
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.pdf)))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.notice.setText(str(exc))

    def _reveal(self):
        if self._folder:
            reveal_in_file_manager(str(self._folder / "walkthrough.pdf"))

    def _delete(self):
        if self._folder and QMessageBox.question(
            self, "Delete walkthrough", "Permanently delete this walkthrough, its audio and screenshots?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            folder = self._folder
            self._run(lambda: library.delete_entry(folder))

    def _retention(self, _index=None):
        days = self.keep.currentData()
        saved = library.retention_days(self.root)
        if days == saved:
            return
        if days and not (self._discard_changes() and QMessageBox.question(
                self, "Keep walkthroughs",
                f"Delete completed walkthroughs older than {self.keep.currentText().lower()} "
                "now and at each startup? Interrupted drafts and standalone screenshots are never deleted.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes):
            self.keep.blockSignals(True)
            self.keep.setCurrentIndex(self.keep.findData(saved))
            self.keep.blockSignals(False)
            return
        def apply():
            library.set_retention(self.root, days)
            library.prune(self.root)
        self._run(apply)

    def reject(self):
        if self._busy:
            self._cancel_operation()
            return
        if self._discard_changes():
            super().reject()

    def closeEvent(self, event):
        if self._busy or not self._discard_changes():
            event.ignore()
            if self._busy:
                self._cancel.set()
        else:
            event.accept()
