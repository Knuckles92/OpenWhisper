"""
Meeting Controller for orchestrating long-form meeting transcription.
Manages the recording lifecycle, durable chunk spooling, and async transcription.
"""
import logging
import os
import queue
import threading
import time
import wave
from typing import Callable, List, Optional

import numpy as np
from scipy import signal
from scipy.io import wavfile

from config import config
from services.database import db
from services.meeting_storage import MeetingEntry, meeting_storage
from services.recorder import AudioRecorder
from transcriber import LocalWhisperBackend

# Whisper models expect 16kHz audio
WHISPER_SAMPLE_RATE = 16000

# Meeting-specific settings (larger chunks for better quality)
MEETING_CHUNK_DURATION_SEC = config.MEETING_CHUNK_DURATION_SEC


class ChunkSpooler:
    """Accumulate live audio and durably spool fixed-size WAV chunks to disk."""

    def __init__(self, meeting_id: str, sample_rate: int, chunk_duration_sec: float = MEETING_CHUNK_DURATION_SEC):
        self.meeting_id = meeting_id
        self.sample_rate = sample_rate
        self.chunk_duration_sec = chunk_duration_sec
        self.audio_queue: queue.Queue = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.is_running = False
        self._stop_requested = False
        self._chunk_index = 0
        self._elapsed_audio_sec = 0.0

    def start(self) -> None:
        if self.is_running:
            return
        self.is_running = True
        self._stop_requested = False
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
        logging.info("ChunkSpooler started")

    def feed_audio(self, audio_chunk: np.ndarray) -> None:
        """Accept audio from the recorder callback without dropping it."""
        if self.is_running:
            self.audio_queue.put(audio_chunk.copy())

    def stop(self) -> None:
        if not self.is_running:
            return
        self._stop_requested = True
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=30.0)
            if self.worker_thread.is_alive():
                logging.warning("ChunkSpooler worker did not finish in time")
        self.worker_thread = None
        self.is_running = False

    def _worker_loop(self) -> None:
        accumulated_audio: List[np.ndarray] = []
        accumulated_duration = 0.0

        try:
            while not self._stop_requested or not self.audio_queue.empty():
                try:
                    audio_chunk = self.audio_queue.get(timeout=0.2)
                    accumulated_audio.append(audio_chunk)
                    accumulated_duration += len(audio_chunk) / self.sample_rate

                    if accumulated_duration >= self.chunk_duration_sec:
                        self._flush_chunk(accumulated_audio, accumulated_duration)
                        accumulated_audio = []
                        accumulated_duration = 0.0
                except queue.Empty:
                    continue
        except Exception as e:
            logging.error(f"ChunkSpooler worker error: {e}", exc_info=True)
        finally:
            if accumulated_audio:
                self._flush_chunk(accumulated_audio, accumulated_duration)

    def _flush_chunk(self, audio_chunks: List[np.ndarray], duration_sec: float) -> None:
        if not audio_chunks:
            return

        start_offset_sec = self._elapsed_audio_sec
        end_offset_sec = start_offset_sec + duration_sec
        self._elapsed_audio_sec = end_offset_sec

        audio_array = np.concatenate(audio_chunks)
        if len(audio_array.shape) > 1:
            audio_array = audio_array.mean(axis=1)

        meeting_folder = os.path.join(config.MEETING_AUDIO_FOLDER, self.meeting_id[:8])
        os.makedirs(meeting_folder, exist_ok=True)
        filename = f"chunk_{self._chunk_index:04d}.wav"
        file_path = os.path.join(meeting_folder, filename)
        relative_path = os.path.join(self.meeting_id[:8], filename)

        with wave.open(file_path, 'wb') as wf:
            wf.setnchannels(config.CHANNELS)
            wf.setsampwidth(np.dtype(config.AUDIO_FORMAT).itemsize)
            wf.setframerate(self.sample_rate)
            wf.writeframes(audio_array.astype(config.AUDIO_FORMAT).tobytes())

        meeting_storage.register_spool_chunk(
            meeting_id=self.meeting_id,
            chunk_index=self._chunk_index,
            audio_file=relative_path,
            start_offset_sec=start_offset_sec,
            end_offset_sec=end_offset_sec,
        )
        self._chunk_index += 1


