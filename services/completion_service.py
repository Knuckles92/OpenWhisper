"""
Completion handler for post-transcription processing.

Handles history management, clipboard operations, auto-paste, and statistics.
"""

import logging
from typing import Optional, Callable

import pyperclip
import keyboard

from PyQt6.QtCore import QObject, pyqtSignal

from services.settings_service import settings_manager
from services.history_service import history_service


class CompletionService(QObject):
    """Handles post-transcription processing including history, clipboard, and auto-paste."""

    # Signals
    status_update = pyqtSignal(str)
    transcription_display = pyqtSignal(str)  # For updating UI transcription display
    stats_update = pyqtSignal(float, float, int)  # transcription_time, audio_duration, file_size
    history_refresh = pyqtSignal()  # Signal to refresh history display

    def __init__(self, get_model_info: Callable[[], str]):
        """Initialize completion handler.

        Args:
            get_model_info: Callback to get current model info string.
        """
        super().__init__()
        self._get_model_info = get_model_info

        # Pending transcription metadata
        self._pending_audio_file: Optional[str] = None
        self._pending_audio_duration: Optional[float] = None
        self._pending_file_size: Optional[int] = None
        self._transcription_start_time: Optional[float] = None

    def set_pending_metadata(
        self,
        audio_file: Optional[str] = None,
        audio_duration: Optional[float] = None,
        file_size: Optional[int] = None,
        start_time: Optional[float] = None
    ):
        """Set metadata for pending transcription.

        Args:
            audio_file: Path to source audio file (for history).
            audio_duration: Duration of audio in seconds.
            file_size: Size of audio file in bytes.
            start_time: Transcription start time (time.time()).
        """
        self._pending_audio_file = audio_file
        self._pending_audio_duration = audio_duration
        self._pending_file_size = file_size
        self._transcription_start_time = start_time

    def on_transcription_complete(self, transcribed_text: str, transcription_time: Optional[float] = None):
        """Handle transcription completion.

        Args:
            transcribed_text: The transcribed text.
            transcription_time: Time taken for transcription in seconds.
        """
        # Emit transcription to UI
        self.transcription_display.emit(transcribed_text)
        self.status_update.emit("Transcription complete!")

        # Update stats if available
        if transcription_time is not None:
            audio_duration = self._pending_audio_duration or 0.0
            file_size = self._pending_file_size or 0
            self.stats_update.emit(transcription_time, audio_duration, file_size)

        # Save to history
        self._save_to_history(transcribed_text, transcription_time)

        # Handle clipboard and auto-paste
        self._handle_clipboard_and_paste(transcribed_text)

        # Clear pending metadata
        self._clear_pending_metadata()

    def on_transcription_error(self, error_message: str):
        """Handle transcription error.

        Args:
            error_message: The error message.
        """
        self.status_update.emit(f"Error: {error_message}")
        self.transcription_display.emit(f"Error: {error_message}")
        self._clear_pending_metadata()

    def _save_to_history(self, transcribed_text: str, transcription_time: Optional[float]):
        """Save transcription to history."""
        try:
            model_info = self._get_model_info()

            history_service.add_entry(
                text=transcribed_text,
                model=model_info,
                source_audio_file=self._pending_audio_file,
                transcription_time=transcription_time,
                audio_duration=self._pending_audio_duration,
                file_size=self._pending_file_size
            )
            self.history_refresh.emit()
            logging.info("Transcription saved to history")
        except Exception as e:
            logging.error(f"Failed to save transcription to history: {e}")

    def _handle_clipboard_and_paste(self, transcribed_text: str):
        """Handle clipboard copy and auto-paste based on settings."""
        settings = settings_manager.load_all_settings()
        copy_clipboard = settings.get('copy_clipboard', True)
        auto_paste = settings.get('auto_paste', True)

        if copy_clipboard:
            try:
                pyperclip.copy(transcribed_text)
                logging.info("Transcription copied to clipboard")
            except Exception as e:
                logging.error(f"Failed to copy to clipboard: {e}")

        if auto_paste:
            try:
                keyboard.send('ctrl+v')
                logging.info("Transcription auto-pasted")
                self.status_update.emit("Ready (Pasted)")
            except Exception as e:
                logging.error(f"Failed to auto-paste: {e}")
                self.status_update.emit("Transcription complete (paste failed)")
        else:
            self.status_update.emit("Ready")

    def _clear_pending_metadata(self):
        """Clear all pending transcription metadata."""
        self._pending_audio_file = None
        self._pending_audio_duration = None
        self._pending_file_size = None
        self._transcription_start_time = None

    def cleanup(self):
        """Cleanup resources."""
        self._clear_pending_metadata()


__all__ = ["CompletionService"]
