"""Meeting deletion options in a layout owned by the dialog."""

from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from ui_qt.widgets.buttons import Button, compact_danger_button, neutral_button


class MeetingDeleteDialog(QDialog):
    def __init__(self, parent=None, *, has_audio=False):
        super().__init__(parent)
        self.setObjectName("meetingDeleteDialog")
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

        # Cancel, then the action on the right, as in the app's other dialogs;
        # a platform button box put Delete first on some desktops.
        self.buttons = QWidget(self)
        row = QHBoxLayout(self.buttons)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addStretch()
        self.cancel_button = neutral_button(Button("Cancel"))
        self.cancel_button.clicked.connect(self.reject)
        row.addWidget(self.cancel_button)
        self.delete_button = compact_danger_button(Button("Delete"))
        self.delete_button.setAutoDefault(False)
        self.delete_button.clicked.connect(self.accept)
        row.addWidget(self.delete_button)
        layout.addWidget(self.buttons)
        # Enter keeps the harmless choice.
        self.cancel_button.setDefault(True)
        self.cancel_button.setFocus()
