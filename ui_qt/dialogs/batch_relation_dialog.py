"""Describe how the files of a multi-file upload relate, for the cleanup model."""
from typing import Sequence

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QTextEdit,
    QVBoxLayout,
)

from config import config
from ui_qt.widgets import Button, ElidingLabel, PrimaryButton, WrappedLabel

#: File names listed in the dialog before the rest collapse into a count.
_MAX_LISTED_FILES = 6


class BatchRelationDialog(QDialog):
    def __init__(
        self,
        filenames: Sequence[str],
        instructions: str = "",
        combine: bool = False,
        parent=None,
    ):
        """Edit the Custom preset's description and output shape.

        Args:
            filenames: The queued files, shown for reference while writing.
            instructions: The remembered description, prefilled.
            combine: Whether the remembered choice was one combined transcript.
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setObjectName("batchRelationDialog")
        self.setWindowTitle("Describe These Files")
        self.setModal(True)
        self.setMinimumSize(560, 460)
        self.resize(620, 520)
        self._setup_ui(list(filenames), instructions, combine)

    def _setup_ui(self, filenames: list, instructions: str, combine: bool) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        title = QLabel("Describe these files")
        title.setObjectName("batchRelationTitle")
        title.setFont(QFont("Segoe UI", 15, QFont.Weight.DemiBold))
        layout.addWidget(title)

        info = WrappedLabel(
            "Tell the cleanup model what these recordings are and how it should "
            "treat them: who is speaking, what belongs together, what to drop. "
            "The transcripts themselves are never treated as instructions."
        )
        info.setObjectName("batchRelationInfo")
        layout.addWidget(info)

        files_card = QFrame()
        files_card.setObjectName("batchRelationFilesCard")
        files_layout = QVBoxLayout(files_card)
        files_layout.setContentsMargins(14, 12, 14, 12)
        files_layout.setSpacing(4)
        files_label = QLabel(f"FILES IN THIS BATCH  ·  {len(filenames)}")
        files_label.setObjectName("batchRelationFilesLabel")
        files_layout.addWidget(files_label)
        for name in filenames[:_MAX_LISTED_FILES]:
            row = ElidingLabel(name)
            row.setObjectName("batchRelationFileName")
            files_layout.addWidget(row)
        hidden = len(filenames) - _MAX_LISTED_FILES
        if hidden > 0:
            more = QLabel(f"+{hidden} more")
            more.setObjectName("batchRelationFileName")
            files_layout.addWidget(more)
        layout.addWidget(files_card)

        editor = QFrame()
        editor.setObjectName("batchRelationEditorCard")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(14, 12, 14, 14)
        editor_layout.setSpacing(8)

        editor_label = QLabel("HOW THEY RELATE")
        editor_label.setObjectName("batchRelationEditorLabel")
        editor_layout.addWidget(editor_label)

        self.instructions_edit = QTextEdit()
        self.instructions_edit.setObjectName("batchRelationEditor")
        self.instructions_edit.setAcceptRichText(False)
        self.instructions_edit.setFont(QFont("Segoe UI", 12))
        self.instructions_edit.setPlainText(instructions or "")
        self.instructions_edit.setPlaceholderText(
            "e.g. Three interviews with the same client, recorded on different "
            "days. Keep each one separate and drop the small talk at the start."
        )
        self.instructions_edit.setMinimumHeight(110)
        editor_layout.addWidget(self.instructions_edit, stretch=1)
        layout.addWidget(editor, stretch=1)

        self.combine_check = QCheckBox("Combine into one transcript")
        self.combine_check.setObjectName("batchRelationCombine")
        self.combine_check.setChecked(bool(combine))
        self.combine_check.setToolTip(
            "On: the files are joined in order and cleaned as one transcript "
            "with one History entry. Off: each file is cleaned and saved on its own."
        )
        layout.addWidget(self.combine_check)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch()

        cancel_btn = Button("Cancel")
        cancel_btn.set_base_minimum_size(92, 42)
        cancel_btn.setAutoDefault(False)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)

        self.use_btn = PrimaryButton("Use Description")
        self.use_btn.set_base_minimum_size(140, 42)
        self.use_btn.setDefault(True)
        self.use_btn.clicked.connect(self.accept)
        buttons.addWidget(self.use_btn)
        layout.addLayout(buttons)

        self.instructions_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def instructions_text(self) -> str:
        text = self.instructions_edit.toPlainText().strip()
        return text[: config.MAX_TRANSCRIPT_BATCH_INSTRUCTION_CHARS]

    def combine_checked(self) -> bool:
        return self.combine_check.isChecked()
