from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QLabel, QHBoxLayout, QVBoxLayout, QWidget

from ui_qt.widgets.buttons import DangerButton, SuccessButton, WarningButton


class CompactRecordController(QWidget):
    record_requested = pyqtSignal()
    stop_requested = pyqtSignal()
    cancel_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("compactRecordController")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 18, 12, 18)
        layout.setSpacing(14)
        layout.addStretch()

        self.status_label = QLabel("Ready to record")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setFont(QFont("Segoe UI", 13))
        layout.addWidget(self.status_label)

        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)
        button_layout.setSpacing(8)

        self.record_button = SuccessButton("Record")
        self.stop_button = DangerButton("Stop")
        self.cancel_button = WarningButton("Cancel")
        for button in (self.record_button, self.stop_button, self.cancel_button):
            button.set_base_minimum_size(88, 44)
            button_layout.addWidget(button, stretch=1)

        self.stop_button.set_active(False)
        self.cancel_button.set_active(False)

        self.record_button.clicked.connect(self.record_requested)
        self.stop_button.clicked.connect(self.stop_requested)
        self.cancel_button.clicked.connect(self.cancel_requested)

        layout.addLayout(button_layout)
        layout.addStretch()

    def set_recording_state(self, recording: bool) -> None:
        self.record_button.set_active(not recording)
        self.record_button.setText("Recording" if recording else "Record")
        self.stop_button.set_active(recording)
        self.cancel_button.set_active(recording)

    def set_status(self, status_text: str) -> None:
        self.status_label.setText(status_text)

    def update_hotkeys(self, record_key: str, cancel_key: str) -> None:
        self.record_button.set_hotkey(record_key)
        self.stop_button.set_hotkey(record_key)
        self.cancel_button.set_hotkey(cancel_key)
