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
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from services.walkthrough import library
from services.walkthrough.storage import captures_root
from ui_qt.utils.icons import design_icon
from ui_qt.widgets.past_walkthroughs import (
    Summary,
    load_summaries,
    walkthrough_list_relay,
)
from ui_qt.widgets.walkthrough_review import WalkthroughReview, _day, _pill, _time

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

    def __init__(self, summary: Summary, today: date):
        super().__init__()
        self.setObjectName("wlEntry")
        when = summary.when
        column = QVBoxLayout(self)
        column.setContentsMargins(12, 9, 12, 10)
        column.setSpacing(3)
        title = QLabel(summary.title)
        title.setObjectName("wlEntryDay")
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setWordWrap(True)
        title.setMaximumWidth(230)
        column.addWidget(title)
        top = QHBoxLayout()
        top.setSpacing(6)
        day = QLabel(_day(when, today) if when else summary.folder.name)
        day.setObjectName("wlEntryTime")
        top.addWidget(day)
        top.addStretch(1)
        if when:
            stamp = QLabel(_time(when))
            stamp.setObjectName("wlEntryTime")
            top.addWidget(stamp)
        column.addLayout(top)
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        label = QLabel(summary.meta)
        label.setObjectName("wlEntryMeta")
        bottom.addWidget(label)
        bottom.addStretch(1)
        if summary.pill:
            bottom.addWidget(_pill(summary.pill, summary.pill_tone))
        column.addLayout(bottom)
        self.summary = ", ".join(filter(None, (summary.title, day.text(),
                                                _time(when) if when else "",
                                                summary.meta, summary.pill)))


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
        self._load_token = 0
        self._requested_folder = None
        self._reload_review = False
        self._loading = False
        walkthrough_list_relay().loaded.connect(self._on_loaded)
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
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search walkthroughs")
        self.search_input.setAccessibleName("Search walkthroughs")
        self.search_input.textChanged.connect(self._filter)
        column.addWidget(self.search_input)
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

    def refresh(self, folder=None, *, reload_review=True):
        self._requested_folder = Path(folder) if folder is not None else None
        self._reload_review = reload_review
        self._loading = True
        self.count_label.setText("Loading walkthroughs…")
        self._load_token = load_summaries(self.root, self._today(), thumbnails=False)

    def _on_loaded(self, token, result):
        if token != self._load_token:
            return
        self._loading = False
        summaries, _total = result
        review_locked = self.review._busy or self.review._dirty
        folder = self.review.folder if review_locked else self._requested_folder
        self.list.blockSignals(True)
        self.list.clear()
        today = self._today()
        for summary in summaries:
            entry = _Entry(summary, today)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, str(summary.folder))
            item.setData(Qt.ItemDataRole.AccessibleTextRole, entry.summary)
            item.setData(Qt.ItemDataRole.UserRole + 1, summary.words)
            item.setSizeHint(entry.sizeHint())
            self.list.addItem(item)
            self.list.setItemWidget(item, entry)
            if summary.folder == folder:
                self.list.setCurrentItem(item)
        if self.list.currentRow() < 0 and self.list.count() and not review_locked:
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self._filter()
        item = self.list.currentItem()
        selected = Path(item.data(Qt.ItemDataRole.UserRole)) if item else None
        if not review_locked and (self._reload_review or selected != self.review.folder):
            self.review.show_folder(selected)

    def _filter(self, *_args):
        terms = self.search_input.text().casefold().split()
        count = 0
        for row in range(self.list.count()):
            item = self.list.item(row)
            words = item.data(Qt.ItemDataRole.UserRole + 1).casefold()
            matched = all(term in words for term in terms)
            item.setHidden(not matched)
            count += matched
        message = f"{count} saved" if count else "No matches" if terms else "None yet"
        if not self._loading:
            self.count_label.setText(message)

    def _refresh_requested(self):
        if self.review.discard_changes():
            # Agreed once: refresh must not ask again for the same edits.
            self.review._set_dirty(False)
            self.refresh(self.review.folder)

    def _selected(self, item, previous):
        if not self.review.discard_changes():
            self.list.blockSignals(True)
            self.list.setCurrentItem(previous)
            self.list.blockSignals(False)
            return
        folder = Path(item.data(Qt.ItemDataRole.UserRole)) if item else None
        if self._loading:
            self._requested_folder = folder
        self.review.show_folder(folder)

    def _review_changed(self, folder):
        self.review.show_folder(folder)
        self.refresh(folder, reload_review=False)
        self.changed.emit()

    def _review_busy(self, busy: bool):
        for widget in (self.list, self.keep, self.refresh_button):
            widget.setEnabled(not busy)
        if not busy:
            # A retention change that failed leaves the saved choice showing.
            self._show_retention()

    def _show_retention(self) -> None:
        self.keep.blockSignals(True)
        self.keep.setCurrentIndex(self.keep.findData(library.retention_days(self.root)))
        self.keep.blockSignals(False)

    def _retention(self, _index=None):
        days = self.keep.currentData()
        saved = library.retention_days(self.root)
        if days == saved:
            return
        # Applying reloads the walkthrough shown, so unsaved edits are asked
        # about whatever the choice, Forever included.
        if not self.review.discard_changes() or (days and QMessageBox.question(
                self, "Keep walkthroughs",
                f"Delete completed walkthroughs older than {self.keep.currentText().lower()} "
                "now and at each startup? Interrupted drafts and standalone screenshots are never deleted.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes):
            self._show_retention()
            return
        def apply():
            library.set_retention(self.root, days)
            library.prune(self.root)
        self.review.run(apply, done="Retention updated.")
        if not self.review._busy:
            # Refused before it started (another job holds the library).
            self._show_retention()

    def reject(self):
        if self.review._busy:
            self.review.cancel()
            return
        if self.review.discard_changes():
            self._drop_edits()
            super().reject()

    def closeEvent(self, event):
        if self.review._busy or not self.review.discard_changes():
            event.ignore()
            self.review.cancel()
        else:
            self._drop_edits()
            event.accept()

    def _drop_edits(self) -> None:
        """The dialog is kept and reopened: edits the user let go must not come back."""
        if self.review._dirty:
            self.review._discard()
