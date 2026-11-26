"""Dialog for viewing and editing Agent Notes, tags, and external links on a record."""

from __future__ import annotations

import logging
from typing import Any

from PyQt6.QtCore import Qt, QUrl
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from services import annotations as record_annotations
from services.format_utils import format_timestamp
from ui_qt.widgets import Button, DangerButton, PrimaryButton
from ui_qt.widgets.buttons import neutral_button

logger = logging.getLogger(__name__)

_DIALOG_STYLE = """
    QFrame#recordNotesSection {
        background-color: rgba(@surface-rgb, 0.55);
        border: 1px solid rgba(@overlay-rgb, 0.06);
        border-radius: 10px;
    }
    QLabel#recordNotesHeading {
        color: @text;
        background-color: transparent;
        font-size: 16px;
        font-weight: 700;
    }
    QLabel#recordNotesSubhead {
        color: @text-secondary;
        background-color: transparent;
        font-size: 11px;
    }
    QLabel#recordNotesFieldLabel {
        color: @text-secondary-strong;
        background-color: transparent;
        font-size: 11px;
        font-weight: 600;
    }
    QLabel#recordNotesTagChip {
        background-color: rgba(@purple-rgb, 0.12);
        color: @purple-text;
        border: 1px solid rgba(@purple-rgb, 0.25);
        border-radius: 6px;
        padding: 2px 8px;
        font-size: 10px;
        font-weight: 600;
    }
    QPlainTextEdit#recordNotesEditor {
        background-color: rgba(@surface-rgb, 0.72);
        color: @text;
        border: 1px solid rgba(@overlay-rgb, 0.10);
        border-radius: 8px;
        padding: 8px 10px;
    }
    QPlainTextEdit#recordNotesEditor:focus {
        border: 1px solid @accent;
    }
    QPushButton#recordNotesRefLink {
        background-color: rgba(@accent-rgb, 0.10);
        color: @accent-soft;
        border: 1px solid rgba(@accent-rgb, 0.24);
        border-radius: 6px;
        padding: 4px 10px;
        font-size: 11px;
        font-weight: 600;
        text-align: left;
    }
    QPushButton#recordNotesRefLink:hover {
        background-color: rgba(@accent-rgb, 0.20);
        border: 1px solid rgba(@accent-rgb, 0.42);
    }
    QLabel#recordNotesError {
        color: @danger-text;
        background-color: transparent;
        font-size: 11px;
    }
"""

_KIND_LABELS = {
    "transcription": "Transcription",
    "meeting": "Meeting",
    "walkthrough": "Walkthrough",
}


