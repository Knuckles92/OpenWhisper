"""
Main application bootstrap
"""
import warnings

warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

import faulthandler
import logging
import os
import signal
import sys
import subprocess
import platform
import time
import pyperclip
import keyboard
from pathlib import Path
from typing import Dict, Optional
from concurrent.futures import ThreadPoolExecutor

from PyQt6.QtCore import QObject, pyqtSignal

from config import config


_CRASH_LOG_FILE = None
_QT_MESSAGE_HANDLER_INSTALLED = False


def _patch_subprocess_for_windows():
    """Patch subprocess.Popen to hide console windows on Windows.
    
    This prevents the console flash when running with pythonw.exe,
    especially when whisper calls ffmpeg internally via subprocess.
    """
    if platform.system() != "Windows":
        return
    
    _original_popen = subprocess.Popen
    
    class _NoConsolePopen(_original_popen):
        """Popen wrapper that adds CREATE_NO_WINDOW flag on Windows."""
        
        def __init__(self, *args, **kwargs):
            # Add CREATE_NO_WINDOW to creationflags if not already set
            if 'creationflags' not in kwargs:
                kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
            elif not (kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW):
                kwargs['creationflags'] |= subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)
    
    subprocess.Popen = _NoConsolePopen


# Apply the subprocess patch immediately on import (before whisper is loaded)
_patch_subprocess_for_windows()
from ui_qt.app import QtApplication
from ui_qt.loading_screen_qt import ModernLoadingScreen
from ui_qt.ui_controller import UIController
from services.recorder import AudioRecorder
from services.hotkey_manager import HotkeyManager
from services.settings import settings_manager
from services.streaming_transcriber import StreamingTranscriber
from transcriber import TranscriptionBackend, LocalWhisperBackend, OpenAIBackend
from services.audio_processor import audio_processor
from services.history_manager import history_manager


