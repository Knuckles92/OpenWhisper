"""SoundDevice audio recording."""
import sounddevice as sd
import wave
import threading
import logging
import math
import os
import tempfile
import numpy as np
import time
from datetime import datetime

from typing import BinaryIO, Callable, List, Optional, Tuple
from config import config
from services.wav_metadata import stamp_wav_origination

logger = logging.getLogger(__name__)

AudioLevelCallback = Callable[[float], None]

# Keep short dictations in memory, then transparently spill longer captures to
# an anonymous temporary file.  This bounds resident audio memory without doing
# an unbounded list append for multi-hour recordings.
SPOOL_MEMORY_LIMIT_BYTES = 8 * 1024 * 1024
COPY_BLOCK_BYTES = 1024 * 1024

# Adaptive post-roll keeps each callback block's level in a histogram of
# whole-dB bins over this range, so a percentile of an hours-long recording
# costs constant memory and one pass over 121 bins at the stop press.
LEVEL_HISTOGRAM_MIN_DB = -120
LEVEL_HISTOGRAM_MAX_DB = 0
# A dictation's noise floor is the level its quietest tenth of blocks stay
# under; its speech level is the one its loudest tenth reach.
NOISE_FLOOR_PERCENTILE = 10
SPEECH_LEVEL_PERCENTILE = 90


def block_level_db(audio: np.ndarray) -> float:
    """RMS level of one block of samples in dBFS, floored at the histogram."""
    samples = np.asarray(audio).reshape(-1)
    if samples.size == 0:
        return float(LEVEL_HISTOGRAM_MIN_DB)
    if np.issubdtype(samples.dtype, np.integer):
        full_scale = float(-np.iinfo(samples.dtype).min)
    else:
        full_scale = 1.0
    values = samples.astype(np.float64)
    mean_square = float(np.dot(values, values)) / values.size
    if mean_square <= 0.0:
        return float(LEVEL_HISTOGRAM_MIN_DB)
    level = 10.0 * math.log10(mean_square) - 20.0 * math.log10(full_scale)
    return max(float(LEVEL_HISTOGRAM_MIN_DB), level)


