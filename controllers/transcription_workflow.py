"""
Transcription workflow controller for executing transcription jobs.

Handles regular transcription, large file processing, retranscription, and file uploads.
"""

import logging
import os
import time
from typing import Optional, Callable, List
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import QObject, pyqtSignal

from config import config
from audio_processor import audio_processor
from transcriber import TranscriptionBackend


class TranscriptionWorkflow(QObject):
    """Executes transcription jobs including large file handling."""

    # Signals
    transcription_completed = pyqtSignal(str, float)  # text, transcription_time
    transcription_failed = pyqtSignal(str)  # error_message
    status_update = pyqtSignal(str)
    show_large_file_overlay = pyqtSignal(float, bool)  # file_size_mb, is_splitting

    def __init__(self, get_backend: Callable[[], Optional[TranscriptionBackend]]):
        """Initialize transcription workflow.

        Args:
            get_backend: Callback to get the current transcription backend.
        """
        super().__init__()
        self._get_backend = get_backend
        self.executor = ThreadPoolExecutor(max_workers=2)

        # Track transcription timing
        self._transcription_start_time: Optional[float] = None

    def transcribe_recording(self, audio_file: str):
        """Start transcription of a recording.

        Args:
            audio_file: Path to the recorded audio file.
        """
        if not os.path.exists(audio_file):
            logging.error(f"Audio file not found: {audio_file}")
            self.transcription_failed.emit("Audio file not found")
            return

        file_size = os.path.getsize(audio_file)
        logging.info(f"Audio file size: {file_size} bytes")

        if file_size < 100:
            logging.error(f"Audio file too small: {file_size} bytes")
            self.transcription_failed.emit("Audio file is empty or corrupted")
            return

        self._start_transcription(audio_file)

    def retranscribe_audio(self, audio_file_path: str):
        """Re-transcribe an existing audio file.

        Args:
            audio_file_path: Path to the audio file to transcribe.
        """
        if not os.path.exists(audio_file_path):
            logging.error(f"Audio file not found for re-transcription: {audio_file_path}")
            self.status_update.emit("Error: Audio file not found")
            return

        logging.info(f"Re-transcribing audio file: {audio_file_path}")
        self.status_update.emit("Processing...")
        self._start_transcription(audio_file_path)

    def upload_audio_file(self, audio_file_path: str):
        """Transcribe an uploaded audio file.

        Args:
            audio_file_path: Path to the uploaded audio file.
        """
        if not os.path.exists(audio_file_path):
            logging.error(f"Uploaded audio file not found: {audio_file_path}")
            self.status_update.emit("Error: Audio file not found")
            return

        logging.info(f"Processing uploaded audio file: {audio_file_path}")
        self.status_update.emit("Processing uploaded file...")
        self._start_transcription(audio_file_path)

    def _start_transcription(self, audio_file: str):
        """Start transcription workflow for an audio file.

        Handles both regular and large file transcription.

        Args:
            audio_file: Path to the audio file.
        """
        try:
            backend = self._get_backend()
            if not backend:
                self.transcription_failed.emit("No transcription backend available")
                return

            needs_splitting, file_size_mb = audio_processor.check_file_size(audio_file)

            # Only split if backend requires it (OpenAI has 25MB limit, local doesn't)
            should_split = needs_splitting and backend.requires_file_splitting

            if should_split:
                logging.info(f"Large file ({file_size_mb:.2f} MB), backend requires splitting")
                self.show_large_file_overlay.emit(file_size_mb, True)
                self.status_update.emit(f"Splitting large file ({file_size_mb:.1f} MB)...")
                self.executor.submit(self._transcribe_large_audio, audio_file)
            elif needs_splitting:
                logging.info(f"Large file ({file_size_mb:.2f} MB), processing without splitting")
                self.show_large_file_overlay.emit(file_size_mb, False)
                self.status_update.emit(f"Processing large file ({file_size_mb:.1f} MB)...")
                self.executor.submit(self._transcribe_audio, audio_file)
            else:
                self.executor.submit(self._transcribe_audio, audio_file)

        except Exception as e:
            logging.error(f"Failed to start transcription: {e}")
            self.transcription_failed.emit(f"Failed to process audio: {e}")

    def _transcribe_audio(self, audio_file: str):
        """Transcribe audio in background thread.

        Args:
            audio_file: Path to the audio file.
        """
        try:
            backend = self._get_backend()
            if not backend:
                self.transcription_failed.emit("No transcription backend available")
                return

            self.status_update.emit("Transcribing...")
            start_time = time.time()
            transcribed_text = backend.transcribe(audio_file)
            transcription_time = time.time() - start_time
            self.transcription_completed.emit(transcribed_text, transcription_time)

        except Exception as e:
            logging.error(f"Transcription failed: {e}")
            self.transcription_failed.emit(str(e))

    def _transcribe_large_audio(self, audio_file: str):
        """Transcribe large audio file by splitting into chunks.

        Args:
            audio_file: Path to the audio file.
        """
        chunk_files: List[str] = []
        start_time = time.time()

        try:
            backend = self._get_backend()
            if not backend:
                self.transcription_failed.emit("No transcription backend available")
                return

            def progress_callback(message: str):
                self.status_update.emit(message)

            chunk_files = audio_processor.split_audio_file(audio_file, progress_callback)

            if not chunk_files:
                raise Exception("Failed to split audio file")

            if hasattr(backend, 'transcribe_chunks'):
                self.status_update.emit(f"Transcribing {len(chunk_files)} chunks...")
                transcribed_text = backend.transcribe_chunks(chunk_files)
            else:
                transcriptions = []
                for i, chunk_file in enumerate(chunk_files):
                    self.status_update.emit(f"Transcribing chunk {i+1}/{len(chunk_files)}...")
                    transcriptions.append(backend.transcribe(chunk_file))

                transcribed_text = audio_processor.combine_transcriptions(transcriptions)

            transcription_time = time.time() - start_time
            self.transcription_completed.emit(transcribed_text, transcription_time)

        except Exception as e:
            logging.error(f"Large audio transcription failed: {e}")
            self.transcription_failed.emit(str(e))
        finally:
            try:
                audio_processor.cleanup_temp_files()
            except Exception as cleanup_error:
                logging.warning(f"Failed to cleanup temp files: {cleanup_error}")

    def cancel_transcription(self):
        """Cancel any ongoing transcription."""
        backend = self._get_backend()
        if backend and backend.is_transcribing:
            backend.cancel_transcription()
            self.status_update.emit("Transcription cancelled")
            logging.info("Transcription cancelled")
            return True
        return False

    def is_transcribing(self) -> bool:
        """Check if transcription is in progress."""
        backend = self._get_backend()
        return backend.is_transcribing if backend else False

    def cleanup(self):
        """Cleanup resources."""
        try:
            # Cancel any ongoing transcription
            backend = self._get_backend()
            if backend and backend.is_transcribing:
                logging.info("Cancelling ongoing transcription...")
                backend.cancel_transcription()
        except Exception as e:
            logging.debug(f"Error cancelling transcription: {e}")

        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            # Python < 3.9 doesn't support cancel_futures
            self.executor.shutdown(wait=False)
        except Exception as e:
            logging.debug(f"Error during executor shutdown: {e}")