def setup_logging():
    """Setup application logging."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
        handlers=[
            logging.FileHandler(config.LOG_FILE),
            logging.StreamHandler()
        ],
        force=True  # Required: removes handlers created during import-time logging
    )
    _enable_crash_logging()
    _install_qt_message_handler()


def _enable_crash_logging():
    """Enable faulthandler crash logging for hard crashes."""
    global _CRASH_LOG_FILE

    try:
        crash_log_path = Path(config.LOG_FILE).with_suffix(".crash.log")
        _CRASH_LOG_FILE = open(crash_log_path, "a", buffering=1)
        faulthandler.enable(file=_CRASH_LOG_FILE, all_threads=True)

        for sig in (signal.SIGSEGV, signal.SIGABRT, signal.SIGFPE, signal.SIGILL):
            try:
                faulthandler.register(sig, file=_CRASH_LOG_FILE, all_threads=True)
            except (ValueError, RuntimeError, AttributeError):
                # Some platforms or Python builds disallow registering these signals.
                pass

        logging.info(f"Faulthandler enabled for crash diagnostics: {crash_log_path}")
    except Exception as e:
        logging.warning(f"Failed to enable faulthandler: {e}")


def _install_qt_message_handler():
    """Route Qt warnings/errors to the Python logger."""
    global _QT_MESSAGE_HANDLER_INSTALLED

    if _QT_MESSAGE_HANDLER_INSTALLED:
        return

    try:
        from PyQt6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception as e:
        logging.warning(f"Failed to install Qt message handler: {e}")
        return

    def _qt_message_handler(msg_type, context, message):
        logger = logging.getLogger("qt")
        context_info = ""
        try:
            if context and (context.file or context.function or context.line):
                context_info = f" ({context.file}:{context.line} {context.function})"
        except Exception:
            context_info = ""

        text = f"{message}{context_info}"

        if msg_type == QtMsgType.QtDebugMsg:
            logger.debug(text)
        elif msg_type == QtMsgType.QtInfoMsg:
            logger.info(text)
        elif msg_type == QtMsgType.QtWarningMsg:
            logger.warning(text)
        elif msg_type == QtMsgType.QtCriticalMsg:
            logger.error(text)
        elif msg_type == QtMsgType.QtFatalMsg:
            logger.critical(text)
        else:
            logger.info(text)

    qInstallMessageHandler(_qt_message_handler)
    _QT_MESSAGE_HANDLER_INSTALLED = True
    logging.info("Qt message handler installed")


class ApplicationController(QObject):
    """Main application controller integrating UI and logic."""

    # Qt signals for thread-safe UI updates
    transcription_completed = pyqtSignal(str)
    transcription_failed = pyqtSignal(str)
    status_update = pyqtSignal(str)
    stt_state_changed = pyqtSignal(bool)  # True = enabled, False = disabled
    recording_state_changed = pyqtSignal(bool)  # True = started, False = stopped
    partial_transcription = pyqtSignal(str, bool)  # (text, is_final)
    streaming_text_update = pyqtSignal(str, bool)  # (text, is_final) for streaming overlay
    streaming_overlay_show = pyqtSignal()  # Signal to show streaming overlay (thread-safe)
    streaming_overlay_hide = pyqtSignal()  # Signal to hide streaming overlay
    caret_indicator_show = pyqtSignal()  # Signal to show caret paste indicator
    caret_indicator_hide = pyqtSignal()  # Signal to hide caret paste indicator

    def __init__(self, ui_controller: UIController):
        super().__init__()
        self.ui_controller = ui_controller
        # Load saved audio device and create recorder
        saved_device_id = settings_manager.load_audio_input_device()
        self.recorder = AudioRecorder(device_id=saved_device_id)
        self.hotkey_manager: Optional[HotkeyManager] = None
        self.executor = ThreadPoolExecutor(max_workers=2)

        self.transcription_backends: Dict[str, TranscriptionBackend] = {}
        self.current_backend: Optional[TranscriptionBackend] = None

        self._current_model_name: str = "local_whisper"

        # Streaming transcription
        self.streaming_transcriber: Optional[StreamingTranscriber] = None
        self._streaming_enabled = False
        self._streaming_paste_enabled = False

        # Track source audio file for history (to save recording)
        self._pending_audio_file: Optional[str] = None

        self._transcription_start_time: Optional[float] = None
        self._pending_audio_duration: Optional[float] = None
        self._pending_file_size: Optional[int] = None

        self._setup_transcription_backends()
        self._setup_hotkeys()
        self._setup_ui_callbacks()
        self._setup_audio_level_callback()
        self._setup_streaming()
        self._connect_signals()

    def _setup_transcription_backends(self):
        """Initialize transcription backends."""
        logging.info("Setting up transcription backends...")

        self.transcription_backends['local_whisper'] = LocalWhisperBackend()
        self.transcription_backends['api_whisper'] = OpenAIBackend('api_whisper')
        self.transcription_backends['api_gpt4o'] = OpenAIBackend('api_gpt4o')
        self.transcription_backends['api_gpt4o_mini'] = OpenAIBackend('api_gpt4o_mini')

        saved_model = settings_manager.load_model_selection()
        self.current_backend = self.transcription_backends.get(
            saved_model, self.transcription_backends['local_whisper']
        )
        logging.info(f"Using transcription backend: {saved_model}")

    def _setup_hotkeys(self):
        """Setup hotkey management."""
        logging.info("Setting up hotkeys...")
        hotkeys = settings_manager.load_hotkey_settings()
        self.hotkey_manager = HotkeyManager(hotkeys)
        self.hotkey_manager.set_callbacks(
            on_record_toggle=self.toggle_recording,
            on_cancel=self.cancel_recording,
            on_status_update=self.update_status_with_auto_hide,
            on_status_update_auto_hide=self.update_status_with_auto_hide
        )
        self.ui_controller.update_hotkey_display(hotkeys)

    def _setup_ui_callbacks(self):
        """Setup UI event callbacks."""
        self.ui_controller.on_record_start = self.start_recording
        self.ui_controller.on_record_stop = self.stop_recording
        self.ui_controller.on_record_cancel = self.cancel_recording
        self.ui_controller.on_model_changed = self.on_model_changed
        self.ui_controller.on_hotkeys_changed = self.update_hotkeys
        self.ui_controller.on_retranscribe = self.retranscribe_audio
        self.ui_controller.on_upload_audio = self.upload_audio_file
        self.ui_controller.on_whisper_settings_changed = self.reload_whisper_model
        self.ui_controller.on_audio_device_changed = self.change_audio_device

    def update_hotkeys(self, hotkeys: Dict[str, str]):
        """Update application hotkeys."""
        logging.info(f"Updating hotkeys: {hotkeys}")
        if self.hotkey_manager:
            self.hotkey_manager.update_hotkeys(hotkeys)
            settings_manager.save_hotkey_settings(hotkeys)
            self.ui_controller.set_status("Hotkeys updated")

    def reload_whisper_model(self):
        """Reload the local whisper model with current settings."""
        logging.info("Reloading whisper model...")
        self.ui_controller.set_status("Reloading whisper engine...")

        local_backend = self.transcription_backends.get('local_whisper')
        if local_backend:
            # Reload the model (will pick up new settings)
            local_backend.reload_model()

            if hasattr(local_backend, 'device_info'):
                self.ui_controller.set_device_info(local_backend.device_info)
                logging.info(f"Whisper reloaded: {local_backend.device_info}")

            self.ui_controller.set_status("Whisper engine reloaded")
        else:
            logging.warning("Local whisper backend not found")
            self.ui_controller.set_status("Ready")

    def change_audio_device(self, device_id: Optional[int]):
        """Change the audio input device.

        Args:
            device_id: New device ID, or None for system default.
        """
        logging.info(f"Changing audio device to: {device_id}")

        # Don't change device while recording
        if self.recorder.is_recording:
            logging.warning("Cannot change audio device while recording")
            self.ui_controller.set_status("Stop recording before changing device")
            return

        # Clean up old recorder
        self.recorder.cleanup()

        # Create new recorder with new device
        self.recorder = AudioRecorder(device_id=device_id)
        self._setup_audio_level_callback()

        device_name = "System Default" if device_id is None else f"Device {device_id}"
        logging.info(f"Audio device changed to: {device_name}")
        self.ui_controller.set_status(f"Audio device changed")

    def _setup_audio_level_callback(self):
        """Setup audio level callback for waveform display."""
        def audio_level_callback(level: float):
            # Convert single level to list for compatibility
            levels = [level] * 20
            self.ui_controller.update_audio_levels(levels)

        self.recorder.set_audio_level_callback(audio_level_callback)

    def _setup_streaming(self):
        """Initialize streaming transcriber if enabled."""
        try:
            # Load streaming settings
            settings = settings_manager.load_all_settings()
            self._streaming_enabled = settings.get('streaming_enabled', config.STREAMING_ENABLED)
            self._streaming_paste_enabled = settings.get('streaming_paste_enabled', False)

            if self._streaming_enabled and isinstance(self.current_backend, LocalWhisperBackend):
                chunk_duration = settings.get('streaming_chunk_duration', config.STREAMING_CHUNK_DURATION_SEC)
                self.streaming_transcriber = StreamingTranscriber(
                    backend=self.current_backend,
                    chunk_duration_sec=chunk_duration
                )
                logging.info(f"Streaming transcription enabled (chunk_duration={chunk_duration}s, paste_overlay={self._streaming_paste_enabled})")
            else:
                if self._streaming_enabled:
                    logging.info("Streaming requested but not available (requires Local Whisper backend)")
                self._streaming_enabled = False
                self._streaming_paste_enabled = False
        except Exception as e:
            logging.error(f"Failed to setup streaming: {e}")
            self._streaming_enabled = False
            self._streaming_paste_enabled = False

    def _connect_signals(self):
        """Connect Qt signals to UI controller methods."""
        self.transcription_completed.connect(self._on_transcription_complete)
        self.transcription_failed.connect(self._on_transcription_error)
        self.status_update.connect(self.ui_controller.set_status)
        self.stt_state_changed.connect(self._on_stt_state_changed)
        self.recording_state_changed.connect(self._on_recording_state_changed)
        self.partial_transcription.connect(self.ui_controller.main_window.set_partial_transcription)
        self.streaming_text_update.connect(self.ui_controller.update_streaming_text)
        self.streaming_overlay_show.connect(self.ui_controller.show_streaming_overlay)
        self.streaming_overlay_hide.connect(self.ui_controller.hide_streaming_overlay)
        self.caret_indicator_show.connect(self.ui_controller.show_caret_paste_indicator)
        self.caret_indicator_hide.connect(self.ui_controller.hide_caret_paste_indicator)

    def _on_stt_state_changed(self, enabled: bool):
        """Handle STT state change on main thread."""
        if enabled:
            self.ui_controller.overlay.show_at_cursor(self.ui_controller.overlay.STATE_STT_ENABLE)
        else:
            self.ui_controller.overlay.show_at_cursor(self.ui_controller.overlay.STATE_STT_DISABLE)

    def _on_recording_state_changed(self, is_recording: bool):
        """Handle recording state change on main thread.

        This ensures UI state is synchronized when recording is triggered via hotkeys.
        """
        # Update ui_controller and main_window state on the main thread
        self.ui_controller.is_recording = is_recording
        if self.ui_controller.main_window.is_recording != is_recording:
            self.ui_controller.main_window.is_recording = is_recording
            self.ui_controller.main_window._update_recording_state()

    def _on_partial_transcription(self, text: str, is_final: bool):
        """Handle partial transcription from streaming worker (background thread).

        Args:
            text: Partial transcription text
            is_final: Whether this chunk is finalized
        """
        # Emit signal for thread-safe UI update (main window display)
        self.partial_transcription.emit(text, is_final)

        # Update streaming text overlay if paste mode is enabled
        if self._streaming_paste_enabled and text:
            self.streaming_text_update.emit(text, is_final)

    def start_recording(self):
        """Start audio recording."""
        if self.recorder.start_recording():
            logging.info("Recording started")
            self.ui_controller.clear_transcription_stats()
            self.ui_controller.main_window.clear_partial_transcription()

            # Start streaming transcription if enabled
            if self.streaming_transcriber:
                self.recorder.set_streaming_callback(self.streaming_transcriber.feed_audio)
                self.streaming_transcriber.start_streaming(
                    sample_rate=config.SAMPLE_RATE,
                    callback=self._on_partial_transcription
                )
                logging.info("Streaming transcription started")

                # Show streaming overlay instead of waveform when paste mode is enabled
                # Use signal for thread-safe UI update (recording can be triggered via hotkey thread)
                if self._streaming_paste_enabled:
                    self.streaming_overlay_show.emit()

            # Emit signal to update UI state (thread-safe for hotkey triggers)
            self.recording_state_changed.emit(True)
            self.status_update.emit("Recording...")
        else:
            self.status_update.emit("Failed to start recording")

    def stop_recording(self):
        """Stop audio recording and start transcription."""
        if self._streaming_paste_enabled:
            self.streaming_overlay_hide.emit()
            settings = settings_manager.load_all_settings()
            auto_paste = settings.get('auto_paste', True)
            if auto_paste:
                self.caret_indicator_show.emit()

        # Stop streaming first and get accumulated text
        streaming_text = ""
        if self.streaming_transcriber:
            streaming_text = self.streaming_transcriber.stop_streaming()
            self.recorder.set_streaming_callback(None)  # Disconnect callback
            logging.info(f"Streaming transcription stopped, got {len(streaming_text)} chars")

        if not self.recorder.stop_recording():
            self.status_update.emit("Failed to stop recording")
            return

        # Emit signal to update UI state (thread-safe for hotkey triggers)
        self.recording_state_changed.emit(False)
        self.status_update.emit("Processing...")

        # Ensure the recorder thread has flushed the post-roll before saving
        if not self.recorder.wait_for_stop_completion():
            logging.warning("Proceeding without confirmed post-roll completion; tail of recording may be short")

        # Check if we have recording data
        if not self.recorder.has_recording_data():
            logging.error("No recording data available")
            self._on_transcription_error("No audio data recorded")
            return

        if not self.recorder.save_recording():
            logging.error("Failed to save recording")
            self._on_transcription_error("Failed to save audio file")
            return

        if not os.path.exists(config.RECORDED_AUDIO_FILE):
            logging.error(f"Audio file not found: {config.RECORDED_AUDIO_FILE}")
            self._on_transcription_error("Audio file not created")
            return

        file_size = os.path.getsize(config.RECORDED_AUDIO_FILE)
        logging.info(f"Audio file size: {file_size} bytes")
        if file_size < 100:
            logging.error(f"Audio file too small: {file_size} bytes")
            self._on_transcription_error("Audio file is empty or corrupted")
            return

        # Track the audio file for history saving
        self._pending_audio_file = config.RECORDED_AUDIO_FILE

        self._pending_audio_duration = self.recorder.get_recording_duration()
        self._pending_file_size = file_size

        try:
            needs_splitting, file_size_mb = audio_processor.check_file_size(
                config.RECORDED_AUDIO_FILE
            )

            # Only split if backend requires it (OpenAI has 25MB limit, local doesn't)
            should_split = needs_splitting and self.current_backend.requires_file_splitting

            if should_split:
                logging.info(f"Large file ({file_size_mb:.2f} MB), backend requires splitting")
                self._show_large_file_overlay(file_size_mb, is_splitting=True)
                self.status_update.emit(f"Splitting large file ({file_size_mb:.1f} MB)...")
                self.executor.submit(self._transcribe_large_audio)
            elif needs_splitting:
                logging.info(f"Large file ({file_size_mb:.2f} MB), processing without splitting")
                self._show_large_file_overlay(file_size_mb, is_splitting=False)
                self.status_update.emit(f"Processing large file ({file_size_mb:.1f} MB)...")
                self.executor.submit(self._transcribe_audio)
            else:
                self.executor.submit(self._transcribe_audio)

            logging.info(f"Transcription started. Duration: {self.recorder.get_recording_duration():.2f}s")

        except Exception as e:
            logging.error(f"Failed to start transcription: {e}")
            self._on_transcription_error(f"Failed to process audio: {e}")

    def toggle_recording(self):
        """Toggle between starting and stopping recording.

        Uses signals to ensure thread-safe UI updates when triggered via hotkeys.
        """
        logging.info(f"Toggle recording. Current state: {self.recorder.is_recording}")
        if not self.recorder.is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def cancel_recording(self):
        """Cancel recording or transcription.

        Uses signals to ensure thread-safe UI updates when triggered via hotkeys.
        """
        logging.info(f"Cancel called. Recording: {self.recorder.is_recording}")

        if self.recorder.is_recording:
            # Stop streaming if active
            if self.streaming_transcriber:
                self.streaming_transcriber.stop_streaming()
                self.recorder.set_streaming_callback(None)
                logging.info("Streaming transcription cancelled")

            # Hide streaming overlay if visible (no text deletion needed!)
            if self._streaming_paste_enabled:
                self.streaming_overlay_hide.emit()
                self.caret_indicator_hide.emit()

            # Emit signal to update UI state (thread-safe for hotkey triggers)
            self.recording_state_changed.emit(False)
            self.recorder.stop_recording()
            self.recorder.clear_recording_data()
            self.status_update.emit("Recording cancelled")
            logging.info("Recording cancelled")
        elif self.current_backend and self.current_backend.is_transcribing:
            self.current_backend.cancel_transcription()
            self.status_update.emit("Transcription cancelled")
            logging.info("Transcription cancelled")
        else:
            self.status_update.emit("Cancelled")

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
        
        # Track the audio file for history (won't re-save since it's already in recordings)
        self._pending_audio_file = None
        
        self.status_update.emit("Processing...")
        
        try:
            needs_splitting, file_size_mb = audio_processor.check_file_size(audio_file_path)

            # Only split if backend requires it (OpenAI has 25MB limit, local doesn't)
            should_split = needs_splitting and self.current_backend.requires_file_splitting

            if should_split:
                logging.info(f"Large file ({file_size_mb:.2f} MB), backend requires splitting")
                self._show_large_file_overlay(file_size_mb, is_splitting=True)
                self.status_update.emit(f"Splitting large file ({file_size_mb:.1f} MB)...")
                self.executor.submit(self._retranscribe_large_audio, audio_file_path)
            elif needs_splitting:
                logging.info(f"Large file ({file_size_mb:.2f} MB), processing without splitting")
                self._show_large_file_overlay(file_size_mb, is_splitting=False)
                self.status_update.emit(f"Processing large file ({file_size_mb:.1f} MB)...")
                self.executor.submit(self._retranscribe_audio_file, audio_file_path)
            else:
                self.executor.submit(self._retranscribe_audio_file, audio_file_path)

        except Exception as e:
            logging.error(f"Failed to start re-transcription: {e}")
            self._on_transcription_error(f"Failed to process audio: {e}")

    def upload_audio_file(self, audio_file_path: str):
        """Transcribe an uploaded audio file.
        
        This method handles manually uploaded audio files, processing them
        through the standard transcription pipeline with chunking support.
        
        Args:
            audio_file_path: Path to the uploaded audio file.
        """
        if not os.path.exists(audio_file_path):
            logging.error(f"Uploaded audio file not found: {audio_file_path}")
            self.status_update.emit("Error: Audio file not found")
            return
        
        logging.info(f"Processing uploaded audio file: {audio_file_path}")
        
        # For uploaded files, we don't save to recordings folder (it's already external)
        self._pending_audio_file = None
        
        self.status_update.emit("Processing uploaded file...")
        
        try:
            needs_splitting, file_size_mb = audio_processor.check_file_size(audio_file_path)

            # Only split if backend requires it (OpenAI has 25MB limit, local doesn't)
            should_split = needs_splitting and self.current_backend.requires_file_splitting

            if should_split:
                logging.info(f"Large uploaded file ({file_size_mb:.2f} MB), backend requires splitting")
                self._show_large_file_overlay(file_size_mb, is_splitting=True)
                self.status_update.emit(f"Splitting large file ({file_size_mb:.1f} MB)...")
                self.executor.submit(self._retranscribe_large_audio, audio_file_path)
            elif needs_splitting:
                logging.info(f"Large uploaded file ({file_size_mb:.2f} MB), processing without splitting")
                self._show_large_file_overlay(file_size_mb, is_splitting=False)
                self.status_update.emit(f"Processing large file ({file_size_mb:.1f} MB)...")
                self.executor.submit(self._retranscribe_audio_file, audio_file_path)
            else:
                self.executor.submit(self._retranscribe_audio_file, audio_file_path)

        except Exception as e:
            logging.error(f"Failed to process uploaded audio: {e}")
            self._on_transcription_error(f"Failed to process audio: {e}")
    
    def _retranscribe_audio_file(self, audio_file_path: str):
        """Re-transcribe a single audio file in background thread."""
        try:
            self._pending_file_size = os.path.getsize(audio_file_path)
            self._pending_audio_duration = None

            self.status_update.emit("Transcribing...")
            self._transcription_start_time = time.time()
            transcribed_text = self.current_backend.transcribe(audio_file_path)
            self.transcription_completed.emit(transcribed_text)
        except Exception as e:
            logging.error(f"Re-transcription failed: {e}")
            self.transcription_failed.emit(str(e))
    
    def _retranscribe_large_audio(self, audio_file_path: str):
        """Re-transcribe a large audio file by splitting into chunks."""
        chunk_files = []
        self._pending_file_size = os.path.getsize(audio_file_path)
        self._pending_audio_duration = None
        self._transcription_start_time = time.time()
        try:
            def progress_callback(message):
                self.status_update.emit(message)

            chunk_files = audio_processor.split_audio_file(audio_file_path, progress_callback)
            
            if not chunk_files:
                raise Exception("Failed to split audio file")
            
            if hasattr(self.current_backend, 'transcribe_chunks'):
                self.status_update.emit(f"Transcribing {len(chunk_files)} chunks...")
                transcribed_text = self.current_backend.transcribe_chunks(chunk_files)
            else:
                transcriptions = []
                for i, chunk_file in enumerate(chunk_files):
                    self.status_update.emit(f"Transcribing chunk {i+1}/{len(chunk_files)}...")
                    transcriptions.append(self.current_backend.transcribe(chunk_file))
                transcribed_text = audio_processor.combine_transcriptions(transcriptions)
            
            self.transcription_completed.emit(transcribed_text)
            
        except Exception as e:
            logging.error(f"Large audio re-transcription failed: {e}")
            self.transcription_failed.emit(str(e))
        finally:
            try:
                audio_processor.cleanup_temp_files()
            except Exception as cleanup_error:
                logging.warning(f"Failed to cleanup temp files: {cleanup_error}")

    def _transcribe_audio(self):
        """Transcribe audio in background thread."""
        try:
            self.status_update.emit("Transcribing...")
            self._transcription_start_time = time.time()
            transcribed_text = self.current_backend.transcribe(config.RECORDED_AUDIO_FILE)
            self.transcription_completed.emit(transcribed_text)

        except Exception as e:
            logging.error(f"Transcription failed: {e}")
            self.transcription_failed.emit(str(e))

    def _transcribe_large_audio(self):
        """Transcribe large audio file by splitting into chunks."""
        chunk_files = []
        self._transcription_start_time = time.time()
        try:
            def progress_callback(message):
                self.status_update.emit(message)

            chunk_files = audio_processor.split_audio_file(
                config.RECORDED_AUDIO_FILE, progress_callback
            )

            if not chunk_files:
                raise Exception("Failed to split audio file")

            if hasattr(self.current_backend, 'transcribe_chunks'):
                self.status_update.emit(f"Transcribing {len(chunk_files)} chunks...")
                transcribed_text = self.current_backend.transcribe_chunks(chunk_files)
            else:
                transcriptions = []
                for i, chunk_file in enumerate(chunk_files):
                    self.status_update.emit(
                        f"Transcribing chunk {i+1}/{len(chunk_files)}..."
                    )
                    transcriptions.append(self.current_backend.transcribe(chunk_file))

                transcribed_text = audio_processor.combine_transcriptions(transcriptions)

            self.transcription_completed.emit(transcribed_text)

        except Exception as e:
            logging.error(f"Large audio transcription failed: {e}")
            self.transcription_failed.emit(str(e))
        finally:
            try:
                audio_processor.cleanup_temp_files()
            except Exception as cleanup_error:
                logging.warning(f"Failed to cleanup temp files: {cleanup_error}")

    def _on_transcription_complete(self, transcribed_text: str):
        """Handle transcription completion."""
        self.ui_controller.set_transcription(transcribed_text)
        self.ui_controller.set_status("Transcription complete!")
        self.ui_controller.hide_overlay()

        transcription_time = None
        if self._transcription_start_time is not None:
            transcription_time = time.time() - self._transcription_start_time
            self._transcription_start_time = None

        if transcription_time is not None:
            audio_duration = self._pending_audio_duration or 0.0
            file_size = self._pending_file_size or 0
            self.ui_controller.set_transcription_stats(
                transcription_time, audio_duration, file_size
            )

        try:
            # Get detailed model info for local whisper
            model_info = self._current_model_name
            if self._current_model_name == 'local_whisper':
                local_backend = self.transcription_backends.get('local_whisper')
                if local_backend and hasattr(local_backend, 'device_info'):
                    model_info = f"local_whisper ({local_backend.device_info})"

            history_manager.add_entry(
                text=transcribed_text,
                model=model_info,
                source_audio_file=self._pending_audio_file,
                transcription_time=transcription_time,
                audio_duration=self._pending_audio_duration,
                file_size=self._pending_file_size
            )
            self.ui_controller.refresh_history()
            logging.info("Transcription saved to history")
        except Exception as e:
            logging.error(f"Failed to save transcription to history: {e}")
        finally:
            self._pending_audio_file = None
            self._pending_audio_duration = None
            self._pending_file_size = None

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
                # Single clean paste operation - no text selection/deletion needed
                # The streaming overlay showed text without modifying the document
                keyboard.send('ctrl+v')

                logging.info("Transcription auto-pasted")
                self.ui_controller.set_status("Ready (Pasted)")
            except Exception as e:
                logging.error(f"Failed to auto-paste: {e}")
                self.ui_controller.set_status("Transcription complete (paste failed)")
        else:
            self.ui_controller.set_status("Ready")

        if self._streaming_paste_enabled:
            self.caret_indicator_hide.emit()

    def _on_transcription_error(self, error_message: str):
        """Handle transcription error."""
        self.ui_controller.set_status(f"Error: {error_message}")
        self.ui_controller.set_transcription(f"Error: {error_message}")
        self.ui_controller.hide_overlay()
        if self._streaming_paste_enabled:
            self.caret_indicator_hide.emit()

    def on_model_changed(self, model_name: str):
        """Handle model selection change."""
        model_value = config.MODEL_VALUE_MAP.get(model_name)
        if model_value and model_value in self.transcription_backends:
            self.current_backend = self.transcription_backends[model_value]
            self._current_model_name = model_value
            settings_manager.save_model_selection(model_value)
            logging.info(f"Switched to model: {model_value}")

            if model_value == 'local_whisper':
                local_backend = self.transcription_backends.get('local_whisper')
                if local_backend and hasattr(local_backend, 'device_info'):
                    self.ui_controller.set_device_info(local_backend.device_info)
            else:
                self.ui_controller.set_device_info("")

    def update_status_with_auto_hide(self, status: str):
        """Update status with auto-hide after delay."""
        # Use signals for thread-safe UI updates (called from hotkey thread)
        self.status_update.emit(status)

        if status == "STT Enabled":
            self.stt_state_changed.emit(True)
        elif status == "STT Disabled":
            self.stt_state_changed.emit(False)

    def _show_large_file_overlay(self, file_size_mb: float, is_splitting: bool):
        """Show appropriate overlay for large file processing.

        Args:
            file_size_mb: Size of the file in megabytes.
            is_splitting: True if file will be split (OpenAI), False otherwise (local).
        """
        overlay = self.ui_controller.overlay
        overlay.set_large_file_info(file_size_mb)

        if is_splitting:
            overlay.show_at_cursor(overlay.STATE_LARGE_FILE_SPLITTING)
        else:
            overlay.show_at_cursor(overlay.STATE_LARGE_FILE_PROCESSING)

    def cleanup(self):
        """Cleanup resources."""
        logging.info("Starting application cleanup...")
        
        try:
            if self.current_backend and self.current_backend.is_transcribing:
                logging.info("Cancelling ongoing transcription...")
                self.current_backend.cancel_transcription()
        except Exception as e:
            logging.debug(f"Error cancelling transcription: {e}")
        
        try:
            if self.hotkey_manager:
                self.hotkey_manager.cleanup()
        except Exception as e:
            logging.debug(f"Error during hotkey cleanup: {e}")
        
        try:
            if self.recorder:
                self.recorder.cleanup()
        except Exception as e:
            logging.debug(f"Error during recorder cleanup: {e}")

        try:
            if self.streaming_transcriber:
                self.streaming_transcriber.cleanup()
        except Exception as e:
            logging.debug(f"Error during streaming transcriber cleanup: {e}")

        try:
            self.executor.shutdown(wait=True, cancel_futures=True)
        except TypeError:
            # Python < 3.9 doesn't support cancel_futures
            self.executor.shutdown(wait=False)
        except Exception as e:
            logging.debug(f"Error during executor shutdown: {e}")
        
        try:
            for backend_name, backend in self.transcription_backends.items():
                try:
                    logging.info(f"Cleaning up transcription backend: {backend_name}")
                    backend.cleanup()
                except Exception as e:
                    logging.debug(f"Error cleaning up {backend_name} backend: {e}")
            self.transcription_backends.clear()
            self.current_backend = None
        except Exception as e:
            logging.debug(f"Error during transcription backends cleanup: {e}")
        
        try:
            self.ui_controller.cleanup()
        except Exception as e:
            logging.debug(f"Error during UI controller cleanup: {e}")
        
        logging.info("Application controller cleaned up")


def main():
    """Main application entry point with modern PyQt6 UI."""
    setup_logging()
    logging.info("=" * 60)
    logging.info("Starting OpenWhisper with Modern PyQt6 UI")
    logging.info("=" * 60)

    qt_app = QtApplication()

    loading_screen = None
    ui_controller = None
    app_controller = None

    try:
        loading_screen = ModernLoadingScreen()
        loading_screen.show()

        loading_screen.update_status("Initializing components...")
        loading_screen.update_progress("Loading theme...")
        loading_screen.repaint()

        from PyQt6.QtCore import QCoreApplication
        QCoreApplication.processEvents()

        loading_screen.update_status("Creating interface...")
        loading_screen.update_progress("Setting up windows...")
        QCoreApplication.processEvents()

        ui_controller = UIController()

        loading_screen.update_status("Initializing audio system...")
        loading_screen.update_progress("Loading transcription models...")
        QCoreApplication.processEvents()

        app_controller = ApplicationController(ui_controller)

        local_backend = app_controller.transcription_backends.get('local_whisper')
        if local_backend and hasattr(local_backend, 'device_info'):
            device_info = local_backend.device_info
            loading_screen.update_progress(f"Using {device_info}")
            QCoreApplication.processEvents()
            logging.info(f"Whisper device: {device_info}")

        loading_screen.destroy()
        loading_screen = None

        ui_controller.show_main_window()

        if local_backend and hasattr(local_backend, 'device_info'):
            device_info = local_backend.device_info
            ui_controller.set_device_info(device_info)

        logging.info("Application initialization complete")
        logging.info("Starting event loop")

        return qt_app.run(ui_controller.main_window)

    except Exception as e:
        logging.exception("Application startup failed")
        raise

    finally:
        try:
            if loading_screen is not None:
                loading_screen.destroy()
        except Exception as e:
            logging.exception("Failed to cleanup loading screen")

        try:
            if app_controller is not None:
                app_controller.cleanup()
            elif ui_controller is not None:
                ui_controller.cleanup()
        except Exception as e:
            logging.exception("Failed to cleanup controllers")

        logging.info("=" * 60)
        logging.info("Application shutdown complete")
        logging.info("=" * 60)


if __name__ == "__main__":
    sys.exit(main())
