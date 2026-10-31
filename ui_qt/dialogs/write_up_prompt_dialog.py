"""Write a walkthrough up again with a prompt typed for it, maybe kept as a style."""

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
from services.walkthrough.styles import MAX_NAME, load_styles, save_style, style_for
from ui_qt.widgets import Button, PrimaryButton
from ui_qt.widgets.no_wheel import NoWheelComboBox

#: The style menu's first item while the prompt is the user's own words.
OWN_WORDS = "Your own words"


class WriteUpPromptDialog(QDialog):
    """The prompt for one write-up; Settings' prompt for the next ones stays."""

    def __init__(self, prompt: str, parent=None, *, written: bool = True):
        super().__init__(parent)
        self.setWindowTitle("Redo write-up" if written else "Write up with AI")
        self.setMinimumSize(520, 440)
        self.resize(580, 500)
        self._styles = load_styles(settings_manager.load_all_settings())
        self._build(prompt, written)

    def _build(self, prompt: str, written: bool) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        title = QLabel("What should the write-up be?")
        title.setObjectName("headerLabel")
        title.setFont(QFont("Segoe UI", 14, QFont.Weight.DemiBold))
        layout.addWidget(title)
        info = QLabel(
            "The agent writes it up from your words as spoken, with this prompt"
            + (" instead, and replaces the current write-up. " if written else ". ")
            + "Your prompt for new walkthroughs doesn't change.")
        info.setObjectName("infoLabel")
        info.setWordWrap(True)
        layout.addWidget(info)

        head = QHBoxLayout()
        head.addWidget(QLabel("Start from"))
        self.style_combo = NoWheelComboBox()
        self.style_combo.setAccessibleName("Write-up style")
        for style in self._styles:
            self.style_combo.addItem(style.name, style.instructions)
        self.style_combo.activated.connect(self._on_style_chosen)
        head.addWidget(self.style_combo, 1)
        layout.addLayout(head)

        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setAccessibleName("Write-up prompt")
        self.prompt_edit.setPlaceholderText("Say what you want, such as \"a short checklist "
                                            "for a new teammate, one screenshot per step\".")
        self.prompt_edit.setTabChangesFocus(True)
        self.prompt_edit.setPlainText(prompt)
        self.prompt_edit.textChanged.connect(self._show_style)
        layout.addWidget(self.prompt_edit, stretch=1)

        self.name_edit = QLineEdit()
        self.name_edit.setAccessibleName("Style name")
        self.name_edit.setMaxLength(MAX_NAME)
        self.name_edit.setPlaceholderText("Name it to keep it as a style (optional)")
        self.name_edit.setToolTip("Saved styles show in the Redo as menu and the "
                                  "Walkthrough tab. A name already saved is replaced.")
        layout.addWidget(self.name_edit)

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
        self.go_button = PrimaryButton("Redo write-up" if written else "Write it up")
        self.go_button.clicked.connect(self._go)
        buttons.addWidget(self.go_button)
        layout.addLayout(buttons)
        self._show_style()
        self.prompt_edit.setFocus()
        self.prompt_edit.selectAll()

    def prompt_text(self) -> str:
        return self.prompt_edit.toPlainText().strip()

    def _on_style_chosen(self, index: int) -> None:
        words = self.style_combo.itemData(index)
        if words is not None and words != self.prompt_text():
            self.prompt_edit.setPlainText(words)

    def _show_style(self) -> None:
        """Point the menu at the style the prompt is, or at the user's own words."""
        style = style_for(self.prompt_text(), self._styles) if self.prompt_text() else None
        own = self.style_combo.findText(OWN_WORDS)
        if style is None and own < 0:
            self.style_combo.insertItem(0, OWN_WORDS)
            own = 0
        elif style is not None and own >= 0:
            self.style_combo.removeItem(own)
        self.style_combo.setCurrentIndex(
            self.style_combo.findText(style.name) if style else self.style_combo.findText(OWN_WORDS))

    def _go(self) -> None:
        text = self.prompt_text()
        if not text:
            self._fail("Type what you want the write-up to be.")
            return
        name = self.name_edit.text().strip()
        if name:
            try:
                settings_manager.mutate_settings(lambda settings: save_style(settings, name, text))
            except ValueError as exc:
                self._fail(str(exc))
                return
        self.accept()

    def _fail(self, message: str) -> None:
        self.error.setText(message)
        self.error.show()