class AsyncTranscriptionWorker:
    """Consume durable chunks from storage and transcribe them in order."""

    def __init__(self, backend: LocalWhisperBackend, meeting_id: str):
        self.backend = backend
        self.meeting_id = meeting_id
        self.worker_thread: Optional[threading.Thread] = None
        self.is_running = False
        self._stop_requested = False
        self.on_chunk_transcribed: Optional[Callable[[str], None]] = None

    def start(self, on_chunk_transcribed: Callable[[str], None]) -> None:
        if self.is_running:
            return
        self.on_chunk_transcribed = on_chunk_transcribed
        self.is_running = True
        self._stop_requested = False
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
        logging.info("AsyncTranscriptionWorker started")

    def stop(self) -> None:
        if not self.is_running:
            return
        self._stop_requested = True
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=60.0)
            if self.worker_thread.is_alive():
                logging.warning("AsyncTranscriptionWorker did not finish in time")
        self.worker_thread = None
        self.is_running = False

    def drain_backlog(self, timeout_sec: float = 300.0) -> bool:
        """Wait for the durable chunk backlog to clear."""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            if not db.get_pending_chunks_for_meeting(self.meeting_id):
                return True
            time.sleep(0.25)
        return False

    def _worker_loop(self) -> None:
        while not self._stop_requested:
            pending_chunks = db.get_pending_chunks_for_meeting(self.meeting_id)
            if not pending_chunks:
                time.sleep(0.25)
                continue

            chunk = pending_chunks[0]
            chunk_index = chunk['chunk_index']
            audio_file = chunk.get('audio_file')
            if not audio_file:
                db.mark_chunk_failed(self.meeting_id, chunk_index, "Missing audio_file")
                continue

            file_path = os.path.join(config.MEETING_AUDIO_FOLDER, audio_file)
            if not os.path.exists(file_path):
                db.mark_chunk_failed(self.meeting_id, chunk_index, f"Missing spool file: {audio_file}")
                continue

            db.mark_chunk_processing(self.meeting_id, chunk_index)
            try:
                text = self._transcribe_file(file_path)
                db.update_chunk_transcribed(self.meeting_id, chunk_index, text)
                if text and self.on_chunk_transcribed:
                    self.on_chunk_transcribed(text)
            except Exception as e:
                logging.error(f"Error transcribing chunk {chunk_index}: {e}", exc_info=True)
                db.mark_chunk_failed(self.meeting_id, chunk_index, str(e))

    def _transcribe_file(self, file_path: str) -> str:
        sample_rate, audio_array = wavfile.read(file_path)

        if audio_array.dtype == np.int16:
            audio_array = audio_array.astype(np.float32) / 32768.0
        else:
            audio_array = audio_array.astype(np.float32)

        if len(audio_array.shape) > 1:
            audio_array = audio_array.mean(axis=1)

        if sample_rate != WHISPER_SAMPLE_RATE:
            num_samples = int(len(audio_array) * WHISPER_SAMPLE_RATE / sample_rate)
            audio_array = signal.resample(audio_array, num_samples)

        segments, _ = self.backend.model.transcribe(
            audio_array,
            beam_size=config.FASTER_WHISPER_BEAM_SIZE,
            vad_filter=config.FASTER_WHISPER_VAD_ENABLED,
        )
        return " ".join(segment.text for segment in segments).strip()


