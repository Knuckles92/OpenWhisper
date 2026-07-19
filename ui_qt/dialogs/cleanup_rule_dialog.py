"""
Confirm/edit dialog for a single learned cleanup rule.
"""
from typing import Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
)
from PyQt6.QtGui import QFont

from ui_qt.widgets import PrimaryButton, Button


class CleanupRuleDialog(QDialog):
    """Popup to confirm an AI-polished learned rule or edit an existing one."""

    def __init__(
        self,
        rule: str,
        original: Optional[str] = None,
        notice: Optional[str] = None,
        parent=None,
    ):
        """Initialize the rule dialog.

        Args:
            rule: Rule text to confirm or edit.
            original: The raw instruction the rule was polished from, shown
                read-only for reference. None when editing an existing rule.
            notice: Optional warning line (e.g. AI polish unavailable).
            parent: Optional parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle(
            "Confirm Learned Rule" if original is not None else "Edit Learned Rule"
        )
        self.setMinimumSize(460, 260)
        self.resize(500, 300)
        self._setup_ui(rule, original, notice)

    def _setup_ui(
        self, rule: str, original: Optional[str], notice: Optional[str]
    ) -> None:
        """Build the dialog layout."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel(
            "Confirm Learned Rule" if original is not None else "Edit Learned Rule"
        )
        title.setObjectName("headerLabel")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.DemiBold))
        layout.addWidget(title)

        if original is not None and original.strip():
            said = QLabel(f'You said: "{original.strip()}"')
            said.setObjectName("infoLabel")
            said.setWordWrap(True)
            layout.addWidget(said)

        if notice:
            warn = QLabel(notice)
            warn.setObjectName("infoLabel")
            warn.setWordWrap(True)
            layout.addWidget(warn)

        info = QLabel(
            "This rule is added to the cleanup prompt on every transcript. "
            "Edit it if needed, then save."
        )
        info.setObjectName("infoLabel")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.rule_edit = QTextEdit()
        self.rule_edit.setAcceptRichText(False)
        self.rule_edit.setFont(QFont("Segoe UI", 12))
        self.rule_edit.setPlainText(rule or "")
        self.rule_edit.setPlaceholderText("Enter the rule…")
        self.rule_edit.setMinimumHeight(80)
        layout.addWidget(self.rule_edit, stretch=1)

        buttons = QHBoxLayout()
        buttons.addStretch()

        cancel_btn = Button("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)

        save_btn = PrimaryButton("Save Rule")
        save_btn.clicked.connect(self.accept)
        buttons.addWidget(save_btn)

        layout.addLayout(buttons)

    def rule_text(self) -> str:
        """Return the edited rule text."""
        return self.rule_edit.toPlainText().strip()