class PostRollGate:
    """Decides when the audio after a stop press has gone quiet.

    Every block captured before the stop goes into a level histogram.
    ``arm`` (at the stop press) freezes a quiet threshold from it: the
    recording's own noise floor plus POST_ROLL_QUIET_MARGIN_DB, but never
    lower than POST_ROLL_SPEECH_HEADROOM_DB under its speech level, so a mic
    whose floor is near digital silence does not count every faint rustle as
    speech. After that, ``observe`` returns True once blocks adding up to
    POST_ROLL_QUIET_MS in a row stay below the threshold. A recording whose
    floor cannot be trusted never ends early; it runs to the POST_ROLL_MS
    cap exactly as every recording did before.

    Not thread-safe: the recorder only touches it under its callback lock.
    """

    def __init__(self, rate: int):
        self.rate = rate
        self._histogram = np.zeros(
            LEVEL_HISTOGRAM_MAX_DB - LEVEL_HISTOGRAM_MIN_DB + 1, dtype=np.int64
        )
        self.frames_before_stop = 0
        self.frames_after_stop = 0
        self._quiet_frames = 0
        self._quiet_frames_needed = 0
        self.armed = False
        self.ended = False
        self.floor_db: Optional[float] = None
        self.speech_db: Optional[float] = None
        self.threshold_db: Optional[float] = None
        # Why this stop cannot end early; empty while the gate is usable.
        self.fallback = ""

    def observe(self, level_db: float, frames: int) -> bool:
        """Account one block; True exactly once, when the tail has gone quiet."""
        if not self.armed:
            index = int(round(level_db)) - LEVEL_HISTOGRAM_MIN_DB
            index = min(max(index, 0), len(self._histogram) - 1)
            self._histogram[index] += 1
            self.frames_before_stop += frames
            return False
        self.frames_after_stop += frames
        if self.threshold_db is None or self.ended:
            return False
        if level_db < self.threshold_db:
            self._quiet_frames += frames
        else:
            self._quiet_frames = 0
        if self._quiet_frames >= self._quiet_frames_needed:
            self.ended = True
            return True
        return False

    def arm(self) -> None:
        """Freeze the quiet threshold from what was captured before the stop."""
        if self.armed:
            return
        self.armed = True
        if not config.POST_ROLL_ADAPTIVE:
            self.fallback = "adaptive post-roll disabled"
            return
        captured_ms = self.frames_before_stop * 1000.0 / self.rate
        if captured_ms < config.POST_ROLL_FLOOR_MIN_MS:
            self.fallback = (
                f"only {captured_ms:.0f} ms before stop, too short for a noise floor"
            )
            return
        self.floor_db = self._percentile(NOISE_FLOOR_PERCENTILE)
        self.speech_db = self._percentile(SPEECH_LEVEL_PERCENTILE)
        snr_db = self.speech_db - self.floor_db
        if snr_db < config.POST_ROLL_MIN_SNR_DB:
            self.fallback = f"speech only {snr_db:.0f} dB above the noise floor"
            return
        self.threshold_db = max(
            self.floor_db + config.POST_ROLL_QUIET_MARGIN_DB,
            self.speech_db - config.POST_ROLL_SPEECH_HEADROOM_DB,
        )
        self._quiet_frames_needed = max(
            1, int(round(self.rate * config.POST_ROLL_QUIET_MS / 1000.0))
        )

    def describe(self) -> str:
        """One log-friendly phrase for how this stop's threshold was set."""
        if self.threshold_db is None:
            return self.fallback or "not armed"
        return (
            f"floor {self.floor_db:.0f} dBFS, speech {self.speech_db:.0f} dBFS, "
            f"quiet below {self.threshold_db:.0f} dBFS"
        )

    def _percentile(self, percent: float) -> float:
        counts = np.cumsum(self._histogram)
        rank = counts[-1] * percent / 100.0
        index = int(np.searchsorted(counts, rank, side="left"))
        return float(min(index, len(counts) - 1) + LEVEL_HISTOGRAM_MIN_DB)


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
        self._audio_spool: Optional[BinaryIO] = None
        self._recorded_bytes = 0
        self._recorded_sample_frames = 0
        self.stream: Optional[sd.InputStream] = None
        self.recording_thread: Optional[threading.Thread] = None
        self._stop_requested: bool = False
        # time.monotonic() values; zero while no stop is pending.
        self._stop_requested_at: float = 0.0
        self._post_roll_deadline: float = 0.0
        self._post_roll_gate = PostRollGate(config.SAMPLE_RATE)
        # Set by cancel_recording: the callback drops blocks until next start.
        self._capture_canceled = False
        self._stop_event = threading.Event()
        self._post_roll_end_event = threading.Event()
        self._post_roll_end_reason = ""
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
            # Fresh per-session events: a previous session's thread keeps its
            # own references, so it can never signal this one.
            self._recording_complete_event = threading.Event()
            self._stop_event = threading.Event()
            self._post_roll_end_event = threading.Event()
            self._post_roll_end_reason = ""

            self.clear_recording_data()
            with self._callback_lock:
                self._post_roll_gate = PostRollGate(self.rate)
                self._capture_canceled = False

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
            self._stop_requested_at = 0.0
            self._post_roll_deadline = 0.0

            self.recording_thread = threading.Thread(
                target=self._wait_for_stop,
                args=(
                    self._stop_event,
                    self._post_roll_end_event,
                    self._recording_complete_event,
                ),
                daemon=True,
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
        """Request stop; capture continues until the tail is quiet or the cap.

        A repeat request during post-roll (cancel right after stop) keeps the
        first request's cap instead of extending it.
        """
        if not self.is_recording:
            logger.warning("No recording in progress")
            return False

        try:
            with self._callback_lock:
                if self._stop_requested:
                    logger.debug("Recording stop already requested; post-roll continues")
                    return True
                self._post_roll_gate.arm()
                self._stop_requested = True
                self._stop_requested_at = time.monotonic()
                self._post_roll_deadline = (
                    self._stop_requested_at + config.POST_ROLL_MS / 1000.0
                )
            self._stop_event.set()

            logger.info("Recording stop requested, post-roll continuing in background")
            return True

        except Exception as e:
            logger.error(f"Failed to stop recording: {e}")
            return False

    def _end_post_roll(self, reason: str) -> None:
        """Wake the recording thread to close the stream before the cap."""
        if not self._post_roll_end_event.is_set():
            self._post_roll_end_reason = reason
            self._post_roll_end_event.set()

    def wait_for_stop_completion(self, timeout: float = None) -> bool:
        """Wait for post-roll capture, using the cap plus its grace by default."""
        thread = self.recording_thread
        if not thread or not thread.is_alive():
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
                if self._capture_canceled:
                    return  # the stream is only closing; keep nothing more
                audio_copy = indata.copy()
                payload = audio_copy.tobytes()
                if self._audio_spool is None:
                    self._audio_spool = tempfile.SpooledTemporaryFile(
                        max_size=SPOOL_MEMORY_LIMIT_BYTES,
                        mode="w+b",
                    )
                self._audio_spool.write(payload)
                self._recorded_bytes += len(payload)
                block_frames = int(audio_copy.shape[0] if audio_copy.ndim else frames)
                self._recorded_sample_frames += block_frames

                if self._post_roll_gate.observe(block_level_db(audio_copy), block_frames):
                    self._end_post_roll("quiet")

                if self.audio_level_callback:
                    self._calculate_and_report_level(audio_copy)

                if self.streaming_callback:
                    try:
                        self.streaming_callback(audio_copy.copy())
                    except Exception as stream_err:
                        logger.debug(f"Streaming callback error: {stream_err}")

        except Exception as e:
            logger.error(f"Error in audio callback: {e}")

    def _wait_for_stop(
        self,
        stop_event: threading.Event,
        post_roll_end_event: threading.Event,
        complete_event: threading.Event,
    ):
        """Keep the open stream alive until stop plus post-roll complete.

        Sleeps on events rather than polling, so the stream closes as soon as
        the callback reports a quiet tail instead of up to a poll later.
        """
        reason = "cap"
        try:
            logger.info("Audio stream started")
            stop_event.wait()
            remaining = self._post_roll_deadline - time.monotonic()
            if post_roll_end_event.wait(max(0.0, remaining)):
                reason = self._post_roll_end_reason or "quiet"
        except Exception as e:
            logger.error(f"Error while recording audio: {e}")
        finally:
            if self.stream:
                try:
                    # abort() drops what PortAudio still buffers instead of
                    # draining it: 7 ms median against stop()'s 33 ms (56 ms
                    # max) on MME, September 2026, and neither delivered one
                    # more callback. That audio is post-roll either way.
                    self.stream.abort()
                    self.stream.close()
                    logger.info("Audio stream stopped and closed")
                except Exception as e:
                    logger.error(f"Error closing audio stream: {e}")
                self.stream = None
            self._log_post_roll(reason)
            self._stop_requested = False
            self._stop_requested_at = 0.0
            self._post_roll_deadline = 0.0
            self.recording_thread = None
            self.is_recording = False
            complete_event.set()

    def _log_post_roll(self, reason: str) -> None:
        """One INFO line per stop, so log audits can measure the post-roll."""
        if not self._stop_requested_at:
            return
        elapsed_ms = (time.monotonic() - self._stop_requested_at) * 1000.0
        with self._callback_lock:
            gate = self._post_roll_gate
            audio_ms = gate.frames_after_stop * 1000.0 / self.rate
            detail = gate.describe()
        logger.info(
            "Post-roll ended by %s after %.0f ms (%.0f ms of audio after stop; %s)",
            reason,
            elapsed_ms,
            audio_ms,
            detail,
        )

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
        """Atomically stream captured PCM into a WAV file."""
        filename = filename or self.output_file

        # Trailing silence prevents some ASR models from dropping the last word.
        padding_bytes = b''
        if config.END_PADDING_MS > 0:
            padding_samples = int(self.rate * (config.END_PADDING_MS / 1000.0))
            if padding_samples > 0:
                silence_shape = (padding_samples, self.channels) if self.channels > 1 else (padding_samples,)
                padding_bytes = np.zeros(silence_shape, dtype=self.dtype).tobytes()

        try:
            directory = os.path.dirname(os.path.abspath(filename)) or os.curdir
            temp_fd, temp_path = tempfile.mkstemp(suffix='.wav', dir=directory)

            try:
                # The callback may still be completing post-roll after a timeout.
                # Holding its lock produces one coherent snapshot without ever
                # materializing the whole recording as a bytes object.
                with self._callback_lock:
                    if self._audio_spool is None or self._recorded_bytes <= 0:
                        os.close(temp_fd)
                        temp_fd = -1
                        os.remove(temp_path)
                        logger.warning("No audio data to save")
                        return False
                    recorded_bytes = self._recorded_bytes
                    recorded_sample_frames = self._recorded_sample_frames
                    self._audio_spool.flush()
                    self._audio_spool.seek(0)
                    with os.fdopen(temp_fd, 'wb') as temp_file:
                        temp_fd = -1
                        with wave.open(temp_file, 'wb') as wf:
                            wf.setnchannels(self.channels)
                            wf.setsampwidth(np.dtype(self.dtype).itemsize)
                            wf.setframerate(self.rate)
                            while True:
                                block = self._audio_spool.read(COPY_BLOCK_BYTES)
                                if not block:
                                    break
                                wf.writeframesraw(block)
                            if padding_bytes:
                                wf.writeframesraw(padding_bytes)
                    self._audio_spool.seek(0, os.SEEK_END)

                os.replace(temp_path, filename)
                temp_path = ""
                try:
                    stamp_wav_origination(filename, datetime.now())
                except Exception:
                    logger.warning(
                        "Failed to stamp WAV origination metadata on %s",
                        filename,
                        exc_info=True,
                    )

                total_bytes = recorded_bytes + len(padding_bytes)
                if padding_bytes:
                    logger.info(f"Appended {config.END_PADDING_MS}ms of silence to protect the tail of the recording")
                logger.info(
                    f"Audio saved to {filename} at {time.strftime('%Y-%m-%d %H:%M:%S')} "
                    f"- {recorded_sample_frames} sample frames, {total_bytes} bytes, "
                    f"{self.get_recording_duration():.2f}s"
                )
                return True

            except Exception:
                if temp_fd >= 0:
                    os.close(temp_fd)
                    temp_fd = -1
                if temp_path and os.path.exists(temp_path):
                    os.remove(temp_path)
                raise

        except Exception as e:
            logger.error(f"Failed to save audio to {filename}: {e}")
            return False

    def get_recording_duration(self) -> float:
        """Return captured duration in seconds."""
        with self._callback_lock:
            return self._recorded_sample_frames / self.rate

    def has_recording_data(self) -> bool:
        """Return whether audio frames have been captured."""
        with self._callback_lock:
            return self._recorded_bytes > 0

    def read_recorded_bytes(self, offset: int) -> Optional[bytes]:
        """Return the PCM captured from byte ``offset`` on, as the WAV will hold it.

        Lets a reader follow a recording in progress (incremental dictation)
        without the streaming callback, which the live preview owns. The
        spool's position is where the callback writes next, so it is restored
        before the lock is released. Returns None when the capture no longer
        reaches ``offset``: it was cleared or restarted since.
        """
        with self._callback_lock:
            if offset < 0 or offset > self._recorded_bytes:
                return None
            if offset == self._recorded_bytes:
                return b""
            spool = self._audio_spool
            position = spool.tell()
            try:
                spool.seek(offset)
                return spool.read(self._recorded_bytes - offset)
            finally:
                spool.seek(position)

    def cancel_recording(self) -> None:
        """Throw the capture away: end post-roll now and keep nothing after it.

        A bare clear lets the stream refill a fresh spool until post-roll
        ends, so a stop pressed in that window (the record hotkey in toggle
        mode, or a push-and-hold release) would save and paste audio the user
        had just discarded.
        """
        with self._callback_lock:
            self._capture_canceled = True
        if self.is_recording:
            self.stop_recording()
            self._end_post_roll("cancel")
        self.clear_recording_data()

    @property
    def capture_canceled(self) -> bool:
        """True from ``cancel_recording`` until the next recording starts."""
        return self._capture_canceled

    def clear_recording_data(self):
        """Clear the recorded audio data."""
        with self._callback_lock:
            old_bytes = self._recorded_bytes
            spool, self._audio_spool = self._audio_spool, None
            self._recorded_bytes = 0
            self._recorded_sample_frames = 0
            if spool is not None:
                spool.close()

        logger.info(f"Cleared recording data. Old byte count: {old_bytes}")

    def cleanup(self):
        """Clean up audio resources."""
        try:
            if self.is_recording:
                self.stop_recording()
                # The data is cleared below, so nothing needs the post-roll.
                self._end_post_roll("cleanup")
                # The thread clears recording_thread as it exits; keep a local.
                thread = self.recording_thread
                if thread and thread.is_alive():
                    thread.join(timeout=0.5)
                    if thread.is_alive():
                        logger.warning("Recording thread did not finish during cleanup timeout")

            if self.stream:
                try:
                    self.stream.stop()
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None

            self.clear_recording_data()

            logger.info("Audio recorder cleaned up")

        except Exception as e:
            logger.debug(f"Error during audio recorder cleanup: {e}")
