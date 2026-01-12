"""
Meeting Controller for orchestrating long-form meeting transcription.
Manages the recording lifecycle, chunked transcription, and auto-save.
"""
import logging
import threading
import queue
import time
import numpy as np
from scipy import signal
from typing import Optional, Callable, List
from concurrent.futures import ThreadPoolExecutor

from config import config
from services.meeting_storage import meeting_storage, MeetingEntry
from services.recorder import AudioRecorder
from transcriber import LocalWhisperBackend

# Whisper models expect 16kHz audio
WHISPER_SAMPLE_RATE = 16000

# Meeting-specific settings (larger chunks for better quality)
MEETING_CHUNK_DURATION_SEC = 30.0  # Process every 30 seconds
MEETING_QUEUE_SIZE = 20  # Larger queue for longer recordings


class MeetingTranscriber:
    """Handles chunked transcription for meeting recordings."""
    
    def __init__(self, backend: LocalWhisperBackend, 
                 chunk_duration_sec: float = MEETING_CHUNK_DURATION_SEC):
        """Initialize the meeting transcriber.
        
        Args:
            backend: LocalWhisperBackend instance with loaded model
            chunk_duration_sec: Duration of audio chunks to process
        """
        self.backend = backend
        self.chunk_duration_sec = chunk_duration_sec
        
        # Audio queue for producer-consumer pattern
        self.audio_queue: queue.Queue = queue.Queue(maxsize=MEETING_QUEUE_SIZE)
        
        # Worker thread management
        self.worker_thread: Optional[threading.Thread] = None
        self.is_running = False
        self._stop_requested = False
        
        # Callbacks
        self.on_chunk_transcribed: Optional[Callable[[str], None]] = None
        
        # Audio parameters
        self.sample_rate = 0
        
        logging.info(f"MeetingTranscriber initialized (chunk_duration={chunk_duration_sec}s)")
    
    def start(self, sample_rate: int, 
              on_chunk_transcribed: Callable[[str], None]):
        """Start the transcription worker thread.
        
        Args:
            sample_rate: Audio sample rate (Hz)
            on_chunk_transcribed: Callback for each transcribed chunk
        """
        if self.is_running:
            logging.warning("Meeting transcriber already running")
            return
        
        self.sample_rate = sample_rate
        self.on_chunk_transcribed = on_chunk_transcribed
        self.is_running = True
        self._stop_requested = False
        
        # Start worker thread
        self.worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker_thread.start()
        
        logging.info("Meeting transcription started")
    
    def feed_audio(self, audio_chunk: np.ndarray):
        """Feed audio chunk to transcription queue.
        
        Args:
            audio_chunk: NumPy array of audio data
        """
        if not self.is_running:
            return
        
        try:
            # Non-blocking put
            self.audio_queue.put_nowait(audio_chunk.copy())
        except queue.Full:
            logging.debug("Meeting audio queue full, dropping chunk")
    
    def stop(self) -> None:
        """Stop the transcription worker."""
        if not self.is_running:
            return
        
        logging.info("Stopping meeting transcription...")
        self._stop_requested = True
        
        # Wait for worker thread
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=10.0)
            if self.worker_thread.is_alive():
                logging.warning("Meeting transcriber worker did not finish in time")
        
        self.is_running = False
        self.worker_thread = None
        logging.info("Meeting transcription stopped")
    
    def _worker_loop(self):
        """Worker thread that processes audio chunks."""
        logging.info("Meeting transcription worker started")
        
        accumulated_audio: List[np.ndarray] = []
        accumulated_duration = 0.0
        
        try:
            while not self._stop_requested or not self.audio_queue.empty():
                try:
                    # Get audio chunk from queue
                    audio_chunk = self.audio_queue.get(timeout=0.2)
                    
                    # Accumulate audio
                    accumulated_audio.append(audio_chunk)
                    chunk_duration = len(audio_chunk) / self.sample_rate
                    accumulated_duration += chunk_duration
                    
                    # Process when we have enough audio
                    if accumulated_duration >= self.chunk_duration_sec:
                        self._process_audio(accumulated_audio)
                        accumulated_audio.clear()
                        accumulated_duration = 0.0
                    
                except queue.Empty:
                    # Process remaining audio on stop
                    if self._stop_requested and accumulated_audio:
                        self._process_audio(accumulated_audio)
                        accumulated_audio.clear()
                        accumulated_duration = 0.0
                    continue
                    
        except Exception as e:
            logging.error(f"Error in meeting transcription worker: {e}", exc_info=True)
        finally:
            logging.info("Meeting transcription worker exiting")
    
    def _process_audio(self, audio_chunks: List[np.ndarray]):
        """Process accumulated audio and emit transcription.
        
        Args:
            audio_chunks: List of audio arrays to process
        """
        if not audio_chunks:
            return
        
        try:
            start_time = time.time()
            
            # Concatenate audio
            audio_array = np.concatenate(audio_chunks)
            
            # Convert to float32
            if audio_array.dtype == np.int16:
                audio_array = audio_array.astype(np.float32) / 32768.0
            
            # Ensure mono
            if len(audio_array.shape) > 1:
                audio_array = audio_array.mean(axis=1)
            
            # Resample to 16kHz for Whisper
            if self.sample_rate != WHISPER_SAMPLE_RATE:
                num_samples = int(len(audio_array) * WHISPER_SAMPLE_RATE / self.sample_rate)
                audio_array = signal.resample(audio_array, num_samples)
            
            # Transcribe
            segments, info = self.backend.model.transcribe(
                audio_array,
                beam_size=config.FASTER_WHISPER_BEAM_SIZE,
                vad_filter=config.FASTER_WHISPER_VAD_ENABLED
            )
            
            # Collect text
            text_parts = []
            for segment in segments:
                if self._stop_requested:
                    break
                text_parts.append(segment.text)
            
            chunk_text = " ".join(text_parts).strip()
            processing_time = time.time() - start_time
            
            logging.info(f"Meeting chunk transcribed: {processing_time:.2f}s processing, "
                        f"{len(chunk_text)} chars")
            
            if chunk_text and self.on_chunk_transcribed:
                self.on_chunk_transcribed(chunk_text)
                
        except Exception as e:
            logging.error(f"Error processing meeting audio chunk: {e}", exc_info=True)
    
    def cleanup(self):
        """Clean up resources."""
        self.stop()
        
        # Clear queue
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break


