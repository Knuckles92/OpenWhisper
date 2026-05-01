"""
UI Controller for PyQt6 Application.
Manages the main window, overlay, and dialogs.
Bridges between UI and application logic.
"""
import logging
from enum import Enum
from typing import Optional, Callable, List
from PyQt6.QtCore import QTimer, pyqtSignal, QObject
from PyQt6.QtWidgets import QMessageBox, QFileDialog

from config import config

logger = logging.getLogger(__name__)


class OverlayState(Enum):
    """Logical overlay states. Routed by UIController.set_overlay_state.

    These describe *what to show*, not the human-readable status text. Status
    text is kept separate so renaming a status string never breaks UI routing.
    """
    NONE = "none"  # Hide all overlays
    RECORDING = "recording"
    PROCESSING = "processing"
    TRANSCRIBING = "transcribing"
    CANCELLING = "cancelling"
    STT_ENABLED = "stt_enabled"
    STT_DISABLED = "stt_disabled"


def _status_to_overlay_state(status: str) -> Optional[OverlayState]:
    """Infer the appropriate overlay state from a human-readable status string.

    Used to bridge legacy callers that emit only text. New code should call
    `UIController.set_overlay_state()` directly with an `OverlayState` value.
    """
    lower = status.lower()
    if "cancel" in lower:
        return OverlayState.CANCELLING
    if "STT Enabled" in status:
        return OverlayState.STT_ENABLED
    if "STT Disabled" in status:
        return OverlayState.STT_DISABLED
    if "recording" in lower:
        return OverlayState.RECORDING
    if "processing" in lower:
        return OverlayState.PROCESSING
    if "transcribing" in lower:
        return OverlayState.TRANSCRIBING
    if any(k in lower for k in ("ready", "failed", "error", "complete")):
        return OverlayState.NONE
    return None
from ui_qt.main_window import MainWindow
from ui_qt.overlays import CaretPasteIndicator, StreamingTextOverlay, WaveformOverlay
from ui_qt.system_tray import SystemTrayManager
from ui_qt.dialogs.settings_dialog import SettingsDialog
from ui_qt.dialogs.hotkey_dialog import HotkeyDialog
from ui_qt.dialogs.upload_preview_dialog import UploadPreviewDialog
from ui_qt.widgets import QuickRecordTab, TabbedContentWidget
from services.audio_processor import audio_processor


