"""Persistent, keyboard-accessible review and recovery of local walkthroughs."""
from __future__ import annotations

import threading
from pathlib import Path

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QImage, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.walkthrough import library
from services.walkthrough.storage import captures_root
from ui_qt.utils.file_reveal import reveal_in_file_manager


class WalkthroughLibraryDialog(QDialog):
    _finished = pyqtSignal(object, str)

    def __init__(self, parent=None, *, root=None, copy_files=None, recover=None,
                 acquire=None, release=None, submit=None):
        super().__init__(parent)
        self.setWindowTitle("Saved Walkthroughs")
        self.resize(1000, 720)
        self.root = Path(root) if root is not None else captures_root()
        self._copy_files = copy_files
        self._recover = recover
        self._acquire = acquire or (lambda: None)
        self._release = release or (lambda: None)
        self._submit = submit
        self._busy = False
        self._dirty = False
        self._folder = None
        self._cancel = threading.Event()
        self._editors = []
        self._checks = []
        self._finished.connect(self._done)
        layout = QVBoxLayout(self)
        self.notice = QLabel("Review your words and uncheck screenshots to leave them out. Original PNGs stay.")
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        split = QSplitter()
        self.list = QListWidget()
        self.list.setAccessibleName("Saved walkthroughs and interrupted drafts")
        self.list.currentItemChanged.connect(self._selected)
        split.addWidget(self.list)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        split.addWidget(self.scroll)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        actions = QHBoxLayout()
        self.actions = []
        def action(label, slot):
            button = QPushButton(label)
            button.clicked.connect(slot)
            actions.addWidget(button)
            self.actions.append(button)
            return button
        self.save_button = action("Save review", self._save)
        self.copy_button = action("Copy for agent", lambda: self._copy(False))
        self.pdf_button = action("Copy PDF", lambda: self._copy(True))
        self.retry_button = action("Retry AI write-up", self._retry)
        layout.addLayout(actions)
        actions = QHBoxLayout()
        self.recover_button = action("Recover draft", self._recover_draft)
        self.open_button = action("Open PDF", self._open)
        self.folder_button = action("Show in folder", self._reveal)
        self.delete_button = action("Delete…", self._delete)
        layout.addLayout(actions)
        retention = QHBoxLayout()
        retention.addWidget(QLabel("Keep completed walkthroughs for"))
        self.days = QSpinBox()
        self.days.setRange(0, 3650)
        self.days.setSpecialValueText("Forever")
        self.days.setSuffix(" days")
        self.days.setAccessibleName("Walkthrough retention in days; zero keeps forever")
        self.days.setValue(library.retention_days(self.root))
        retention.addWidget(self.days)
        self.retention_button = QPushButton("Apply retention…")
        self.retention_button.clicked.connect(self._retention)
        retention.addWidget(self.retention_button)
        retention.addStretch()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self._refresh_requested)
        retention.addWidget(self.refresh_button)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        retention.addWidget(self.close_button)
        layout.addLayout(retention)
        self.refresh()

    def refresh(self, folder=None):
        self.list.blockSignals(True)
        self.list.clear()
        for path in library.entries(self.root):
            draft = (path / library.DRAFT_NAME).exists()
            item = QListWidgetItem(path.name + (" — interrupted draft" if draft else ""))
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.list.addItem(item)
            if folder is not None and path == Path(folder):
                self.list.setCurrentItem(item)
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self._selected(self.list.currentItem(), None)

    def select_folder(self, folder):
        if self._discard_changes():
            self._dirty = False
            self.refresh(folder)

    def _refresh_requested(self):
        if self._discard_changes():
            self._dirty = False
            self.refresh(self._folder)

    def _discard_changes(self):
        return not self._dirty or QMessageBox.question(
            self, "Unsaved review", "Discard your unsaved review changes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def _selected(self, item, previous):
        if not self._discard_changes():
            self.list.blockSignals(True)
            self.list.setCurrentItem(previous)
            self.list.blockSignals(False)
            return
        self._dirty = False
        self._folder = Path(item.data(Qt.ItemDataRole.UserRole)) if item else None
        self._editors, self._checks = [], []
        body = QWidget()
        column = QVBoxLayout(body)
        ready = False
        draft = False
        if self._folder:
            draft = (self._folder / library.DRAFT_NAME).exists()
            try:
                walkthrough = library.load_document(self._folder)
                ready = True
                for index, section in enumerate(walkthrough.sections, 1):
                    column.addWidget(QLabel(f"Section {index}"))
                    editor = QTextEdit()
                    editor.setAcceptRichText(False)
                    editor.setPlainText(section.text)
                    editor.setAccessibleName(f"Section {index} transcript")
                    editor.setMinimumHeight(110)
                    editor.textChanged.connect(self._changed)
                    column.addWidget(editor)
                    self._editors.append(editor)
                    check = QCheckBox(f"Include {section.shot.name}" if section.shot else "No screenshot")
                    check.setChecked(section.shot is not None)
                    check.setEnabled(section.shot is not None)
                    check.toggled.connect(self._changed)
                    column.addWidget(check)
                    self._checks.append(check)
                    if section.shot:
                        preview = QLabel()
                        preview.setPixmap(QPixmap.fromImage(QImage(str(section.shot)).scaled(
                            480, 180, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)))
                        preview.setAccessibleName(f"Preview of {section.shot.name}")
                        column.addWidget(preview)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                column.addWidget(QLabel("This walkthrough needs recovery." if draft else f"Cannot open: {exc}"))
            if draft:
                has_audio = (self._folder / library.AUDIO_NAME).is_file()
                message = ("Recovered audio and screenshots are ready. Recover draft to transcribe them."
                           if has_audio else "Screenshots are kept. Audio recovery has not completed; try restarting OpenWhisper.")
                label = QLabel(message)
                label.setWordWrap(True)
                column.addWidget(label)
        else:
            column.addWidget(QLabel("No saved walkthroughs yet."))
        column.addStretch()
        old = self.scroll.takeWidget()
        if old is not None:
            old.deleteLater()
        self.scroll.setWidget(body)
        for button in (self.save_button, self.copy_button, self.pdf_button,
                       self.retry_button, self.open_button):
            button.setEnabled(ready)
        self.recover_button.setEnabled(bool(draft and self._recover and self._folder
                                           and (self._folder / library.AUDIO_NAME).is_file()))
        self.folder_button.setEnabled(self._folder is not None)
        self.delete_button.setEnabled(self._folder is not None)

    def _changed(self, *_):
        self._dirty = True

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
        for button in self.actions + [self.retention_button]:
            button.setEnabled(False)
        self.close_button.setText("Cancel operation")
        self.notice.setText("Working… Your saved source documents stay available if this fails.")
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
        self.retention_button.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.close_button.setText("Close")
        if not error:
            self._dirty = False
        folder = getattr(result, "folder", self._folder)
        if error:
            # Preserve unsaved editor contents after a failed regeneration.
            for button, enabled in zip(self.actions, self._enabled, strict=True):
                button.setEnabled(enabled)
        else:
            self.refresh(folder)
        self.notice.setText(f"Couldn't finish: {error}" if error else "Saved. Review or copy the document when you're ready.")

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
            self.notice.setText("Save your review before copying it.")
            return
        try:
            result = library.load_result(self._folder)
            ok = self._copy_files([result.pdf] if pdf_only else [],
                                  text="" if pdf_only else result.clipboard_text)
            self.notice.setText("Copied." if ok else "Copying failed — open the folder and drag the PDF instead.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.notice.setText(str(exc))

    def _retry(self):
        if self._folder and not self._dirty:
            folder = self._folder
            self._run(lambda: library.retry_write_up(folder, cancel_event=self._cancel))
        elif self._dirty:
            self.notice.setText("Save your review before writing it up.")

    def _recover_draft(self):
        if self._folder and self._recover:
            self._recover(str(self._folder))
            self.notice.setText("Recovery requested. Refresh this window after processing finishes.")

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

    def _retention(self):
        if not self._discard_changes():
            return
        days = self.days.value()
        message = (f"Delete completed walkthroughs older than {days} days now and at startup? "
                   "Interrupted drafts and standalone screenshots are never pruned."
                   if days else "Keep all walkthroughs until you delete them yourself?")
        if QMessageBox.question(self, "Walkthrough retention", message,
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                               QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            def apply():
                library.set_retention(self.root, days)
                library.prune(self.root)
            self._run(apply)

    def reject(self):
        if self._busy:
            self._cancel.set()
            self.notice.setText("Cancellation requested; waiting for the operation to stop safely…")
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
