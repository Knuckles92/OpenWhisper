"""Persistent, keyboard-accessible review and recovery of local walkthroughs.

The left column lists every walkthrough, newest first, and how long they are
kept; the right is the same storyboard the Walkthrough tab shows, for the
selected one (``ui_qt/widgets/walkthrough_review.py``).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, pyqtSignal
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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from services.walkthrough import library
from services.walkthrough.document import clock
from services.walkthrough.storage import captures_root
from ui_qt.utils.icons import design_icon
from ui_qt.widgets.walkthrough_review import (
    WalkthroughReview,
    _day,
    _pill,
    _shots,
    _started,
    _time,
)

#: Retention choices; another saved value is offered as itself.
KEEP_CHOICES = ((0, "Forever"), (7, "1 week"), (30, "30 days"), (90, "90 days"), (365, "1 year"))

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
    QToolButton#wlRefresh {
        background: transparent;
        border: none;
        border-radius: 8px;
        padding: 6px;
    }
    QToolButton#wlRefresh:hover, QToolButton#wlRefresh:pressed {
        background-color: rgba(@overlay-rgb, 0.07);
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
    QComboBox#wlKeep {
        padding: 3px 10px;
        font-size: 12px;
        border-radius: 12px;
    }
    QPushButton#wlClose {
        background-color: @surface;
        color: @text;
        border: 1px solid rgba(@overlay-rgb, 0.1);
        border-radius: 14px;
        padding: 6px 14px;
        font-size: 13px;
        font-weight: 600;
    }
    QPushButton#wlClose:hover {
        background-color: @surface-hover;
    }
"""


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
        elif library.written_up(folder):
            bottom.addWidget(_pill("AI write-up"))
        column.addLayout(bottom)
        self.summary = f"{day.text()} {_time(when) if when else ''}, {meta}" + (
            ", interrupted" if draft else "")


class WalkthroughLibraryDialog(QDialog):
    #: A walkthrough was saved, written up again, recovered or deleted.
    changed = pyqtSignal()

    def __init__(self, parent=None, *, root=None, copy_files=None, recover=None,
                 acquire=None, release=None, submit=None, today=date.today):
        super().__init__(parent)
        self.setObjectName("walkthroughLibraryDialog")
        self.setWindowTitle("Walkthroughs")
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        self.resize(1100, 780)
        self.setStyleSheet(_STYLE)
        self.root = Path(root) if root is not None else captures_root()
        self._today = today
        self.review = WalkthroughReview(copy_files=copy_files, recover=recover, acquire=acquire,
                                        release=release, submit=submit)
        self.review.changed.connect(self._review_changed)
        self.review.busy_changed.connect(self._review_busy)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._sidebar())
        detail = QVBoxLayout()
        detail.setContentsMargins(28, 22, 28, 18)
        detail.setSpacing(10)
        detail.addWidget(self.review, 1)
        footer = QHBoxLayout()
        footer.addStretch(1)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("wlClose")
        self.close_button.setAutoDefault(False)
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(self.reject)
        footer.addWidget(self.close_button)
        detail.addLayout(footer)
        layout.addLayout(detail, 1)
        self.refresh()

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
        self.refresh_button = QToolButton()
        self.refresh_button.setObjectName("wlRefresh")
        self.refresh_button.setIcon(design_icon("refresh-blue.svg"))
        self.refresh_button.setIconSize(QSize(18, 18))
        self.refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.refresh_button.setAccessibleName("Refresh")
        self.refresh_button.setToolTip("Refresh")
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
        if self.review.discard_changes():
            self.refresh(folder)

    def _refresh_requested(self):
        if self.review.discard_changes():
            self.refresh(self.review.folder)

    def _selected(self, item, previous):
        if not self.review.discard_changes():
            self.list.blockSignals(True)
            self.list.setCurrentItem(previous)
            self.list.blockSignals(False)
            return
        self.review.show_folder(Path(item.data(Qt.ItemDataRole.UserRole)) if item else None)

    def _review_changed(self, folder):
        self.refresh(folder)
        self.changed.emit()

    def _review_busy(self, busy: bool):
        for widget in (self.list, self.keep, self.refresh_button):
            widget.setEnabled(not busy)

    def _retention(self, _index=None):
        days = self.keep.currentData()
        saved = library.retention_days(self.root)
        if days == saved:
            return
        if days and not (self.review.discard_changes() and QMessageBox.question(
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
        self.review.run(apply)

    def reject(self):
        if self.review._busy:
            self.review.cancel()
            return
        if self.review.discard_changes():
            super().reject()

    def closeEvent(self, event):
        if self.review._busy or not self.review.discard_changes():
            event.ignore()
            self.review.cancel()
        else:
            event.accept()
