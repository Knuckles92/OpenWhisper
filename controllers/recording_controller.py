"""
Recording controller for managing audio recording lifecycle.
"""

import logging
import os
from typing import Optional, Callable

from PyQt6.QtCore import QObject, pyqtSignal

from config import config
from recorder import AudioRecorder


class RecordingController(QObject):
    """Manages audio recording lifecycle and audio level streaming."""

    # Signals
    recording_state_changed = pyqtSignal(bool)  # True = started, False = stopped
    status_update = pyqtSignal(str)
    audio_levels_updated = pyqtSignal(list)  # Audio levels for waveform display
    recording_ready = pyqtSignal(str, float, int)  # audio_file, duration, file_size

    def __init__(self, recorder: Optional[AudioRecorder] = None):
        """Initialize recording controller.

        Args:
            recorder: AudioRecorder instance. If None, creates a new one.
        """
        super().__init__()
        self.recorder = recorder or AudioRecorder()
        self._setup_audio_level_callback()

    def _setup_audio_level_callback(self):
        """Setup audio level callback for waveform display."""
        def audio_level_callback(level: float):
            # Convert single level to list for compatibility with overlay
            levels = [level] * 20
            self.audio_levels_updated.emit(levels)

        self.recorder.set_audio_level_callback(audio_level_callback)

    @property
    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self.recorder.is_recording

    def start_recording(self) -> bool:
        """Start audio recording.

        Returns:
            True if recording started successfully.
        """
        if self.recorder.start_recording():
            logging.info("Recording started")
            self.recording_state_changed.emit(True)
            self.status_update.emit("Recording...")
            return True
        else:
            self.status_update.emit("Failed to start recording")
            return False

    def stop_recording(self) -> bool:
        """Stop audio recording and prepare file for transcription.

        Returns:
            True if recording stopped and saved successfully.
        """
        if not self.recorder.stop_recording():
            self.status_update.emit("Failed to stop recording")
            return False

        self.recording_state_changed.emit(False)
        self.status_update.emit("Processing...")

        # Ensure the recorder thread has flushed the post-roll before saving
        if not self.recorder.wait_for_stop_completion():
            logging.warning("Proceeding without confirmed post-roll completion; tail of recording may be short")

        # Check if we have recording data
        if not self.recorder.has_recording_data():
            logging.error("No recording data available")
            self.status_update.emit("No audio data recorded")
            return False

        if not self.recorder.save_recording():
            logging.error("Failed to save recording")
            self.status_update.emit("Failed to save audio file")
            return False

        audio_file = config.RECORDED_AUDIO_FILE
        if not os.path.exists(audio_file):
            logging.error(f"Audio file not found: {audio_file}")
            self.status_update.emit("Audio file not created")
            return False

        file_size = os.path.getsize(audio_file)
        logging.info(f"Audio file size: {file_size} bytes")

        if file_size < 100:
            logging.error(f"Audio file too small: {file_size} bytes")
            self.status_update.emit("Audio file is empty or corrupted")
            return False

        # Get recording duration and emit that recording is ready
        duration = self.recorder.get_recording_duration()
        logging.info(f"Recording ready. Duration: {duration:.2f}s")
        self.recording_ready.emit(audio_file, duration, file_size)
        return True

    def toggle_recording(self):
        """Toggle between starting and stopping recording."""
        logging.info(f"Toggle recording. Current state: {self.recorder.is_recording}")
        if not self.recorder.is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def cancel_recording(self) -> bool:
        """Cancel current recording (discards data).

        Returns:
            True if recording was cancelled, False if nothing to cancel.
        """
        if self.recorder.is_recording:
            self.recording_state_changed.emit(False)
            self.recorder.stop_recording()
            self.recorder.clear_recording_data()
            self.status_update.emit("Recording cancelled")
            logging.info("Recording cancelled")
            return True
        return False

    def get_recording_duration(self) -> float:
        """Get the duration of the current/last recording.

        Returns:
            Duration in seconds.
        """
        return self.recorder.get_recording_duration()

    def cleanup(self):
        """Cleanup resources."""
        try:
            if self.recorder:
                self.recorder.cleanup()
        except Exception as e:
            logging.debug(f"Error during recorder cleanup: {e}")
