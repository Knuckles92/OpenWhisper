"""
Transcription statistics display widget for PyQt6 UI.
"""
from PyQt6.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont


class TranscriptionStatsWidget(QWidget):
    """Displays transcription statistics in a compact horizontal layout."""

    # Signal emitted when visibility changes (True = shown, False = hidden)
    visibility_changed = pyqtSignal(bool)

    def __init__(self, parent=None):
        """Initialize the stats widget."""
        super().__init__(parent)
        self.setObjectName("statsWidget")
        self._setup_ui()
        self.hide()  # Hidden by default until stats are set

    def _setup_ui(self):
        """Setup the user interface."""
        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(16, 12, 16, 12)
        main_layout.setSpacing(24)

        # Add stretch to center content
        main_layout.addStretch()

        # Transcription time stat
        self.transcription_time_widget = self._create_stat_item(
            "Transcription Time",
            "--"
        )
        main_layout.addWidget(self.transcription_time_widget)

        # Separator
        main_layout.addWidget(self._create_separator())

        # Audio duration stat
        self.audio_duration_widget = self._create_stat_item(
            "Audio Duration",
            "--"
        )
        main_layout.addWidget(self.audio_duration_widget)

        # Separator
        main_layout.addWidget(self._create_separator())

        # File size stat
        self.file_size_widget = self._create_stat_item(
            "File Size",
            "--"
        )
        main_layout.addWidget(self.file_size_widget)

        # Add stretch to center content
        main_layout.addStretch()

    def _create_stat_item(self, label_text: str, value_text: str) -> QWidget:
        """Create a single stat item with label and value.

        Args:
            label_text: The label for the stat.
            value_text: The initial value text.

        Returns:
            QWidget containing the stat item.
        """
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        # Label
        label = QLabel(label_text)
        label.setObjectName("statsLabel")
        label.setFont(QFont("Segoe UI", 10))
        label.setStyleSheet("color: #8e8e93;")  # Secondary text color
        layout.addWidget(label)

        # Value
        value = QLabel(value_text)
        value.setObjectName("statsValue")
        value.setFont(QFont("Segoe UI", 13, QFont.Weight.Bold))
        value.setStyleSheet("color: #f5f5f7;")  # Primary text color
        layout.addWidget(value)

        # Store reference to value label for updates
        widget.value_label = value

        return widget

    def _create_separator(self) -> QWidget:
        """Create a vertical separator line.

        Returns:
            QWidget representing the separator.
        """
        separator = QWidget()
        separator.setFixedWidth(1)
        separator.setStyleSheet("background-color: #3a3a3c;")
        return separator

    def set_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int
    ):
        """Update displayed statistics.

        Args:
            transcription_time: Time taken to transcribe in seconds.
            audio_duration: Duration of the audio in seconds.
            file_size: Size of the audio file in bytes.
        """
        # Format transcription time
        if transcription_time < 60:
            time_str = f"{transcription_time:.1f}s"
        else:
            minutes = int(transcription_time // 60)
            seconds = transcription_time % 60
            time_str = f"{minutes}m {seconds:.0f}s"
        self.transcription_time_widget.value_label.setText(time_str)

        # Format audio duration
        if audio_duration < 60:
            duration_str = f"{audio_duration:.1f}s"
        else:
            minutes = int(audio_duration // 60)
            seconds = int(audio_duration % 60)
            duration_str = f"{minutes}:{seconds:02d}"
        self.audio_duration_widget.value_label.setText(duration_str)

        # Format file size
        file_size_str = self._format_file_size(file_size)
        self.file_size_widget.value_label.setText(file_size_str)

        self.show()
        self.visibility_changed.emit(True)

    def _format_file_size(self, size_bytes: int) -> str:
        """Format file size in human-readable format.

        Args:
            size_bytes: Size in bytes.

        Returns:
            Human-readable file size string.
        """
        if size_bytes < 1024:
            return f"{size_bytes} B"
        elif size_bytes < 1024 * 1024:
            return f"{size_bytes / 1024:.1f} KB"
        else:
            return f"{size_bytes / (1024 * 1024):.2f} MB"

    def clear(self):
        """Clear statistics display and hide the widget."""
        self.transcription_time_widget.value_label.setText("--")
        self.audio_duration_widget.value_label.setText("--")
        self.file_size_widget.value_label.setText("--")
        self.hide()
        self.visibility_changed.emit(False)
