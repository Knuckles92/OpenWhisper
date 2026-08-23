"""SoundDevice audio recording."""
import sounddevice as sd
import wave
import threading
import logging
import numpy as np
import time

from typing import Callable, List, Optional, Tuple
from config import config

logger = logging.getLogger(__name__)

AudioLevelCallback = Callable[[float], None]


class AudioRecorder:
    """Handles audio recording using SoundDevice."""

    @staticmethod
    def get_input_devices() -> List[Tuple[int, str]]:
        """Return ``(device_id, name)`` pairs for audio input devices."""
        devices = []
        try:
            all_devices = sd.query_devices()
            for i, device in enumerate(all_devices):
                if device['max_input_channels'] > 0:
                    devices.append((i, device['name']))
        except Exception as e:
            logger.error(f"Failed to enumerate audio devices: {e}")
        return devices

    def __init__(
        self,
        device_id: Optional[int] = None,
        output_file: Optional[str] = None,
    ):
        """Use a private output path for secondary recorders to avoid clobbering."""
        self.device_id = device_id
        self.output_file = output_file or config.RECORDED_AUDIO_FILE
        self.is_recording = False
        self.frames: List[bytes] = []
        self.stream: Optional[sd.InputStream] = None
        self.recording_thread: Optional[threading.Thread] = None
        self._stop_requested: bool = False
        self._post_roll_until: float = 0.0
        self._recording_complete_event = threading.Event()
        self.last_start_error: Optional[str] = None

        self.chunk = config.CHUNK_SIZE
        self.dtype = config.AUDIO_FORMAT
        self.channels = config.CHANNELS
        self.rate = config.SAMPLE_RATE

        self.audio_level_callback: Optional[AudioLevelCallback] = None

        self.streaming_callback: Optional[Callable[[np.ndarray], None]] = None

        self._current_audio_level = 0.0
        self._level_smoothing = config.WAVEFORM_LEVEL_SMOOTHING

        self._callback_lock = threading.Lock()

        logger.info("Audio recorder initialized")

    def set_audio_level_callback(self, callback: AudioLevelCallback):
        """Set the callback receiving normalized real-time audio levels."""
        self.audio_level_callback = callback

    def set_streaming_callback(self, callback: Callable[[np.ndarray], None]):
        """Set the callback receiving audio chunks for streaming transcription."""
        self.streaming_callback = callback

    def start_recording(self) -> bool:
        """Open the input stream before marking the session as recording."""
        if self.is_recording:
            logger.warning("Recording already in progress")
            return False

        self.last_start_error = None
        try:
            self._recording_complete_event = threading.Event()

            self.clear_recording_data()

            import os
            if os.path.exists(self.output_file):
                try:
                    os.remove(self.output_file)
                    logger.info(f"Deleted old audio file: {self.output_file}")
                except Exception as e:
                    logger.warning(f"Could not delete old audio file: {e}")

            self.stream = sd.InputStream(
                device=self.device_id,
                samplerate=self.rate,
                channels=self.channels,
                dtype=self.dtype,
                blocksize=self.chunk,
                callback=self._audio_callback,
            )
            self.stream.start()

            self.is_recording = True
            self._stop_requested = False
            self._post_roll_until = 0.0

            self.recording_thread = threading.Thread(
                target=self._wait_for_stop, daemon=True
            )
            self.recording_thread.start()

            logger.info("Recording started - audio stream open")
            return True

        except Exception as e:
            self.last_start_error = self._format_start_error(e)
            logger.error(f"Failed to start recording: {e}")
            self._unwind_failed_stream()
            self.is_recording = False
            return False

    @staticmethod
    def _format_start_error(exc: Exception) -> str:
        """Turn a PortAudio/sounddevice exception into a user-facing reason."""
        message = str(exc)
        lowered = message.lower()
        if any(
            token in lowered
            for token in (
                "querying device",
                "no such device",
                "invalid device",
                "device unavailable",
                "no default input",
                "portaudio",
            )
        ):
            return "No audio device available"
        return message or "Could not open the audio stream"

    def _unwind_failed_stream(self) -> None:
        """Close a stream that failed during start and drop the reference."""
        if not self.stream:
            return
        try:
            self.stream.stop()
        except Exception:
            pass
        try:
            self.stream.close()
        except Exception:
            pass
        self.stream = None

    def stop_recording(self) -> bool:
        """Request stop after the configured post-roll window."""
        if not self.is_recording:
            logger.warning("No recording in progress")
            return False

        try:
            self._stop_requested = True
            self._post_roll_until = time.time() + (config.POST_ROLL_MS / 1000.0)

            logger.info("Recording stop requested, post-roll continuing in background")
            return True

        except Exception as e:
            logger.error(f"Failed to stop recording: {e}")
            return False

    def wait_for_stop_completion(self, timeout: float = None) -> bool:
        """Wait for post-roll capture, using the configured grace by default."""
        if not self.recording_thread or not self.recording_thread.is_alive():
            return True

        default_timeout = (config.POST_ROLL_MS + config.POST_ROLL_FINALIZE_GRACE_MS) / 1000.0
        wait_timeout = timeout if timeout is not None else default_timeout

        finished = self._recording_complete_event.wait(wait_timeout)
        if not finished:
            logger.warning("Recording thread did not finish during post-roll wait; proceeding with available audio")
        return finished

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status):
        if status:
            logger.warning(f"Audio stream status: {status}")

        try:
            with self._callback_lock:
                self.frames.append(indata.copy().tobytes())

                if self.audio_level_callback:
                    self._calculate_and_report_level(indata.copy())

                if self.streaming_callback:
                    try:
                        self.streaming_callback(indata.copy())
                    except Exception as stream_err:
                        logger.debug(f"Streaming callback error: {stream_err}")

        except Exception as e:
            logger.error(f"Error in audio callback: {e}")

    def _wait_for_stop(self):
        """Keep the open stream alive until stop plus post-roll complete."""
        try:
            logger.info("Audio stream started")
            while True:
                time.sleep(0.01)
                if self._stop_requested and time.time() >= self._post_roll_until:
                    break
        except Exception as e:
            logger.error(f"Error while recording audio: {e}")
        finally:
            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                    logger.info("Audio stream stopped and closed")
                except Exception as e:
                    logger.error(f"Error closing audio stream: {e}")
                self.stream = None
            self.is_recording = False
            self._stop_requested = False
            self._post_roll_until = 0.0
            self.recording_thread = None
            self._recording_complete_event.set()

    def _calculate_and_report_level(self, audio_data: np.ndarray):
        try:
            if len(audio_data) > 0:
                if self.dtype == np.int16:
                    rms_level = np.sqrt(np.mean(audio_data.astype(np.float64) ** 2)) / 32767.0
                elif self.dtype == np.float32:
                    rms_level = np.sqrt(np.mean(audio_data ** 2))
                else:
                    return

                self._current_audio_level = (
                    self._level_smoothing * self._current_audio_level +
                    (1.0 - self._level_smoothing) * rms_level
                )

                self._current_audio_level = max(0.0, min(1.0, self._current_audio_level))

                if self.audio_level_callback:
                    self.audio_level_callback(self._current_audio_level)

        except Exception as e:
            logger.debug(f"Error calculating audio level: {e}")

    def save_recording(self, filename: str = None) -> bool:
        """Atomically save captured frames to a WAV file."""
        if not self.frames:
            logger.warning("No audio data to save")
            return False

        filename = filename or self.output_file

        # The audio callback may append frames while post-roll is finishing.
        with self._callback_lock:
            frames_to_write = list(self.frames)

        frame_count = len(frames_to_write)
        total_bytes = sum(len(frame) for frame in frames_to_write)

        # Trailing silence prevents some ASR models from dropping the last word.
        padding_bytes = b''
        if config.END_PADDING_MS > 0:
            padding_samples = int(self.rate * (config.END_PADDING_MS / 1000.0))
            if padding_samples > 0:
                silence_shape = (padding_samples, self.channels) if self.channels > 1 else (padding_samples,)
                padding_bytes = np.zeros(silence_shape, dtype=self.dtype).tobytes()
                total_bytes += len(padding_bytes)

        try:
            import tempfile
            import os
            temp_fd, temp_path = tempfile.mkstemp(suffix='.wav', dir=os.path.dirname(filename))

            try:
                with os.fdopen(temp_fd, 'wb') as temp_file:
                    with wave.open(temp_file, 'wb') as wf:
                        wf.setnchannels(self.channels)
                        wf.setsampwidth(np.dtype(self.dtype).itemsize)
                        wf.setframerate(self.rate)
                        wf.writeframes(b''.join(frames_to_write) + padding_bytes)

                if os.path.exists(filename):
                    os.remove(filename)
                os.rename(temp_path, filename)

                import time
                if padding_bytes:
                    logger.info(f"Appended {config.END_PADDING_MS}ms of silence to protect the tail of the recording")
                logger.info(f"Audio saved to {filename} at {time.strftime('%Y-%m-%d %H:%M:%S')} - {frame_count} frames, {total_bytes} bytes, {self.get_recording_duration():.2f}s")
                return True

            except Exception as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise

        except Exception as e:
            logger.error(f"Failed to save audio to {filename}: {e}")
            return False

    def get_recording_duration(self) -> float:
        """Return captured duration in seconds."""
        if not self.frames:
            return 0.0

        total_frames = len(self.frames) * self.chunk
        return total_frames / self.rate

    def has_recording_data(self) -> bool:
        """Return whether audio frames have been captured."""
        return bool(self.frames)

    def clear_recording_data(self):
        """Clear the recorded audio data."""
        with self._callback_lock:
            old_frame_count = len(self.frames)
            self.frames = []

        logger.info(f"Cleared recording data. Old frame count: {old_frame_count}")

    def cleanup(self):
        """Clean up audio resources."""
        try:
            if self.is_recording:
                self.stop_recording()
                if self.recording_thread and self.recording_thread.is_alive():
                    self.recording_thread.join(timeout=0.5)
                    if self.recording_thread.is_alive():
                        logger.warning("Recording thread did not finish during cleanup timeout")

            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None

            logger.info("Audio recorder cleaned up")

        except Exception as e:
            logger.debug(f"Error during audio recorder cleanup: {e}")
