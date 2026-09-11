"""Meeting deletion options in a layout owned by the dialog."""

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
)


class MeetingDeleteDialog(QDialog):
    def __init__(self, parent=None, *, has_audio=False):
        super().__init__(parent)
        self.setWindowTitle("Delete Meeting")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)
        layout.addWidget(QLabel("Delete this meeting from Past Meetings?"))
        layout.addWidget(QLabel("This cannot be undone."))

        self.delete_recordings = QCheckBox(
            "Permanently delete saved recordings too", self
        )
        self.delete_recordings.setToolTip(
            "Leave unchecked to keep this meeting's saved recordings."
        )
        layout.addWidget(self.delete_recordings)
        self.delete_recordings.setVisible(has_audio)

        self.dont_ask_again = QCheckBox("Don't ask me again", self)
        layout.addWidget(self.dont_ask_again)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        delete_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        delete_button.setText("Delete")
        delete_button.setAutoDefault(False)
        cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        cancel_button.setDefault(True)
        cancel_button.setFocus()
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
