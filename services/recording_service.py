"""
Recording service for managing audio recording lifecycle.

Handles audio capture using SoundDevice, real-time level monitoring,
and WAV file output.
"""

import logging
import os
import tempfile
import threading
import time
import wave
from typing import List, Optional, Callable

import numpy as np
import sounddevice as sd

from PyQt6.QtCore import QObject, pyqtSignal

from config import config


class RecordingService(QObject):
    """Manages audio recording lifecycle, capture, and level streaming."""

    # Signals
    recording_state_changed = pyqtSignal(bool)  # True = started, False = stopped
    status_update = pyqtSignal(str)
    audio_levels_updated = pyqtSignal(list)  # Audio levels for waveform display
    recording_ready = pyqtSignal(str, float, int)  # audio_file, duration, file_size

    def __init__(self):
        """Initialize recording service."""
        super().__init__()

        # Recording state
        self._is_recording = False
        self._frames: List[bytes] = []
        self._stream: Optional[sd.InputStream] = None
        self._recording_thread: Optional[threading.Thread] = None
        self._stop_requested: bool = False
        self._post_roll_until: float = 0.0
        self._recording_complete_event = threading.Event()

        # Audio settings from config
        self._chunk = config.CHUNK_SIZE
        self._dtype = config.AUDIO_FORMAT
        self._channels = config.CHANNELS
        self._rate = config.SAMPLE_RATE

        # Audio level calculation
        self._current_level = 0.0
        self._level_smoothing = config.WAVEFORM_LEVEL_SMOOTHING

        # Thread safety
        self._callback_lock = threading.Lock()

        logging.info("Recording service initialized")

    @property
    def is_recording(self) -> bool:
        """Check if currently recording."""
        return self._is_recording

    def start_recording(self) -> bool:
        """Start audio recording.

        Returns:
            True if recording started successfully.
        """
        if self._is_recording:
            logging.warning("Recording already in progress")
            return False

        try:
            # Reset completion signal for this session
            self._recording_complete_event = threading.Event()

            # Clear any old recording data
            self._frames = []
            logging.info("Cleared recording frames")

            # Delete old audio file if it exists
            if os.path.exists(config.RECORDED_AUDIO_FILE):
                try:
                    os.remove(config.RECORDED_AUDIO_FILE)
                    logging.info(f"Deleted old audio file: {config.RECORDED_AUDIO_FILE}")
                except Exception as e:
                    logging.warning(f"Could not delete old audio file: {e}")

            self._is_recording = True
            self._stop_requested = False
            self._post_roll_until = 0.0

            # Start recording in a separate thread
            self._recording_thread = threading.Thread(target=self._record_audio, daemon=True)
            self._recording_thread.start()

            logging.info("Recording started")
            self.recording_state_changed.emit(True)
            self.status_update.emit("Recording...")
            return True

        except Exception as e:
            logging.error(f"Failed to start recording: {e}")
            self._is_recording = False
            self.status_update.emit("Failed to start recording")
            return False

    def stop_recording(self) -> bool:
        """Stop audio recording and prepare file for transcription.

        Returns:
            True if recording stopped and saved successfully.
        """
        if not self._is_recording:
            logging.warning("No recording in progress")
            self.status_update.emit("Failed to stop recording")
            return False

        try:
            # Request stop and allow a short post-roll to capture trailing speech
            self._stop_requested = True
            self._post_roll_until = time.time() + (config.POST_ROLL_MS / 1000.0)

            self.recording_state_changed.emit(False)
            self.status_update.emit("Processing...")

            # Wait for the recorder thread to finish post-roll
            if not self._wait_for_stop_completion():
                logging.warning("Proceeding without confirmed post-roll completion")

            # Check if we have recording data
            if not self._frames:
                logging.error("No recording data available")
                self.status_update.emit("No audio data recorded")
                return False

            if not self._save_recording():
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
            duration = self._get_recording_duration()
            logging.info(f"Recording ready. Duration: {duration:.2f}s")
            self.recording_ready.emit(audio_file, duration, file_size)
            return True

        except Exception as e:
            logging.error(f"Failed to stop recording: {e}")
            return False

    def toggle_recording(self):
        """Toggle between starting and stopping recording."""
        logging.info(f"Toggle recording. Current state: {self._is_recording}")
        if not self._is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def cancel_recording(self) -> bool:
        """Cancel current recording (discards data).

        Returns:
            True if recording was cancelled, False if nothing to cancel.
        """
        if self._is_recording:
            self.recording_state_changed.emit(False)
            self._stop_requested = True
            self._post_roll_until = 0.0  # No post-roll for cancel
            self._frames = []
            self.status_update.emit("Recording cancelled")
            logging.info("Recording cancelled")
            return True
        return False

    def get_recording_duration(self) -> float:
        """Get the duration of the current/last recording.

        Returns:
            Duration in seconds.
        """
        return self._get_recording_duration()

    # --- Internal audio recording methods ---

    def _wait_for_stop_completion(self, timeout: float = None) -> bool:
        """Wait for the recorder thread to finish post-roll capture.

        Args:
            timeout: Optional timeout in seconds.

        Returns:
            True if the recorder finished within the timeout.
        """
        if not self._recording_thread or not self._recording_thread.is_alive():
            return True

        default_timeout = (config.POST_ROLL_MS + config.POST_ROLL_FINALIZE_GRACE_MS) / 1000.0
        wait_timeout = timeout if timeout is not None else default_timeout

        finished = self._recording_complete_event.wait(wait_timeout)
        if not finished:
            logging.warning("Recording thread did not finish during post-roll wait")
        return finished

    def _record_audio(self):
        """Record audio data in a separate thread until recording is stopped."""
        try:
            self._stream = sd.InputStream(
                samplerate=self._rate,
                channels=self._channels,
                dtype=self._dtype,
                blocksize=self._chunk,
                callback=self._audio_callback
            )

            self._stream.start()
            logging.info("Audio stream started")

            # Wait until stop is requested and post-roll window has elapsed
            while True:
                time.sleep(0.01)
                if self._stop_requested and time.time() >= self._post_roll_until:
                    break

        except Exception as e:
            logging.error(f"Error opening audio stream: {e}")
        finally:
            if self._stream:
                try:
                    self._stream.stop()
                    self._stream.close()
                    logging.info("Audio stream stopped and closed")
                except Exception as e:
                    logging.error(f"Error closing audio stream: {e}")

            self._is_recording = False
            self._stop_requested = False
            self._post_roll_until = 0.0
            self._recording_thread = None
            self._recording_complete_event.set()

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status):
        """Callback function for sounddevice to process incoming audio data."""
        if status:
            logging.warning(f"Audio stream status: {status}")

        try:
            with self._callback_lock:
                self._frames.append(indata.copy().tobytes())
                self._calculate_and_emit_level(indata.copy())
        except Exception as e:
            logging.error(f"Error in audio callback: {e}")

    def _calculate_and_emit_level(self, audio_data: np.ndarray):
        """Calculate audio level and emit via signal."""
        try:
            if len(audio_data) > 0:
                if self._dtype == np.int16:
                    rms_level = np.sqrt(np.mean(audio_data.astype(np.float64) ** 2)) / 32767.0
                elif self._dtype == np.float32:
                    rms_level = np.sqrt(np.mean(audio_data ** 2))
                else:
                    return

                # Apply smoothing
                self._current_level = (
                    self._level_smoothing * self._current_level +
                    (1.0 - self._level_smoothing) * rms_level
                )
                self._current_level = max(0.0, min(1.0, self._current_level))

                # Emit as list for overlay compatibility
                levels = [self._current_level] * 20
                self.audio_levels_updated.emit(levels)

        except Exception as e:
            logging.debug(f"Error calculating audio level: {e}")

    def _save_recording(self, filename: str = None) -> bool:
        """Save the recorded audio frames to a WAV file.

        Args:
            filename: Output filename. Uses config default if None.

        Returns:
            True if saved successfully.
        """
        if not self._frames:
            logging.warning("No audio data to save")
            return False

        filename = filename or config.RECORDED_AUDIO_FILE

        with self._callback_lock:
            frames_to_write = list(self._frames)

        frame_count = len(frames_to_write)
        total_bytes = sum(len(frame) for frame in frames_to_write)

        # Add trailing silence to reduce ASR truncation
        padding_bytes = b''
        if config.END_PADDING_MS > 0:
            padding_samples = int(self._rate * (config.END_PADDING_MS / 1000.0))
            if padding_samples > 0:
                silence_shape = (padding_samples, self._channels) if self._channels > 1 else (padding_samples,)
                padding_bytes = np.zeros(silence_shape, dtype=self._dtype).tobytes()
                total_bytes += len(padding_bytes)

        try:
            temp_fd, temp_path = tempfile.mkstemp(suffix='.wav', dir=os.path.dirname(filename) or '.')

            try:
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    with wave.open(temp_file, 'wb') as wf:
                        wf.setnchannels(self._channels)
                        wf.setsampwidth(np.dtype(self._dtype).itemsize)
                        wf.setframerate(self._rate)
                        wf.writeframes(b''.join(frames_to_write) + padding_bytes)

                if os.path.exists(filename):
                    os.remove(filename)
                os.rename(temp_path, filename)

                if padding_bytes:
                    logging.info(f"Appended {config.END_PADDING_MS}ms of silence")
                logging.info(f"Audio saved: {frame_count} frames, {total_bytes} bytes")
                return True

            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise

        except Exception as e:
            logging.error(f"Failed to save audio to {filename}: {e}")
            return False

    def _get_recording_duration(self) -> float:
        """Get the duration of the current recording in seconds."""
        if not self._frames:
            return 0.0
        total_frames = len(self._frames) * self._chunk
        return total_frames / self._rate

    def cleanup(self):
        """Cleanup resources."""
        try:
            if self._is_recording:
                self._stop_requested = True
                self._post_roll_until = 0.0

                if self._recording_thread and self._recording_thread.is_alive():
                    self._recording_thread.join(timeout=0.5)
                    if self._recording_thread.is_alive():
                        logging.warning("Recording thread did not finish during cleanup")

            if self._stream:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None

            logging.info("Recording service cleaned up")

        except Exception as e:
            logging.debug(f"Error during recording service cleanup: {e}")


__all__ = ["RecordingService"]