class UIController(QObject):
    """Controls the UI components and manages their interactions."""

    # Signals
    record_started = pyqtSignal()
    record_stopped = pyqtSignal()
    record_cancelled = pyqtSignal()
    model_changed = pyqtSignal(str)
    transcription_received = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    audio_levels_updated = pyqtSignal(list)

    def __init__(self):
        """Initialize UI controller."""
        super().__init__()

        # Create UI components
        self.main_window = MainWindow()
        self.overlay = WaveformOverlay()
        self.streaming_overlay = StreamingTextOverlay()
        self.caret_paste_indicator = CaretPasteIndicator()
        self.tray_manager = SystemTrayManager(self.main_window)

        # State
        self.is_recording = False
        self.audio_levels: List[float] = [0.0] * 20
        self.streaming_flow_active = False

        # Callbacks for external handlers
        self.on_record_start: Optional[Callable] = None
        self.on_record_stop: Optional[Callable] = None
        self.on_record_cancel: Optional[Callable] = None
        self.on_model_changed: Optional[Callable] = None
        self.on_hotkeys_changed: Optional[Callable] = None
        self.on_retranscribe: Optional[Callable] = None
        self.on_upload_audio: Optional[Callable] = None  # Callback for audio file upload
        self.on_whisper_settings_changed: Optional[Callable] = None  # Callback for whisper engine reload
        self.on_audio_device_changed: Optional[Callable] = None  # Callback for audio input device change
        self.on_streaming_settings_changed: Optional[Callable] = None  # Callback for streaming mode change

        # Timer to hide overlay after cancel animation completes
        self.cancel_animation_timer = QTimer()
        self.cancel_animation_timer.setSingleShot(True)
        self.cancel_animation_timer.timeout.connect(self._on_cancel_animation_finished)

        self._setup_connections()

    def _setup_connections(self):
        """Setup signal connections between UI components."""
        # Main window signals
        self.main_window.record_toggled.connect(self._on_record_toggled)
        self.main_window.record_cancelled.connect(self.cancel_recording)
        self.main_window.model_changed.connect(self._on_model_changed)
        self.main_window.settings_requested.connect(self.open_settings_dialog)
        self.main_window.hotkeys_requested.connect(self.open_hotkey_dialog)
        self.main_window.overlay_toggle_requested.connect(self.toggle_overlay)
        self.main_window.test_overlay_requested.connect(self._on_test_overlay_requested)
        self.main_window.about_requested.connect(self.show_about_dialog)
        self.main_window.retranscribe_requested.connect(self._on_retranscribe_requested)
        self.main_window.upload_audio_requested.connect(self.open_upload_audio_dialog)

        # Set up the copied animation callback
        self.main_window.on_show_copied_animation = self.show_copied_animation

        # Tray manager signals
        self.tray_manager.show_requested.connect(self._on_tray_show)
        self.tray_manager.hide_requested.connect(self._on_tray_hide)
        self.tray_manager.exit_requested.connect(self._on_tray_exit)
        self.tray_manager.toggle_recording.connect(self._on_tray_toggle_recording)

        # Overlay signals
        self.overlay.state_changed.connect(self._on_overlay_state_changed)

        # Internal signals
        self.record_started.connect(self._show_recording_overlay)
        self.record_stopped.connect(self._show_processing_overlay)
        self.transcription_received.connect(self._display_transcript)
        self.status_changed.connect(self._apply_status_to_main_window)
        self.audio_levels_updated.connect(self._apply_audio_levels_to_overlay)

    def _on_record_toggled(self, is_recording: bool):
        """Handle record button toggle from main window."""
        if is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def _on_model_changed(self, model_name: str):
        """Handle model selection change."""
        logger.info(f"Model changed to: {model_name}")
        if self.on_model_changed:
            self.on_model_changed(model_name)
        self.model_changed.emit(model_name)

    def _on_tray_show(self):
        """Handle show from tray."""
        self.main_window.showNormal()
        logger.debug("Window shown from tray")

    def _on_tray_hide(self):
        """Handle hide from tray."""
        self.main_window.hide()
        logger.debug("Window hidden to tray")

    def _on_tray_exit(self):
        """Handle exit from tray."""
        logger.info("Exit requested from tray")

    def _on_tray_toggle_recording(self):
        """Handle toggle recording from tray."""
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def _on_overlay_state_changed(self, state: str):
        """Handle overlay state change."""
        logger.debug(f"Overlay state changed to: {state}")

    def _show_recording_overlay(self):
        """Show the waveform overlay in recording state."""
        self.tray_manager.set_recording(True)
        if not self.overlay.isVisible():
            self.overlay.show_at_cursor(self.overlay.STATE_RECORDING)
        else:
            self.overlay.set_state(self.overlay.STATE_RECORDING)

    def _show_processing_overlay(self):
        """Show the waveform overlay in processing state."""
        self.tray_manager.set_recording(False)
        if not self.overlay.isVisible():
            self.overlay.show_at_cursor(self.overlay.STATE_PROCESSING)
        else:
            self.overlay.set_state(self.overlay.STATE_PROCESSING)

    def _display_transcript(self, text: str):
        """Display the completed transcript in the main window."""
        self.main_window.set_transcript(text)
        self.hide_overlay()

    def _apply_status_to_main_window(self, status: str):
        """Forward a status string to the main window status label."""
        self.main_window.set_status(status)

    def _apply_audio_levels_to_overlay(self, levels: List[float]):
        """Forward audio level updates to the waveform overlay."""
        self.overlay.update_audio_levels(levels)

    def start_recording(self):
        """Start recording."""
        self.is_recording = True
        logger.info("Recording started")

        # Sync main window state (important for hotkey-triggered recordings)
        if not self.main_window.is_recording:
            self.main_window.is_recording = True
            self.main_window._update_recording_state()

        if self.on_record_start:
            self.on_record_start()

        self.record_started.emit()

    def stop_recording(self):
        """Stop recording."""
        self.is_recording = False
        logger.info("Recording stopped")

        # Sync main window state (important for hotkey-triggered recordings)
        if self.main_window.is_recording:
            self.main_window.is_recording = False
            self.main_window._update_recording_state()
            logger.info("Main window recording state updated")

        if self.on_record_stop:
            self.on_record_stop()

        self.record_stopped.emit()

    def cancel_recording(self):
        """Cancel recording."""
        self.is_recording = False
        logger.info("Recording cancelled")

        # Sync main window state (important for hotkey-triggered cancellations)
        if self.main_window.is_recording:
            self.main_window.is_recording = False
            self.main_window._update_recording_state()
            logger.info("Main window recording state updated")

        if self.on_record_cancel:
            self.on_record_cancel()
            logger.info("Record cancel callback called")

        self.record_cancelled.emit()
        self.main_window.clear_transcription()
        self._start_cancel_animation()
        logger.info("Cancel animation started")

    def set_transcript(self, text: str):
        """Set transcript text."""
        self.transcription_received.emit(text)

    def set_device_info(self, device_info: str):
        """Set the persistent device info display (e.g., 'cuda (float16)').

        Args:
            device_info: Device information string to display.
        """
        self.main_window.set_device_info(device_info)

    def set_transcription_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int
    ):
        """Set the transcription statistics display.

        Args:
            transcription_time: Time taken to transcribe in seconds.
            audio_duration: Duration of the audio in seconds.
            file_size: Size of the audio file in bytes.
        """
        self.main_window.set_transcription_stats(
            transcription_time, audio_duration, file_size
        )

    def clear_transcription_stats(self):
        """Clear and hide the transcription statistics display."""
        self.main_window.clear_transcription_stats()

    def set_status(self, status: str):
        """Update the status text and infer the overlay state from it.

        Legacy convenience for callers that only emit text. New code should
        call `set_overlay_state` separately for explicit control.
        """
        self.status_changed.emit(status)
        inferred_state = _status_to_overlay_state(status)
        if inferred_state is not None:
            self.set_overlay_state(inferred_state)

    def set_overlay_state(self, state: OverlayState) -> None:
        """Route an explicit overlay-state change to the correct overlay component.

        Centralizes all "show waveform vs streaming overlay vs hide everything"
        logic in one place.
        """
        if state is OverlayState.CANCELLING:
            self._start_cancel_animation()
            return

        if state is OverlayState.NONE:
            self.hide_overlay()
            self.hide_streaming_overlay()
            self.hide_caret_paste_indicator()
            self.streaming_flow_active = False
            return

        streaming_overlay_visible = self.streaming_overlay.isVisible()
        streaming_active = streaming_overlay_visible or self.streaming_flow_active

        if state is OverlayState.RECORDING:
            if not streaming_active:
                self._show_or_set_overlay(self.overlay.STATE_RECORDING)
        elif state is OverlayState.PROCESSING:
            if streaming_overlay_visible:
                self.set_streaming_overlay_finalizing()
            elif not streaming_active:
                self._show_or_set_overlay(self.overlay.STATE_PROCESSING)
        elif state is OverlayState.TRANSCRIBING:
            if streaming_overlay_visible:
                self.set_streaming_overlay_finalizing()
            elif not streaming_active:
                self._show_or_set_overlay(self.overlay.STATE_TRANSCRIBING)
        elif state is OverlayState.STT_ENABLED:
            self._show_or_set_overlay(self.overlay.STATE_STT_ENABLE)
        elif state is OverlayState.STT_DISABLED:
            self._show_or_set_overlay(self.overlay.STATE_STT_DISABLE)

    def _show_or_set_overlay(self, overlay_state: str) -> None:
        """Show overlay at cursor if hidden, otherwise just transition its state."""
        if not self.overlay.isVisible():
            self.overlay.show_at_cursor(overlay_state)
        else:
            self.overlay.set_state(overlay_state)

    def update_audio_levels(self, levels: List[float]):
        """Update audio level display."""
        self.audio_levels = levels
        self.audio_levels_updated.emit(levels)

    def show_overlay(self):
        """Show the overlay."""
        self.overlay.show_at_cursor()

    def hide_overlay(self):
        """Hide the overlay."""
        self.overlay.hide()

    def show_copied_animation(self):
        """Show the copied to clipboard animation overlay."""
        self.overlay.show_at_cursor(self.overlay.STATE_COPIED)

    def toggle_overlay(self):
        """Toggle the overlay visibility."""
        if self.overlay.isVisible():
            self.hide_overlay()
        else:
            self.show_overlay()

    # Streaming text overlay methods
    def show_streaming_overlay(self):
        """Show the streaming text overlay at saved position or screen center."""
        self.streaming_flow_active = True
        self.streaming_overlay.clear_text()
        self.streaming_overlay.show_overlay()
        self.hide_overlay()
        logger.debug("Streaming overlay shown")

    def update_streaming_text(self, text: str, is_final: bool):
        """Update the streaming text display.

        Args:
            text: The transcription text chunk
            is_final: Whether this chunk is finalized
        """
        self.streaming_overlay.update_streaming_text(text, is_final)

    def hide_streaming_overlay(self):
        """Hide the streaming text overlay with animation."""
        if self.streaming_overlay.isVisible():
            self.streaming_overlay.hide_with_animation()
            logger.debug("Streaming overlay hiding")

    def set_streaming_overlay_finalizing(self):
        """Set the streaming overlay to finalizing state."""
        self.streaming_overlay.set_state(self.streaming_overlay.STATE_FINALIZING)

    def show_caret_paste_indicator(self):
        """Show the caret paste indicator."""
        self.caret_paste_indicator.show_indicator()
        logger.debug("Caret paste indicator shown")

    def hide_caret_paste_indicator(self):
        """Hide the caret paste indicator."""
        self.caret_paste_indicator.hide_indicator()
        logger.debug("Caret paste indicator hidden")

    def _on_test_overlay_requested(self, state: str):
        """Handle test overlay state request from menu."""
        logger.info(f"Testing overlay state: {state}")

        # Map state string to overlay state constant
        state_map = {
            "recording": self.overlay.STATE_RECORDING,
            "processing": self.overlay.STATE_PROCESSING,
            "transcribing": self.overlay.STATE_TRANSCRIBING,
            "cancelling": self.overlay.STATE_CANCELLING,
            "stt_enable": self.overlay.STATE_STT_ENABLE,
            "stt_disable": self.overlay.STATE_STT_DISABLE,
            "copied": self.overlay.STATE_COPIED,
            "large_file_splitting": self.overlay.STATE_LARGE_FILE_SPLITTING,
            "large_file_processing": self.overlay.STATE_LARGE_FILE_PROCESSING,
        }

        overlay_state = state_map.get(state)
        if overlay_state:
            # Set test file info for large file states
            if state in ["large_file_splitting", "large_file_processing"]:
                self.overlay.set_large_file_info(42.5)  # Test with 42.5 MB

            self.overlay.show_at_cursor(overlay_state)
        else:
            logger.warning(f"Unknown overlay state: {state}")

    def _start_cancel_animation(self):
        """Show the cancel animation and schedule hide."""
        self.cancel_animation_timer.stop()
        self.hide_caret_paste_indicator()

        # Use streaming overlay's cancel animation if it's visible
        if self.streaming_overlay.isVisible():
            self.streaming_overlay.show_cancel_animation()
            self.streaming_flow_active = False
            # Streaming overlay handles its own hide after animation
            return

        self.streaming_flow_active = False

        # Use waveform overlay cancel animation for non-streaming case
        if not self.overlay.isVisible():
            self.overlay.show_at_cursor(self.overlay.STATE_CANCELLING)
        else:
            self.overlay.set_state(self.overlay.STATE_CANCELLING)

        self.cancel_animation_timer.start(
            config.CANCELLATION_ANIMATION_DURATION_MS + config.CANCELLATION_GRACE_MS
        )

    def _on_cancel_animation_finished(self):
        """Cleanup after cancel animation completes."""
        if self.overlay.current_state not in {
            self.overlay.STATE_CANCELLING,
            self.overlay.STATE_IDLE
        }:
            return
        self.hide_overlay()

    def show_main_window(self):
        """Show the main window."""
        self.main_window.showNormal()
        self.main_window.raise_()
        self.main_window.activateWindow()

    def hide_main_window(self):
        """Hide the main window."""
        self.main_window.hide()

    def open_settings_dialog(self):
        """Open the settings dialog."""
        dialog = SettingsDialog(self.main_window)
        # Connect hotkey button in settings to hotkey dialog
        dialog.tabs.setCurrentIndex(0)  # Default to general

        # Connect settings changed signal
        def on_settings_changed(settings: dict):
            if settings.get('_whisper_settings_changed', False):
                if self.on_whisper_settings_changed:
                    self.on_whisper_settings_changed()
            if settings.get('_audio_device_changed', False):
                if self.on_audio_device_changed:
                    new_device_id = settings.get('audio_input_device')
                    self.on_audio_device_changed(new_device_id)
            if settings.get('_streaming_settings_changed', False):
                if self.on_streaming_settings_changed:
                    self.on_streaming_settings_changed()

        dialog.settings_changed.connect(on_settings_changed)
        dialog.exec()

    def open_hotkey_dialog(self):
        """Open the hotkey configuration dialog."""
        dialog = HotkeyDialog(self.main_window)

        def on_hotkeys_save(hotkeys):
            if self.on_hotkeys_changed:
                self.on_hotkeys_changed(hotkeys)
            # Update the hotkey display in the main window
            self.update_hotkey_display(hotkeys)

        dialog.on_hotkeys_save = on_hotkeys_save
        dialog.exec()

    def open_upload_audio_dialog(self):
        """Open file dialog to select an audio file for transcription."""
        logger.info("Opening upload audio dialog")

        # Define supported audio formats
        audio_filters = "Audio Files (*.wav *.mp3 *.m4a *.ogg *.flac *.wma);;WAV Files (*.wav);;MP3 Files (*.mp3);;All Files (*.*)"

        file_path, _ = QFileDialog.getOpenFileName(
            self.main_window,
            "Select Audio File",
            "",
            audio_filters
        )

        if not file_path:
            logger.info("Audio file selection cancelled")
            return

        logger.info(f"Selected audio file: {file_path}")

        # Analyze the file and show preview
        try:
            preview = audio_processor.preview_file(file_path)

            # Show preview dialog
            dialog = UploadPreviewDialog(preview, self.main_window)
            dialog.on_proceed = self._handle_upload_audio
            dialog.exec()

        except FileNotFoundError as e:
            logger.error(f"File not found: {e}")
            QMessageBox.warning(
                self.main_window,
                "File Not Found",
                f"The selected file could not be found:\n{file_path}"
            )
        except ValueError as e:
            logger.error(f"Invalid audio file: {e}")
            QMessageBox.warning(
                self.main_window,
                "Invalid Audio File",
                f"The selected file could not be read as audio.\n\nError: {e}"
            )
        except Exception as e:
            logger.error(f"Error analyzing audio file: {e}")
            QMessageBox.critical(
                self.main_window,
                "Error",
                f"Failed to analyze audio file:\n{e}"
            )

    def _handle_upload_audio(self, audio_path: str):
        """Handle the audio file upload after user confirms in preview dialog.

        Args:
            audio_path: Path to the audio file to transcribe.
        """
        logger.info(f"Processing uploaded audio: {audio_path}")
        if self.on_upload_audio:
            self.on_upload_audio(audio_path)

    def get_quick_record_tab(self) -> QuickRecordTab:
        """Get the Quick Record tab widget.

        Returns:
            The QuickRecordTab instance
        """
        return self.main_window.quick_record_tab

    def switch_to_tab(self, index: int):
        """Switch to a specific tab.

        Args:
            index: Tab index.
        """
        self.main_window.tabbed_content.set_current_index(index)

    def switch_to_quick_record(self):
        """Switch to the Quick Record tab."""
        self.switch_to_tab(TabbedContentWidget.TAB_QUICK_RECORD)

    def update_hotkey_display(self, hotkeys: dict):
        """
        Update the hotkey display in the main window.

        Args:
            hotkeys: Dictionary with hotkey mappings
        """
        record_key = hotkeys.get('record_toggle', '*')
        cancel_key = hotkeys.get('cancel', '-')
        enable_disable_key = hotkeys.get('enable_disable', 'Ctrl+Alt+*')
        self.main_window.update_hotkeys(record_key, cancel_key, enable_disable_key)

    def show_about_dialog(self):
        """Show the about dialog."""
        QMessageBox.about(
            self.main_window,
            "About OpenWhisper",
            "OpenWhisper - Speech-to-Text Application\n\n"
            "Record audio and turn it into text. Works offline with local Whisper or online with OpenAI.\n\n"
            "Features:\n"
            "• Local or cloud transcription\n"
            "• Global hotkeys (press * to record)\n"
            "• Cool waveform visualizations\n"
            "• Auto-pastes text for you\n"
            "• Runs in the background\n\n"
            "Open source and free to use."
        )

    def get_model_value(self) -> str:
        """Get the selected model value."""
        return self.main_window.get_model_value()

    def refresh_history(self):
        """Refresh the history sidebar."""
        self.main_window.refresh_history()

    def _on_retranscribe_requested(self, audio_path: str):
        """Handle re-transcription request from main window signal."""
        self._handle_retranscribe(audio_path)

    def _handle_retranscribe(self, audio_path: str):
        """Handle re-transcription request."""
        logger.info(f"Re-transcribe requested: {audio_path}")
        if self.on_retranscribe:
            self.on_retranscribe(audio_path)

    def cleanup(self):
        """Cleanup resources."""
        logger.info("Starting UI Controller cleanup...")

        # Stop the cancel animation timer
        try:
            if self.cancel_animation_timer.isActive():
                self.cancel_animation_timer.stop()
        except Exception as e:
            logger.debug(f"Error stopping cancel animation timer: {e}")

        # Stop overlay timer and close
        try:
            if hasattr(self.overlay, 'timer') and self.overlay.timer.isActive():
                self.overlay.timer.stop()
            self.overlay.close()
        except Exception as e:
            logger.debug(f"Error closing overlay: {e}")

        # Cleanup streaming overlay
        try:
            if hasattr(self, 'streaming_overlay'):
                self.streaming_overlay.cleanup()
        except Exception as e:
            logger.debug(f"Error closing streaming overlay: {e}")

        try:
            if hasattr(self, 'caret_paste_indicator'):
                self.caret_paste_indicator.hide_indicator()
                self.caret_paste_indicator.close()
        except Exception as e:
            logger.debug(f"Error closing caret indicator: {e}")

        # Hide and cleanup system tray
        try:
            self.tray_manager.hide()
            self.tray_manager.setParent(None)
        except Exception as e:
            logger.debug(f"Error hiding system tray: {e}")

        # Close main window (force quit to bypass minimize to tray)
        try:
            self.main_window._force_quit = True
            self.main_window.close()
        except Exception as e:
            logger.debug(f"Error closing main window: {e}")

        logger.info("UI Controller cleaned up")