class MeetingController:
    """Orchestrates meeting recording, durable spooling, and storage."""

    def __init__(self, backend: Optional[LocalWhisperBackend] = None):
        self.backend = backend
        self.recorder: Optional[AudioRecorder] = None
        self.spooler: Optional[ChunkSpooler] = None
        self.transcription_worker: Optional[AsyncTranscriptionWorker] = None
        self.is_recording = False
        self._current_meeting: Optional[MeetingEntry] = None
        self.on_chunk_transcribed: Optional[Callable[[str], None]] = None
        self.on_meeting_ended: Optional[Callable[[MeetingEntry], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        self.on_transcription_lag: Optional[Callable[[int], None]] = None
        logging.info("MeetingController initialized")

    def set_backend(self, backend: LocalWhisperBackend):
        """Set the transcription backend."""
        self.backend = backend

    def start_meeting(self, title: str = "", device_id: Optional[int] = None) -> bool:
        if self.is_recording:
            logging.warning("Meeting already in progress")
            return False

        if self.backend is None:
            logging.error("No transcription backend available")
            if self.on_error:
                self.on_error("No transcription backend available")
            return False

        try:
            self._current_meeting = meeting_storage.start_meeting(title)
            self.recorder = AudioRecorder(device_id=device_id)
            self.spooler = ChunkSpooler(
                meeting_id=self._current_meeting.id,
                sample_rate=config.SAMPLE_RATE,
                chunk_duration_sec=MEETING_CHUNK_DURATION_SEC,
            )
            self.transcription_worker = AsyncTranscriptionWorker(
                backend=self.backend,
                meeting_id=self._current_meeting.id,
            )

            self.spooler.start()
            self.transcription_worker.start(on_chunk_transcribed=self._on_chunk_transcribed)
            self.recorder.set_streaming_callback(self.spooler.feed_audio)

            if not self.recorder.start_recording():
                raise Exception("Failed to start audio recording")

            self.is_recording = True
            logging.info(f"Meeting started: {self._current_meeting.id[:8]}...")
            return True
        except Exception as e:
            logging.error(f"Failed to start meeting: {e}", exc_info=True)
            self._cleanup_failed_start()
            if self.on_error:
                self.on_error(f"Failed to start meeting: {e}")
            return False

    def _cleanup_failed_start(self):
        """Clean up after a failed meeting start."""
        if self.transcription_worker:
            self.transcription_worker.stop()
            self.transcription_worker = None
        if self.spooler:
            self.spooler.stop()
            self.spooler = None
        if self.recorder:
            self.recorder.cleanup()
            self.recorder = None
        self._current_meeting = None
        self.is_recording = False

    def _on_chunk_transcribed(self, text: str):
        """Handle a transcribed chunk."""
        if self.on_chunk_transcribed:
            self.on_chunk_transcribed(text)

    def stop_meeting(self) -> Optional[MeetingEntry]:
        from services.settings import settings_manager

        if not self.is_recording:
            logging.warning("No meeting in progress")
            return None

        logging.info("Stopping meeting...")

        try:
            duration = 0.0
            if self.recorder:
                duration = self.recorder.get_recording_duration()
                self.recorder.stop_recording()
                self.recorder.wait_for_stop_completion()

            if self.recorder:
                self.recorder.set_streaming_callback(None)

            if self.spooler:
                self.spooler.stop()
                self.spooler = None

            if self.transcription_worker:
                drained = self.transcription_worker.drain_backlog(timeout_sec=300.0)
                if not drained:
                    logging.warning("Timed out waiting for meeting transcription backlog to drain")
                self.transcription_worker.stop()
                self.transcription_worker = None

            audio_file = None
            if self.recorder and self.recorder.has_recording_data() and self._current_meeting:
                try:
                    settings = settings_manager.load_meeting_recording_settings()
                    if settings.get('enabled', True):
                        audio_file = meeting_storage.save_complete_recording(
                            meeting_id=self._current_meeting.id,
                            recorder=self.recorder
                        )
                        if audio_file:
                            logging.info(f"Saved complete meeting recording: {audio_file}")
                        else:
                            logging.warning("Failed to save meeting recording (no data or error)")
                except Exception as save_err:
                    logging.error(f"Error saving meeting recording: {save_err}")

            meeting = meeting_storage.end_meeting(duration, audio_file=audio_file)

            if self.recorder:
                self.recorder.cleanup()
                self.recorder = None

            self.is_recording = False
            self._current_meeting = None

            if meeting and self.on_meeting_ended:
                self.on_meeting_ended(meeting)

            logging.info(f"Meeting ended: {meeting.id[:8] if meeting else 'unknown'}...")
            return meeting

        except Exception as e:
            logging.error(f"Error stopping meeting: {e}", exc_info=True)
            self.is_recording = False
            return None
    
    def get_current_meeting(self) -> Optional[MeetingEntry]:
        return self._current_meeting

    def get_all_meetings(self) -> List[MeetingEntry]:
        return meeting_storage.get_all_meetings()

    def get_meetings_for_display(self) -> List[dict]:
        return meeting_storage.get_meetings_for_display()

    def get_meeting(self, meeting_id: str) -> Optional[MeetingEntry]:
        return meeting_storage.get_meeting(meeting_id)

    def delete_meeting(self, meeting_id: str) -> bool:
        return meeting_storage.delete_meeting(meeting_id)

    def rename_meeting(self, meeting_id: str, new_title: str) -> bool:
        return meeting_storage.update_meeting_title(meeting_id, new_title)

    def recover_pending_chunks(self) -> None:
        """Resume transcription for interrupted meetings with pending durable chunks."""
        if self.backend is None:
            return

        for meeting in db.get_all_meetings():
            if meeting.get('status') != 'interrupted':
                continue

            meeting_id = meeting['id']
            pending_chunks = db.get_pending_chunks_for_meeting(meeting_id)
            if not pending_chunks:
                continue

            logging.info(f"Recovering {len(pending_chunks)} chunks for interrupted meeting {meeting_id[:8]}...")
            worker = AsyncTranscriptionWorker(self.backend, meeting_id)
            worker.start(on_chunk_transcribed=lambda _text: None)
            worker.drain_backlog(timeout_sec=600.0)
            worker.stop()

    def cleanup(self):
        """Clean up all resources."""
        if self.is_recording:
            self.stop_meeting()

        if self.transcription_worker:
            self.transcription_worker.stop()
            self.transcription_worker = None

        if self.spooler:
            self.spooler.stop()
            self.spooler = None

        if self.recorder:
            self.recorder.cleanup()
            self.recorder = None

        logging.info("MeetingController cleaned up")