class RecordNotesDialog(QDialog):
    """Unobtrusive viewer & editor for agent notes, tags, and external references."""

    def __init__(
        self,
        record_kind: str,
        record_id: str,
        *,
        record_title: str = "",
        annotation: dict[str, Any] | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.record_kind = record_kind
        self.record_id = record_id
        self.record_title = (record_title or "").strip()
        self.annotation: dict[str, Any] = (
            dict(annotation)
            if annotation is not None
            else record_annotations.get_annotation(record_kind, record_id)
        )
        self.changed = False

        kind_name = _KIND_LABELS.get(record_kind, record_kind.title())
        self.setWindowTitle(f"Agent Notes · {kind_name}")
        self.setModal(True)
        self.setMinimumSize(540, 480)
        self.resize(620, 560)
        self.setStyleSheet(_DIALOG_STYLE)

        self._setup_ui(kind_name)

    def _setup_ui(self, kind_name: str) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(8)
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        heading_text = self.record_title or f"{kind_name} Notes"
        heading = QLabel(heading_text)
        heading.setObjectName("recordNotesHeading")
        heading.setWordWrap(True)
        title_box.addWidget(heading)

        meta_parts = [f"ID: {self.record_id}"]
        if self.annotation.get("author"):
            meta_parts.append(f"By {self.annotation['author']}")
        if self.annotation.get("updated_at"):
            meta_parts.append(f"Updated {format_timestamp(self.annotation['updated_at'])}")
        self.meta_label = QLabel(" · ".join(meta_parts))
        self.meta_label.setObjectName("recordNotesSubhead")
        self.meta_label.setWordWrap(True)
        self.meta_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        title_box.addWidget(self.meta_label)
        header.addLayout(title_box, 1)

        self.copy_id_button = neutral_button(Button("Copy ID"))
        self.copy_id_button.set_base_minimum_size(0, 32)
        self.copy_id_button.setToolTip(
            "Copy this record's stable unique ID to reference it from an agent or external database"
        )
        self.copy_id_button.clicked.connect(self._copy_record_id)
        header.addWidget(self.copy_id_button, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        # Summary & Tags card
        card = QFrame()
        card.setObjectName("recordNotesSection")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(8)

        summary_label = QLabel("Summary")
        summary_label.setObjectName("recordNotesFieldLabel")
        card_layout.addWidget(summary_label)

        self.summary_edit = QLineEdit()
        self.summary_edit.setPlaceholderText("Short 1–2 line summary (optional)")
        self.summary_edit.setMaxLength(record_annotations.MAX_SUMMARY_CHARS)
        self.summary_edit.setText(self.annotation.get("summary") or "")
        card_layout.addWidget(self.summary_edit)

        tags_label = QLabel("Tags (comma-separated)")
        tags_label.setObjectName("recordNotesFieldLabel")
        card_layout.addWidget(tags_label)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("e.g. project:alpha, follow-up, idea")
        self.tags_edit.setText(", ".join(self.annotation.get("tags") or ()))
        card_layout.addWidget(self.tags_edit)

        notes_label = QLabel("Notes")
        notes_label.setObjectName("recordNotesFieldLabel")
        card_layout.addWidget(notes_label)

        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setObjectName("recordNotesEditor")
        self.notes_edit.setFont(QFont("Segoe UI", 11))
        self.notes_edit.setPlaceholderText(
            "Agent or personal notes, takeaways, action items, or context…"
        )
        self.notes_edit.setPlainText(self.annotation.get("notes") or "")
        self.notes_edit.setMinimumHeight(180)
        card_layout.addWidget(self.notes_edit, 1)
        outer.addWidget(card, 1)

        # External references (read-only links to agent's external DB / Obsidian / Notion / etc.)
        refs = list(self.annotation.get("external_refs") or ())
        self._external_refs = refs
        if refs:
            refs_card = QFrame()
            refs_card.setObjectName("recordNotesSection")
            refs_layout = QVBoxLayout(refs_card)
            refs_layout.setContentsMargins(14, 10, 14, 10)
            refs_layout.setSpacing(6)
            refs_title = QLabel("External links")
            refs_title.setObjectName("recordNotesFieldLabel")
            refs_layout.addWidget(refs_title)
            for ref in refs:
                label = ref.get("label") or ref["id"]
                # Escape '&' so Qt does not turn it into a mnemonic underline.
                text = f"{ref['system']} · {label}".replace("&", "&&")
                btn = QPushButton(text)
                btn.setObjectName("recordNotesRefLink")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                url = ref.get("url")
                if url:
                    btn.setToolTip(f"Open {url}")
                    btn.clicked.connect(lambda _checked=False, target=url: QDesktopServices.openUrl(QUrl(target)))
                else:
                    btn.setToolTip(f"Copy external reference: {ref['system']}:{ref['id']}")
                    btn.clicked.connect(
                        lambda _checked=False, value=f"{ref['system']}:{ref['id']}": self._copy_text(value)
                    )
                refs_layout.addWidget(btn)
            outer.addWidget(refs_card)

        self.error_label = QLabel("")
        self.error_label.setObjectName("recordNotesError")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        outer.addWidget(self.error_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)

        self.copy_notes_button = neutral_button(Button("Copy Notes"))
        self.copy_notes_button.set_base_minimum_size(0, 36)
        self.copy_notes_button.clicked.connect(self._copy_notes)
        actions.addWidget(self.copy_notes_button)

        self.clear_button = DangerButton("Clear Notes")
        self.clear_button.set_base_minimum_size(0, 36)
        self.clear_button.setVisible(bool(self.annotation.get("exists")))
        self.clear_button.clicked.connect(self._on_clear)
        actions.addWidget(self.clear_button)

        actions.addStretch()

        cancel_button = Button("Close")
        cancel_button.set_base_minimum_size(84, 36)
        cancel_button.clicked.connect(self.reject)
        actions.addWidget(cancel_button)

        self.save_button = PrimaryButton("Save")
        self.save_button.set_base_minimum_size(84, 36)
        self.save_button.clicked.connect(self._on_save)
        actions.addWidget(self.save_button)
        outer.addLayout(actions)

    def _copy_text(self, value: str) -> None:
        try:
            QApplication.clipboard().setText(value)
        except Exception:
            logger.debug("Could not copy text to clipboard", exc_info=True)

    def _copy_record_id(self) -> None:
        self._copy_text(self.record_id)
        self.copy_id_button.setText("Copied ID")

    def _copy_notes(self) -> None:
        parts: list[str] = []
        summary = self.summary_edit.text().strip()
        tags = self.tags_edit.text().strip()
        notes = self.notes_edit.toPlainText().strip()
        if summary:
            parts.append(summary)
        if tags:
            parts.append(f"Tags: {tags}")
        if notes:
            parts.append(notes)
        if parts:
            self._copy_text("\n\n".join(parts))
            self.copy_notes_button.setText("Copied")

    def _on_save(self) -> None:
        raw_tags = [
            part.strip()
            for part in self.tags_edit.text().split(",")
            if part.strip()
        ]
        try:
            saved = record_annotations.save_annotation(
                self.record_kind,
                self.record_id,
                summary=self.summary_edit.text(),
                notes=self.notes_edit.toPlainText(),
                tags=raw_tags,
                external_refs=self._external_refs,
                author=self.annotation.get("author") or "User",
            )
        except record_annotations.AnnotationValidationError as exc:
            self.error_label.setText(str(exc))
            self.error_label.show()
            return
        except Exception as exc:
            logger.warning("Could not save record annotation: %s", exc, exc_info=True)
            self.error_label.setText("Could not save notes right now. Try again.")
            self.error_label.show()
            return
        self.annotation = saved
        self.changed = True
        self.accept()

    def _on_clear(self) -> None:
        reply = QMessageBox.question(
            self,
            "Clear Agent Notes",
            "Remove the agent notes, tags, and links from this record?\n\n"
            "The transcript itself will not be changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            record_annotations.delete_annotation(self.record_kind, self.record_id)
        except Exception as exc:
            logger.warning("Could not clear record annotation: %s", exc, exc_info=True)
            self.error_label.setText("Could not clear notes right now. Try again.")
            self.error_label.show()
            return
        self.annotation = record_annotations.empty_annotation(self.record_kind, self.record_id)
        self.changed = True
        self.accept()