class MeetingController:
    """Orchestrates meeting recording, transcription, and storage."""
    
    def __init__(self, backend: Optional[LocalWhisperBackend] = None):
        """Initialize the meeting controller.
        
        Args:
            backend: LocalWhisperBackend instance for transcription
        """
        self.backend = backend
        self.recorder: Optional[AudioRecorder] = None
        self.transcriber: Optional[MeetingTranscriber] = None
        
        # State
        self.is_recording = False
        self._current_meeting: Optional[MeetingEntry] = None
        
        # Callbacks
        self.on_chunk_transcribed: Optional[Callable[[str], None]] = None
        self.on_meeting_ended: Optional[Callable[[MeetingEntry], None]] = None
        self.on_error: Optional[Callable[[str], None]] = None
        
        logging.info("MeetingController initialized")
    
    def set_backend(self, backend: LocalWhisperBackend):
        """Set the transcription backend.
        
        Args:
            backend: LocalWhisperBackend instance
        """
        self.backend = backend
    
    def start_meeting(self, title: str = "", device_id: Optional[int] = None) -> bool:
        """Start a new meeting recording and transcription.
        
        Args:
            title: Optional meeting title
            device_id: Optional audio device ID
            
        Returns:
            True if started successfully
        """
        if self.is_recording:
            logging.warning("Meeting already in progress")
            return False
        
        if self.backend is None:
            logging.error("No transcription backend available")
            if self.on_error:
                self.on_error("No transcription backend available")
            return False
        
        try:
            # Create meeting in storage
            self._current_meeting = meeting_storage.start_meeting(title)
            
            # Create recorder
            self.recorder = AudioRecorder(device_id=device_id)
            
            # Create transcriber
            self.transcriber = MeetingTranscriber(
                backend=self.backend,
                chunk_duration_sec=MEETING_CHUNK_DURATION_SEC
            )
            
            # Start recording
            if not self.recorder.start_recording():
                raise Exception("Failed to start audio recording")
            
            # Connect recorder to transcriber
            self.recorder.set_streaming_callback(self.transcriber.feed_audio)
            
            # Start transcription
            self.transcriber.start(
                sample_rate=config.SAMPLE_RATE,
                on_chunk_transcribed=self._on_chunk_transcribed
            )
            
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
        if self.transcriber:
            self.transcriber.cleanup()
            self.transcriber = None
        
        if self.recorder:
            self.recorder.cleanup()
            self.recorder = None
        
        self._current_meeting = None
        self.is_recording = False
    
    def _on_chunk_transcribed(self, text: str):
        """Handle a transcribed chunk.
        
        Args:
            text: Transcribed text
        """
        # Add to storage (auto-saves)
        meeting_storage.add_chunk(text)
        
        # Notify UI
        if self.on_chunk_transcribed:
            self.on_chunk_transcribed(text)
    
    def stop_meeting(self) -> Optional[MeetingEntry]:
        """Stop the current meeting.
        
        Returns:
            The completed MeetingEntry, or None if no meeting was active
        """
        if not self.is_recording:
            logging.warning("No meeting in progress")
            return None
        
        logging.info("Stopping meeting...")
        
        try:
            # Stop transcriber first (will process remaining audio)
            if self.transcriber:
                self.transcriber.stop()
            
            # Get recording duration
            duration = 0.0
            if self.recorder:
                duration = self.recorder.get_recording_duration()
                self.recorder.stop_recording()
            
            # End meeting in storage
            meeting = meeting_storage.end_meeting(duration)
            
            # Cleanup
            if self.transcriber:
                self.transcriber.cleanup()
                self.transcriber = None
            
            if self.recorder:
                self.recorder.cleanup()
                self.recorder = None
            
            self.is_recording = False
            self._current_meeting = None
            
            # Notify
            if meeting and self.on_meeting_ended:
                self.on_meeting_ended(meeting)
            
            logging.info(f"Meeting ended: {meeting.id[:8] if meeting else 'unknown'}...")
            return meeting
            
        except Exception as e:
            logging.error(f"Error stopping meeting: {e}", exc_info=True)
            self.is_recording = False
            return None
    
    def get_current_meeting(self) -> Optional[MeetingEntry]:
        """Get the current meeting.
        
        Returns:
            The current MeetingEntry, or None if no meeting active
        """
        return self._current_meeting
    
    def get_all_meetings(self) -> List[MeetingEntry]:
        """Get all meetings.
        
        Returns:
            List of MeetingEntry objects
        """
        return meeting_storage.get_all_meetings()
    
    def get_meetings_for_display(self) -> List[dict]:
        """Get meetings formatted for UI display.
        
        Returns:
            List of meeting dicts
        """
        return meeting_storage.get_meetings_for_display()
    
    def get_meeting(self, meeting_id: str) -> Optional[MeetingEntry]:
        """Get a meeting by ID.
        
        Args:
            meeting_id: Meeting ID
            
        Returns:
            The MeetingEntry, or None if not found
        """
        return meeting_storage.get_meeting(meeting_id)
    
    def delete_meeting(self, meeting_id: str) -> bool:
        """Delete a meeting.
        
        Args:
            meeting_id: Meeting ID
            
        Returns:
            True if deleted
        """
        return meeting_storage.delete_meeting(meeting_id)
    
    def cleanup(self):
        """Clean up all resources."""
        if self.is_recording:
            self.stop_meeting()
        
        if self.transcriber:
            self.transcriber.cleanup()
            self.transcriber = None
        
        if self.recorder:
            self.recorder.cleanup()
            self.recorder = None
        
        logging.info("MeetingController cleaned up")
