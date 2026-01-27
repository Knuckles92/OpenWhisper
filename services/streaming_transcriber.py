"""
Real-time streaming transcription using faster-whisper with queue-based architecture.

This module provides live transcription preview while recording, processing audio
in ~3-second chunks and emitting partial results via callback. The streaming
transcriber runs in a separate worker thread to avoid interfering with recording.
"""
import queue
import threading
import logging
import time
import numpy as np
from scipy import signal
from typing import Callable, Optional, List
from config import config

# Whisper models expect 16kHz audio
WHISPER_SAMPLE_RATE = 16000


class StreamingTranscriber:
    """Manages real-time streaming transcription using a worker thread."""

    def __init__(self, backend, chunk_duration_sec: float = 3.0):
        """Initialize the streaming transcriber.

        Args:
            backend: LocalWhisperBackend instance with loaded model
            chunk_duration_sec: Duration of audio chunks to accumulate before transcribing
        """
        self.backend = backend
        self.chunk_duration_sec = chunk_duration_sec

        # Audio queue for producer-consumer pattern
        self.audio_queue: queue.Queue = queue.Queue(maxsize=config.STREAMING_QUEUE_SIZE)

        # Worker thread management
        self.worker_thread: Optional[threading.Thread] = None
        self.is_streaming = False
        self._stop_requested = False

        # Transcription accumulation
        self.all_transcriptions: List[str] = []

        # Master audio buffer - holds ALL audio from session for rolling re-transcription
        self._all_audio_buffer: List[np.ndarray] = []

        # Audio parameters
        self.sample_rate = 0
        self.callback: Optional[Callable[[str, bool], None]] = None

        # Performance monitoring
        self._chunk_count = 0
        self._slow_chunks = 0
        self._last_warning_time = 0

        logging.info(f"StreamingTranscriber initialized (chunk_duration={chunk_duration_sec}s)")

    def start_streaming(self, sample_rate: int, callback: Callable[[str, bool], None]):
        """Start the streaming worker thread.

        Args:
            sample_rate: Audio sample rate (Hz)
            callback: Function(text, is_final) called with partial/final results
        """
        if self.is_streaming:
            logging.warning("Streaming already active")
            return

        self.sample_rate = sample_rate
        self.callback = callback
        self.is_streaming = True
        self._stop_requested = False
        self.all_transcriptions.clear()
        self._all_audio_buffer.clear()  # Clear master audio buffer
        self._chunk_count = 0
        self._slow_chunks = 0

        # Start worker thread
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()

        logging.info("Streaming transcription started")

    def feed_audio(self, audio_chunk: np.ndarray):
        """Feed audio chunk to transcription queue (called from recorder callback).

        Args:
            audio_chunk: NumPy array of audio data (int16 or float32)
        """
        if not self.is_streaming:
            return

        try:
            # Non-blocking put - if queue is full, drop this chunk
            self.audio_queue.put_nowait(audio_chunk.copy())
        except queue.Full:
            # Queue backup - we're falling behind
            logging.debug("Audio queue full, dropping chunk (transcription can't keep up)")

    def stop_streaming(self) -> str:
        """Stop streaming and return final combined transcription.

        Returns:
            Combined transcription text from all chunks
        """
        if not self.is_streaming:
            return ""

        logging.info("Stopping streaming transcription...")
        self._stop_requested = True

        # Wait for worker thread to finish (with timeout)
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5.0)
            if self.worker_thread.is_alive():
                logging.warning("Worker thread did not finish in time")

        self.is_streaming = False
        self.worker_thread = None

        # Get the final transcription (now stored as complete text)
        final_text = " ".join(self.all_transcriptions).strip()

        # Clear buffers
        self._all_audio_buffer.clear()

        logging.info(f"Streaming stopped. Re-transcription cycles: {self._chunk_count}, "
                    f"Final length: {len(final_text)} chars")

        return final_text

    def _worker_loop(self):
        """Worker thread that processes audio chunks from queue."""
        logging.info("Streaming worker thread started")

        # Accumulation buffer for audio chunks
        accumulated_audio: List[np.ndarray] = []
        accumulated_duration = 0.0

        try:
            while not self._stop_requested or not self.audio_queue.empty():
                try:
                    # Get audio chunk from queue (with timeout to check stop flag)
                    audio_chunk = self.audio_queue.get(timeout=0.1)

                    # Add to MASTER buffer (keeps all audio for rolling re-transcription)
                    self._all_audio_buffer.append(audio_chunk)

                    # Track accumulated duration for threshold triggering
                    accumulated_audio.append(audio_chunk)
                    chunk_duration = len(audio_chunk) / self.sample_rate
                    accumulated_duration += chunk_duration

                    # Check if we have enough NEW audio to trigger re-transcription
                    if accumulated_duration >= self.chunk_duration_sec:
                        # Re-transcribe ALL audio (rolling re-transcription for context)
                        self._process_all_audio()

                        # Reset threshold tracker (but NOT the master buffer)
                        accumulated_audio.clear()
                        accumulated_duration = 0.0

                except queue.Empty:
                    # No audio available, check if we should process final buffer
                    if self._stop_requested and self._all_audio_buffer:
                        # Final transcription - process all accumulated audio
                        self._process_all_audio()
                        accumulated_audio.clear()
                        accumulated_duration = 0.0
                    continue

        except Exception as e:
            logging.error(f"Error in streaming worker loop: {e}", exc_info=True)
        finally:
            logging.info("Streaming worker thread exiting")

    def _process_all_audio(self):
        """Re-transcribe ALL accumulated audio (rolling re-transcription).

        This method transcribes the complete master audio buffer each time,
        giving Whisper full context to handle words split across chunk boundaries
        and self-correct earlier transcription mistakes.
        """
        if not self._all_audio_buffer:
            return

        try:
            # Start timing
            start_time = time.time()

            # Concatenate ALL audio from session into single array
            audio_array = np.concatenate(self._all_audio_buffer)

            # Calculate total duration
            total_duration = len(audio_array) / self.sample_rate

            # Convert to float32 format expected by faster-whisper (range: -1.0 to 1.0)
            if audio_array.dtype == np.int16:
                audio_array = audio_array.astype(np.float32) / 32768.0

            # Ensure mono (faster-whisper expects 1D array)
            if len(audio_array.shape) > 1:
                audio_array = audio_array.mean(axis=1)

            # Resample from recording sample rate (44.1kHz) to Whisper's expected rate (16kHz)
            # This is critical - passing 44.1kHz audio to Whisper results in gibberish
            if self.sample_rate != WHISPER_SAMPLE_RATE:
                # Calculate number of samples needed at target rate
                num_samples = int(len(audio_array) * WHISPER_SAMPLE_RATE / self.sample_rate)
                audio_array = signal.resample(audio_array, num_samples)
                logging.debug(f"Resampled audio from {self.sample_rate}Hz to {WHISPER_SAMPLE_RATE}Hz")

            # Transcribe using faster-whisper model
            segments, info = self.backend.model.transcribe(
                audio_array,
                beam_size=config.STREAMING_BEAM_SIZE,
                vad_filter=False  # Disable VAD for streaming (faster)
            )

            # Collect text from all segments
            text_parts = []
            for segment in segments:
                if self._stop_requested:
                    break
                text_parts.append(segment.text)

            # Combine segment texts - this is the COMPLETE transcription
            full_text = " ".join(text_parts).strip()

            # Update metrics
            processing_time = time.time() - start_time
            self._chunk_count += 1

            logging.info(f"Rolling transcription #{self._chunk_count}: "
                        f"{total_duration:.1f}s audio -> {processing_time:.2f}s processing "
                        f"({len(full_text)} chars)")

            # Performance monitoring
            if processing_time > 5.0:
                self._slow_chunks += 1
                if self._slow_chunks >= 3 and time.time() - self._last_warning_time > 30:
                    logging.warning("Rolling transcription falling behind (3+ slow chunks)")
                    self._last_warning_time = time.time()

            # Store the complete transcription (replaces previous)
            self.all_transcriptions = [full_text] if full_text else []

            # Emit callback with COMPLETE transcription (is_final=True means replace)
            if self.callback and full_text:
                self.callback(full_text, True)

        except Exception as e:
            logging.error(f"Error in rolling transcription: {e}", exc_info=True)

    def cleanup(self):
        """Clean up resources and stop streaming."""
        if self.is_streaming:
            self.stop_streaming()

        # Clear queue
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

        logging.info("StreamingTranscriber cleaned up")
