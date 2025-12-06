"""
Main application bootstrap for OpenWhisper.

This module contains the application entry point and the main ApplicationController
that orchestrates all sub-controllers.
"""
import warnings

warnings.filterwarnings("ignore", message="pkg_resources is deprecated")

import logging
import os
import sys
import subprocess
import platform
from typing import Dict, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from config import config


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
from services import settings_manager

# Services
from services import (
    TranscriptionService,
    RecordingService,
    WorkflowService,
    CompletionService,
    HotkeyService,
)


def setup_logging():
    """Setup application logging."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
        handlers=[
            logging.FileHandler(config.LOG_FILE),
            logging.StreamHandler()
        ]
    )


class ApplicationController(QObject):
    """Main application controller that orchestrates all sub-controllers."""

    # Qt signals for thread-safe UI updates
    stt_state_changed = pyqtSignal(bool)  # True = enabled, False = disabled

    def __init__(self, ui_controller: UIController):
        super().__init__()
        self.ui_controller = ui_controller
        self.hotkey_manager: Optional[HotkeyService] = None

        # Create services
        self.transcription_ctrl = TranscriptionService()
        self.recording_ctrl = RecordingService()
        self.workflow = WorkflowService(
            get_backend=self.transcription_ctrl.get_current_backend
        )
        self.completion = CompletionService(
            get_model_info=self.transcription_ctrl.get_model_info
        )

        # Connect all signals
        self._connect_controller_signals()
        self._connect_ui_signals()

        # Setup hotkeys and UI callbacks
        self._setup_hotkeys()
        self._setup_ui_callbacks()

    def _connect_controller_signals(self):
        """Connect signals between sub-controllers."""
        # Recording -> Workflow: when recording is ready, start transcription
        self.recording_ctrl.recording_ready.connect(self._on_recording_ready)

        # Workflow -> Completion: when transcription completes
        self.workflow.transcription_completed.connect(
            self.completion.on_transcription_complete
        )
        self.workflow.transcription_failed.connect(
            self.completion.on_transcription_error
        )

        # Transcription controller -> UI: device info changes
        self.transcription_ctrl.device_info_changed.connect(
            self.ui_controller.set_device_info
        )

    def _connect_ui_signals(self):
        """Connect sub-controller signals to UI updates."""
        # Recording state -> UI
        self.recording_ctrl.recording_state_changed.connect(
            self._on_recording_state_changed
        )
        self.recording_ctrl.status_update.connect(self.ui_controller.set_status)
        self.recording_ctrl.audio_levels_updated.connect(
            self.ui_controller.update_audio_levels
        )

        # Workflow status -> UI
        self.workflow.status_update.connect(self.ui_controller.set_status)
        self.workflow.show_large_file_overlay.connect(self._on_show_large_file_overlay)

        # Completion -> UI
        self.completion.status_update.connect(self.ui_controller.set_status)
        self.completion.transcription_display.connect(
            self.ui_controller.set_transcription
        )
        self.completion.stats_update.connect(self.ui_controller.set_transcription_stats)
        self.completion.history_refresh.connect(self.ui_controller.refresh_history)

        # STT state -> UI overlay
        self.stt_state_changed.connect(self._on_stt_state_changed)

    def _setup_hotkeys(self):
        """Setup hotkey management."""
        logging.info("Setting up hotkeys...")
        hotkeys = settings_manager.load_hotkey_settings()
        self.hotkey_manager = HotkeyService(hotkeys)
        self.hotkey_manager.set_callbacks(
            on_record_toggle=self._on_hotkey_toggle_recording,
            on_cancel=self._on_hotkey_cancel,
            on_status_update=self._on_hotkey_status_update,
            on_status_update_auto_hide=self._on_hotkey_status_update
        )
        self.ui_controller.update_hotkey_display(hotkeys)

    def _setup_ui_callbacks(self):
        """Setup UI event callbacks."""
        self.ui_controller.on_record_start = self.recording_ctrl.start_recording
        self.ui_controller.on_record_stop = self.recording_ctrl.stop_recording
        self.ui_controller.on_record_cancel = self._cancel_operation
        self.ui_controller.on_model_changed = self.transcription_ctrl.on_model_changed
        self.ui_controller.on_hotkeys_changed = self._on_hotkeys_changed
        self.ui_controller.on_retranscribe = self._on_retranscribe
        self.ui_controller.on_upload_audio = self._on_upload_audio
        self.ui_controller.on_whisper_settings_changed = self._on_whisper_settings_changed

    # ----- Signal Handlers -----

    def _on_recording_ready(self, audio_file: str, duration: float, file_size: int):
        """Handle recording ready for transcription."""
        # Set metadata for completion handler
        self.completion.set_pending_metadata(
            audio_file=audio_file,
            audio_duration=duration,
            file_size=file_size
        )
        # Clear transcription stats before new transcription
        self.ui_controller.clear_transcription_stats()
        # Start transcription
        self.workflow.transcribe_recording(audio_file)

    def _on_recording_state_changed(self, is_recording: bool):
        """Handle recording state change on main thread."""
        self.ui_controller.is_recording = is_recording
        if self.ui_controller.main_window.is_recording != is_recording:
            self.ui_controller.main_window.is_recording = is_recording
            self.ui_controller.main_window._update_recording_state()

    def _on_stt_state_changed(self, enabled: bool):
        """Handle STT state change on main thread."""
        if enabled:
            self.ui_controller.overlay.show_at_cursor(
                self.ui_controller.overlay.STATE_STT_ENABLE
            )
        else:
            self.ui_controller.overlay.show_at_cursor(
                self.ui_controller.overlay.STATE_STT_DISABLE
            )

    def _on_show_large_file_overlay(self, file_size_mb: float, is_splitting: bool):
        """Show appropriate overlay for large file processing."""
        overlay = self.ui_controller.overlay
        overlay.set_large_file_info(file_size_mb)

        if is_splitting:
            overlay.show_at_cursor(overlay.STATE_LARGE_FILE_SPLITTING)
        else:
            overlay.show_at_cursor(overlay.STATE_LARGE_FILE_PROCESSING)

    # ----- Hotkey Handlers -----

    def _on_hotkey_toggle_recording(self):
        """Handle hotkey toggle recording."""
        self.recording_ctrl.toggle_recording()

    def _on_hotkey_cancel(self):
        """Handle hotkey cancel."""
        self._cancel_operation()

    def _on_hotkey_status_update(self, status: str):
        """Handle hotkey status update."""
        self.ui_controller.set_status(status)

        if status == "STT Enabled":
            self.stt_state_changed.emit(True)
        elif status == "STT Disabled":
            self.stt_state_changed.emit(False)

    # ----- UI Callback Handlers -----

    def _on_hotkeys_changed(self, hotkeys: Dict[str, str]):
        """Update application hotkeys."""
        logging.info(f"Updating hotkeys: {hotkeys}")
        if self.hotkey_manager:
            self.hotkey_manager.update_hotkeys(hotkeys)
            settings_manager.save_hotkey_settings(hotkeys)
            self.ui_controller.set_status("Hotkeys updated")

    def _on_retranscribe(self, audio_file_path: str):
        """Re-transcribe an existing audio file."""
        # For retranscription, we don't save to recordings (already there)
        self.completion.set_pending_metadata(
            audio_file=None,  # Don't re-save
            file_size=os.path.getsize(audio_file_path) if os.path.exists(audio_file_path) else 0
        )
        self.workflow.retranscribe_audio(audio_file_path)

    def _on_upload_audio(self, audio_file_path: str):
        """Transcribe an uploaded audio file."""
        # For uploads, we don't save to recordings (external file)
        self.completion.set_pending_metadata(
            audio_file=None,
            file_size=os.path.getsize(audio_file_path) if os.path.exists(audio_file_path) else 0
        )
        self.workflow.upload_audio_file(audio_file_path)

    def _on_whisper_settings_changed(self):
        """Reload the local whisper model with current settings."""
        self.ui_controller.set_status("Reloading whisper engine...")
        device_info = self.transcription_ctrl.reload_whisper_model()
        if device_info:
            self.ui_controller.set_status("Whisper engine reloaded")
        else:
            self.ui_controller.set_status("Ready")

    def _cancel_operation(self):
        """Cancel recording or transcription."""
        logging.info(f"Cancel called. Recording: {self.recording_ctrl.is_recording}")

        if self.recording_ctrl.is_recording:
            self.recording_ctrl.cancel_recording()
        elif self.workflow.is_transcribing():
            self.workflow.cancel_transcription()
        else:
            self.ui_controller.set_status("Cancelled")

    def cleanup(self):
        """Cleanup resources."""
        logging.info("Starting application cleanup...")

        # Cleanup sub-controllers in reverse order of dependencies
        try:
            self.workflow.cleanup()
        except Exception as e:
            logging.debug(f"Error during workflow cleanup: {e}")

        try:
            if self.hotkey_manager:
                self.hotkey_manager.cleanup()
        except Exception as e:
            logging.debug(f"Error during hotkey cleanup: {e}")

        try:
            self.recording_ctrl.cleanup()
        except Exception as e:
            logging.debug(f"Error during recording controller cleanup: {e}")

        try:
            self.transcription_ctrl.cleanup()
        except Exception as e:
            logging.debug(f"Error during transcription controller cleanup: {e}")

        try:
            self.completion.cleanup()
        except Exception as e:
            logging.debug(f"Error during completion handler cleanup: {e}")

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

        device_info = app_controller.transcription_ctrl.get_local_device_info()
        if device_info:
            loading_screen.update_progress(f"Using {device_info}")
            QCoreApplication.processEvents()
            logging.info(f"Whisper device: {device_info}")

        loading_screen.destroy()
        loading_screen = None

        ui_controller.show_main_window()

        if device_info:
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
