"""
Real-time streaming transcription using faster-whisper with queue-based architecture.

Provides a live preview while recording by transcribing short incremental audio
chunks (with a small overlap for word boundaries). Preview text is approximate;
final quality still comes from the full-file transcription after stop.
"""
import queue
import threading
import logging
import time
import numpy as np
from scipy import signal
from typing import Callable, List, Optional
from config import config

logger = logging.getLogger(__name__)


def append_preview_text(existing: str, chunk_text: str) -> str:
    """Append a new chunk's text to the accumulated preview.

    Args:
        existing: Preview text so far.
        chunk_text: Newly transcribed chunk text.

    Returns:
        Combined preview string.
    """
    chunk_text = (chunk_text or "").strip()
    if not chunk_text:
        return existing or ""
    if not existing:
        return chunk_text
    return f"{existing} {chunk_text}".strip()


class StreamingTranscriber:
    """Manages real-time streaming transcription using a worker thread."""

    def __init__(
        self,
        backend,
        chunk_duration_sec: float = 3.0,
        overlap_sec: float = None,
    ):
        """Initialize the streaming transcriber.

        Args:
            backend: LocalWhisperBackend instance with loaded model
            chunk_duration_sec: Duration of new audio to accumulate before transcribing
            overlap_sec: Seconds of previous audio to include for word-boundary context
        """
        self.backend = backend
        self.chunk_duration_sec = chunk_duration_sec
        self.overlap_sec = (
            overlap_sec
            if overlap_sec is not None
            else getattr(config, "STREAMING_OVERLAP_SEC", 0.75)
        )

        self.audio_queue: queue.Queue = queue.Queue(maxsize=config.STREAMING_QUEUE_SIZE)

        self.worker_thread: Optional[threading.Thread] = None
        self.is_streaming = False
        self._stop_requested = False

        self.preview_text: str = ""
        self._overlap_tail: Optional[np.ndarray] = None

        self.sample_rate = 0
        self.callback: Optional[Callable[[str, bool], None]] = None

        self._chunk_count = 0
        self._slow_chunks = 0
        self._last_warning_time = 0

        logger.info(
            "StreamingTranscriber initialized "
            f"(chunk_duration={chunk_duration_sec}s, overlap={self.overlap_sec}s)"
        )

    def start_streaming(self, sample_rate: int, callback: Callable[[str, bool], None]):
        """Start the streaming worker thread.

        Args:
            sample_rate: Audio sample rate (Hz)
            callback: Function(text, is_final) called with preview results
        """
        if self.is_streaming:
            logger.warning("Streaming already active")
            return

        self.sample_rate = sample_rate
        self.callback = callback
        self.is_streaming = True
        self._stop_requested = False
        self.preview_text = ""
        self._overlap_tail = None
        self._chunk_count = 0
        self._slow_chunks = 0

        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

        logger.info("Streaming transcription started")

    def feed_audio(self, audio_chunk: np.ndarray):
        """Feed audio chunk to transcription queue (called from recorder callback).

        Args:
            audio_chunk: NumPy array of audio data (int16 or float32)
        """
        if not self.is_streaming:
            return

        try:
            self.audio_queue.put_nowait(audio_chunk.copy())
        except queue.Full:
            logger.debug("Audio queue full, dropping chunk (transcription can't keep up)")

    def stop_streaming(self) -> str:
        """Stop streaming and return the accumulated preview text.

        Returns:
            Combined preview transcription text from all chunks
        """
        if not self.is_streaming:
            return ""

        logger.info("Stopping streaming transcription...")
        self._stop_requested = True

        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5.0)
            if self.worker_thread.is_alive():
                logger.warning("Worker thread did not finish in time")

        self.is_streaming = False
        self.worker_thread = None

        final_text = self.preview_text.strip()
        self._overlap_tail = None

        logger.info(
            f"Streaming stopped. Incremental cycles: {self._chunk_count}, "
            f"Final length: {len(final_text)} chars"
        )

        return final_text

    def _worker_loop(self):
        """Worker thread that processes audio chunks from queue."""
        logger.info("Streaming worker thread started")

        accumulated_audio: List[np.ndarray] = []
        accumulated_duration = 0.0

        try:
            while not self._stop_requested or not self.audio_queue.empty():
                try:
                    audio_chunk = self.audio_queue.get(timeout=0.1)

                    accumulated_audio.append(audio_chunk)
                    chunk_duration = len(audio_chunk) / self.sample_rate
                    accumulated_duration += chunk_duration

                    if accumulated_duration >= self.chunk_duration_sec:
                        self._process_incremental_chunk(accumulated_audio)
                        accumulated_audio.clear()
                        accumulated_duration = 0.0

                except queue.Empty:
                    if self._stop_requested and accumulated_audio:
                        self._process_incremental_chunk(accumulated_audio)
                        accumulated_audio.clear()
                        accumulated_duration = 0.0
                    continue

        except Exception as e:
            logger.error(f"Error in streaming worker loop: {e}", exc_info=True)
        finally:
            logger.info("Streaming worker thread exiting")

    def _process_incremental_chunk(self, new_chunks: List[np.ndarray]):
        """Transcribe only new audio (plus a short overlap tail).

        Args:
            new_chunks: Newly accumulated audio frames since the last cycle.
        """
        if not new_chunks:
            return

        try:
            start_time = time.time()

            new_audio = np.concatenate(new_chunks)
            if self._overlap_tail is not None and len(self._overlap_tail) > 0:
                audio_array = np.concatenate([self._overlap_tail, new_audio])
            else:
                audio_array = new_audio

            total_duration = len(audio_array) / self.sample_rate
            new_duration = len(new_audio) / self.sample_rate

            prepared = self._prepare_audio_for_whisper(audio_array)
            if prepared is None or len(prepared) == 0:
                return

            segments, _info = self.backend.model.transcribe(
                prepared,
                beam_size=1,
                vad_filter=False,
            )

            text_parts = []
            for segment in segments:
                if self._stop_requested:
                    break
                text_parts.append(segment.text)

            chunk_text = " ".join(text_parts).strip()
            self.preview_text = append_preview_text(self.preview_text, chunk_text)

            overlap_samples = int(self.overlap_sec * self.sample_rate)
            if overlap_samples > 0 and len(new_audio) > 0:
                self._overlap_tail = new_audio[-overlap_samples:].copy()
            else:
                self._overlap_tail = None

            processing_time = time.time() - start_time
            self._chunk_count += 1

            logger.info(
                f"Incremental transcription #{self._chunk_count}: "
                f"{new_duration:.1f}s new (+{total_duration - new_duration:.1f}s overlap) "
                f"-> {processing_time:.2f}s processing ({len(chunk_text)} chars)"
            )

            if processing_time > 5.0:
                self._slow_chunks += 1
                if self._slow_chunks >= 3 and time.time() - self._last_warning_time > 30:
                    logger.warning("Incremental transcription falling behind (3+ slow chunks)")
                    self._last_warning_time = time.time()

            if self.callback and self.preview_text:
                # is_final=True means replace the full preview in the UI
                self.callback(self.preview_text, True)

        except Exception as e:
            logger.error(f"Error in incremental transcription: {e}", exc_info=True)

    def _prepare_audio_for_whisper(self, audio_array: np.ndarray) -> Optional[np.ndarray]:
        """Convert recorder audio to float32 mono at Whisper's sample rate."""
        if audio_array.dtype == np.int16:
            audio_array = audio_array.astype(np.float32) / 32768.0
        else:
            audio_array = audio_array.astype(np.float32)

        if len(audio_array.shape) > 1:
            audio_array = audio_array.mean(axis=1)

        if self.sample_rate != config.WHISPER_TARGET_SAMPLE_RATE:
            num_samples = int(
                len(audio_array) * config.WHISPER_TARGET_SAMPLE_RATE / self.sample_rate
            )
            if num_samples <= 0:
                return None
            audio_array = signal.resample(audio_array, num_samples)

        return audio_array

    def cleanup(self):
        """Clean up resources and stop streaming."""
        if self.is_streaming:
            self.stop_streaming()

        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

        logger.info("StreamingTranscriber cleaned up")
