from typing import Optional

from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from services.format_utils import format_audio_duration, format_file_size


class TranscriptionStatsWidget(QWidget):
    visibility_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("statsWidget")
        self._setup_ui()
        self.hide()

    def _setup_ui(self):
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(24)

        main_layout.addStretch()

        self.transcription_time_widget = self._create_stat_item(
            "Transcription Time",
            "--"
        )
        main_layout.addWidget(self.transcription_time_widget)

        main_layout.addWidget(self._create_separator())

        self.audio_duration_widget = self._create_stat_item(
            "Audio Duration",
            "--"
        )
        main_layout.addWidget(self.audio_duration_widget)

        main_layout.addWidget(self._create_separator())

        self.file_size_widget = self._create_stat_item(
            "File Size",
            "--"
        )
        main_layout.addWidget(self.file_size_widget)

        main_layout.addStretch()

    def _create_stat_item(self, label_text: str, value_text: str) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        label = QLabel(label_text)
        label.setObjectName("statsLabel")
        label.setFont(QFont("Segoe UI", 10))
        label.setStyleSheet("color: #8e8e93;")
        layout.addWidget(label)

        value = QLabel(value_text)
        value.setObjectName("statsValue")
        value.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        value.setStyleSheet("color: #f5f5f7;")
        layout.addWidget(value)

        widget.value_label = value

        return widget

    def _create_separator(self) -> QWidget:
        separator = QWidget()
        separator.setFixedWidth(1)
        separator.setStyleSheet("background-color: #3a3a3c;")
        return separator

    def set_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int,
        cleanup_time: Optional[float] = None,
    ):
        self.transcription_time_widget.value_label.setText(
            format_audio_duration(transcription_time)
        )
        self.audio_duration_widget.value_label.setText(
            format_audio_duration(audio_duration)
        )

        self.file_size_widget.value_label.setText(format_file_size(file_size))

        self.show()
        self.visibility_changed.emit(True)

    def clear(self):
        self.transcription_time_widget.value_label.setText("--")
        self.audio_duration_widget.value_label.setText("--")
        self.file_size_widget.value_label.setText("--")
        self.hide()
        self.visibility_changed.emit(False)
