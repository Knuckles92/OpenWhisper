from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit, QFrame,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

from ui_qt.widgets import PrimaryButton, Button


class CleanupRuleDialog(QDialog):
    def __init__(
        self,
        rule: str,
        original: Optional[str] = None,
        notice: Optional[str] = None,
        parent=None,
    ):
        """Offer the polished rule and, when applicable, its original wording.

        Args:
            rule: Rule text to confirm or edit (polished text when confirming).
            original: The raw instruction the rule was polished from. None when
                editing an existing rule. When set and polish succeeded, the
                user can choose polished (recommended) or exactly as typed.
            notice: Optional warning line (e.g. AI polish unavailable).
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setObjectName("cleanupRuleDialog")
        self._original = (original or "").strip()
        self._polished = (rule or "").strip()
        self._offer_choice = (
            original is not None
            and not notice
            and bool(self._original)
            and self._polished
            and self._polished.casefold() != self._original.casefold()
        )
        self.setWindowTitle(
            "Confirm Learned Rule" if original is not None else "Edit Learned Rule"
        )
        self.setMinimumSize(620, 330 if self._offer_choice else 300)
        self.resize(660, 410 if self._offer_choice else 360)
        self._setup_ui(rule, original, notice)

    def _setup_ui(
        self, rule: str, original: Optional[str], notice: Optional[str]
    ) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(14)
        mark = QLabel("AI" if original is not None else "Aa")
        mark.setObjectName("cleanupRuleDialogMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(44, 44)
        header.addWidget(mark)

        heading = QVBoxLayout()
        heading.setSpacing(2)
        title = QLabel(
            "Review your new rule" if original is not None else "Edit learned rule"
        )
        title.setObjectName("cleanupRuleDialogTitle")
        title.setFont(QFont("Segoe UI", 15, QFont.Weight.DemiBold))
        heading.addWidget(title)

        subtitle = QLabel(
            "This instruction becomes part of your personal cleanup profile."
        )
        subtitle.setObjectName("cleanupRuleDialogSubtitle")
        subtitle.setWordWrap(True)
        heading.addWidget(subtitle)
        header.addLayout(heading, stretch=1)
        layout.addLayout(header)

        if original is not None and original.strip():
            said = QLabel(f'You said  ·  “{original.strip()}”')
            said.setObjectName("cleanupRuleOriginal")
            said.setWordWrap(True)
            layout.addWidget(said)

        if notice:
            warn = QLabel(notice)
            warn.setObjectName("cleanupRuleNotice")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        if self._offer_choice:
            info = QLabel(
                "AI polished your instruction into a clearer rule for the cleanup "
                "prompt. We recommend the polished version, or you can keep exactly "
                "what you typed. Edit either choice below before saving."
            )
        else:
            info = QLabel(
                "This rule is added to the cleanup prompt on every transcript. "
                "Edit it if needed, then save."
            )
        info.setObjectName("cleanupRuleDialogInfo")
        info.setWordWrap(True)
        layout.addWidget(info)

        editor = QFrame()
        editor.setObjectName("cleanupRuleEditorCard")
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(14, 12, 14, 14)
        editor_layout.setSpacing(8)

        editor_label = QLabel("RULE INSTRUCTION")
        editor_label.setObjectName("cleanupRuleEditorLabel")
        editor_layout.addWidget(editor_label)

        self.rule_edit = QTextEdit()
        self.rule_edit.setObjectName("cleanupRuleEditor")
        self.rule_edit.setAcceptRichText(False)
        self.rule_edit.setFont(QFont("Segoe UI", 12))
        self.rule_edit.setPlainText(rule or "")
        self.rule_edit.setPlaceholderText("Enter the rule…")
        self.rule_edit.setMinimumHeight(80)
        editor_layout.addWidget(self.rule_edit, stretch=1)
        layout.addWidget(editor, stretch=1)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch()

        cancel_btn = Button("Cancel")
        cancel_btn.set_base_minimum_size(92, 42)
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)

        if self._offer_choice:
            as_typed_btn = Button("Use Exactly as Typed")
            as_typed_btn.set_base_minimum_size(156, 42)
            as_typed_btn.setToolTip("Save your original wording without AI changes")
            as_typed_btn.clicked.connect(self._accept_as_typed)
            buttons.addWidget(as_typed_btn)

            polished_btn = PrimaryButton("Use Polished (Recommended)")
            polished_btn.set_base_minimum_size(190, 42)
            polished_btn.setToolTip(
                "Save the AI-polished rule (or your edits to it)"
            )
            polished_btn.clicked.connect(self._accept_polished)
            buttons.addWidget(polished_btn)
        else:
            save_btn = PrimaryButton("Save Rule")
            save_btn.set_base_minimum_size(112, 42)
            save_btn.clicked.connect(self.accept)
            buttons.addWidget(save_btn)

        layout.addLayout(buttons)

    def _accept_as_typed(self) -> None:
        self.rule_edit.setPlainText(self._original)
        self.accept()

    def _accept_polished(self) -> None:
        text = self.rule_edit.toPlainText().strip()
        if not text:
            self.rule_edit.setPlainText(self._polished)
        self.accept()

    def rule_text(self) -> str:
        return self.rule_edit.toPlainText().strip()
