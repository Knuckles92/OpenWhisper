"""Make a write-up style from scratch: a name and the words it fills in."""

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QVBoxLayout,
)

from services.settings import settings_manager
from services.walkthrough.styles import MAX_NAME, WriteUpStyle, save_style
from ui_qt.widgets import Button, PrimaryButton


class NewStyleDialog(QDialog):
    """Saves the style on Save; ``saved`` is it once accepted.

    Not ``style``: that would hide QWidget.style().
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New style")
        self.setMinimumSize(480, 380)
        self.resize(540, 420)
        self.saved: WriteUpStyle | None = None
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("New write-up style")
        title.setObjectName("headerLabel")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.DemiBold))
        layout.addWidget(title)
        info = QLabel("Say what the AI write-up should be. The style shows under "
                      "Start from here and in a storyboard's Redo as.")
        info.setObjectName("infoLabel")
        info.setWordWrap(True)
        layout.addWidget(info)

        self.name_edit = QLineEdit()
        self.name_edit.setAccessibleName("Style name")
        self.name_edit.setMaxLength(MAX_NAME)
        self.name_edit.setPlaceholderText("Name, such as Release notes")
        self.name_edit.setToolTip("A name already saved is replaced.")
        layout.addWidget(self.name_edit)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setAccessibleName("Style prompt")
        self.prompt_edit.setPlaceholderText("Say what you want, such as \"a short checklist "
                                            "for a new teammate, one screenshot per step\".")
        self.prompt_edit.setTabChangesFocus(True)
        # The Walkthrough tab's field look; the theme has none for a bare QPlainTextEdit.
        self.prompt_edit.setStyleSheet(
            "QPlainTextEdit { color: @text; background-color: @slate-field;"
            " border: 1px solid @slate-border; border-radius: 10px; padding: 6px 8px;"
            " font-size: 13px; }"
            " QPlainTextEdit:focus { border-color: @accent; }")
        layout.addWidget(self.prompt_edit, stretch=1)

        self.error = QLabel("")
        self.error.setStyleSheet("QLabel { color: @danger-text-soft; font-size: 12px; }")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = Button("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = PrimaryButton("Save style")
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)
        self.name_edit.setFocus()

    def _save(self) -> None:
        name, words = self.name_edit.text(), self.prompt_edit.toPlainText()
        try:
            self.saved = settings_manager.mutate_settings(
                lambda settings: save_style(settings, name, words))
        except ValueError as exc:
            self.error.setText(str(exc))
            self.error.show()
            return
        self.accept()
