"""
UI Controller for PyQt6 Application.
Manages the main window, overlay, and dialogs.
Bridges between UI and application logic.
"""
import logging
from typing import Callable, List, Optional
from PyQt6.QtCore import QTimer, pyqtSignal, QObject
from PyQt6.QtWidgets import QMessageBox

from config import config
from services.hotkey_manager import format_hotkey_display
from ui_qt.overlay_state import OverlayState
from ui_qt.main_window import MainWindow
from ui_qt.overlays import CaretPasteIndicator, WaveformOverlay
from ui_qt.system_tray import SystemTrayManager
from ui_qt.dialogs.settings_dialog import SettingsDialog
from ui_qt.dialogs.hotkey_dialog import HotkeyDialog
from ui_qt.widgets import QuickRecordTab, UploadFileTab, TabbedContentWidget
from services.settings import SettingsKey

logger = logging.getLogger(__name__)


class UIController(QObject):
    """Controls the UI components and manages their interactions."""

    # Signals
    record_started = pyqtSignal()
    record_stopped = pyqtSignal()
    record_canceled = pyqtSignal()
    model_changed = pyqtSignal(str)
    transcription_received = pyqtSignal(str, object)  # fixed text, optional raw
    status_changed = pyqtSignal(str)
    audio_levels_updated = pyqtSignal(list)

    def __init__(self):
        """Initialize UI controller."""
        super().__init__()

        # Create UI components
        self.main_window = MainWindow()
        self.overlay = WaveformOverlay()
        self.caret_paste_indicator = CaretPasteIndicator()
        self.tray_manager = SystemTrayManager(self.main_window)

        # State
        self.is_recording = False
        self.audio_levels: List[float] = [0.0] * 20
        self.streaming_flow_active = False
        self._transcription_source_tab: int = TabbedContentWidget.TAB_QUICK_RECORD

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
        self.on_hf_policy_changed: Optional[Callable] = None  # Callback for HuggingFace access policy change
        self.on_model_download_requested: Optional[Callable] = None  # Model Manager: fetch a model
        self.on_model_delete_requested: Optional[Callable] = None  # Model Manager: delete cached files
        self.get_loaded_local_model: Optional[Callable] = None  # Provider: currently loaded model name

        # Non-modal Model Manager dialog (single instance, created lazily)
        self._model_manager_dialog = None

        # Timer to hide overlay after cancel animation completes
        self.cancel_animation_timer = QTimer()
        self.cancel_animation_timer.setSingleShot(True)
        self.cancel_animation_timer.timeout.connect(self._on_cancel_animation_finished)

        self._setup_connections()

    def _setup_connections(self):
        """Setup signal connections between UI components."""
        # Main window signals
        self.main_window.record_toggled.connect(self._on_record_toggled)
        self.main_window.record_canceled.connect(self.cancel_recording)
        self.main_window.model_changed.connect(self._on_model_changed)
        self.main_window.whisper_engine_changed.connect(self._on_whisper_engine_changed)
        self.main_window.settings_requested.connect(self.open_settings_dialog)
        self.main_window.model_manager_requested.connect(self.open_model_manager_dialog)
        self.main_window.hotkeys_requested.connect(self.open_hotkey_dialog)
        self.main_window.about_requested.connect(self.show_about_dialog)
        self.main_window.retranscribe_requested.connect(self._on_retranscribe_requested)
        self.main_window.upload_file_requested.connect(self._on_upload_file_transcribe)

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

    def _on_whisper_engine_changed(self):
        """Handle a local-engine (model/device/quant) change from the main GUI.

        Reuses the same reload hook the Settings dialog fires; the controller's
        handler runs the reload on a background thread.
        """
        logger.info("Local engine settings changed via main GUI")
        if self.on_whisper_settings_changed:
            self.on_whisper_settings_changed()

    def _on_tray_show(self):
        """Handle show from tray."""
        self.main_window.restore_from_tray()
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

    def _display_transcript(self, text: str, raw=None):
        """Display the completed transcript in the tab that started transcription."""
        if self._transcription_source_tab == TabbedContentWidget.TAB_UPLOAD_FILE:
            self.main_window.upload_file_tab.set_transcript(text, raw=raw)
        else:
            self.main_window.set_transcript(text, raw=raw)
        self.hide_overlay()

    def _apply_status_to_main_window(self, status: str):
        """Forward a status string to the active transcription tab."""
        if self._transcription_source_tab == TabbedContentWidget.TAB_UPLOAD_FILE:
            self.main_window.upload_file_tab.set_status(status)
        else:
            self.main_window.set_status(status)

    def _apply_audio_levels_to_overlay(self, levels: List[float]):
        """Forward audio level updates to the waveform overlay."""
        self.overlay.update_audio_levels(levels)

    def start_recording(self):
        """Start recording."""
        self.is_recording = True
        self._transcription_source_tab = TabbedContentWidget.TAB_QUICK_RECORD
        logger.info("Recording started")

        # Sync main window state (important for hotkey-triggered recordings)
        if not self.main_window.is_recording:
            self.main_window.is_recording = True
            self.main_window._update_recording_state()

        if self.on_record_start:
            self.on_record_start()
        else:
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
        else:
            self.record_stopped.emit()

    def cancel_recording(self):
        """Cancel recording."""
        self.is_recording = False
        logger.info("Recording canceled")

        # Sync main window state (important for hotkey-triggered cancellations)
        if self.main_window.is_recording:
            self.main_window.is_recording = False
            self.main_window._update_recording_state()
            logger.info("Main window recording state updated")

        if self.on_record_cancel:
            self.on_record_cancel()
            logger.info("Record cancel callback called")

        self.record_canceled.emit()
        self.main_window.clear_transcription()

    def set_transcript(self, text: str, raw=None):
        """Set transcript text.

        Args:
            text: Fixed/display transcript.
            raw: Optional unprocessed ASR text when distinct from ``text``.
        """
        self.transcription_received.emit(text, raw)

    def set_device_info(self, device_info: str):
        """Set the persistent device info display (e.g., 'cuda (float16)').

        Args:
            device_info: Device information string to display.
        """
        self.main_window.set_device_info(device_info)

    def set_engine_busy(self, busy: bool):
        """Disable/enable the inline local-engine combos during a reload.

        When the engine becomes idle again, refresh the Model Manager so its
        Delete lock tracks the newly loaded model — not the previous one left
        from the pre-reload refresh after Set Active.

        Args:
            busy: True to disable combos while the engine reloads, else False.
        """
        self.main_window.quick_record_tab.local_engine.set_busy(busy)
        self.main_window.upload_file_tab.local_engine.set_busy(busy)
        if not busy:
            self.refresh_model_manager()

    def set_transcription_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int
    ):
        """Set the transcription statistics display on the active transcription tab."""
        if self._transcription_source_tab == TabbedContentWidget.TAB_UPLOAD_FILE:
            self.main_window.upload_file_tab.set_transcription_stats(
                transcription_time, audio_duration, file_size
            )
        else:
            self.main_window.set_transcription_stats(
                transcription_time, audio_duration, file_size
            )

    def clear_transcription_stats(self):
        """Clear and hide the transcription statistics display."""
        self.main_window.clear_transcription_stats()
        self.main_window.upload_file_tab.clear_transcription_stats()

    def set_status(self, status: str):
        """Update only the human-readable status text."""
        self.status_changed.emit(status)

    def set_overlay_state(self, state: OverlayState) -> None:
        """Route an explicit overlay-state change to the correct overlay component.

        Centralizes all "show waveform vs streaming overlay vs hide everything"
        logic in one place.
        """
        if state is OverlayState.CANCELING:
            self.tray_manager.set_recording(False)
            self._start_cancel_animation()
            return

        if state is OverlayState.NONE:
            self.tray_manager.set_recording(False)
            self.hide_overlay()
            self.hide_streaming_overlay()
            self.hide_caret_paste_indicator()
            self.streaming_flow_active = False
            return

        if state is OverlayState.RECORDING:
            self.tray_manager.set_recording(True)
            if self.streaming_flow_active:
                # Live preview uses the same cursor-anchored waveform chrome.
                if not self.overlay.isVisible():
                    self.overlay.show_at_cursor(self.overlay.STATE_STREAMING)
                elif self.overlay.current_state != self.overlay.STATE_STREAMING:
                    self.overlay.set_state(self.overlay.STATE_STREAMING)
            else:
                self._show_or_set_overlay(self.overlay.STATE_RECORDING)
        elif state is OverlayState.PROCESSING:
            self.tray_manager.set_recording(False)
            self._dismiss_streaming_preview_for_waveform(self.overlay.STATE_PROCESSING)
        elif state is OverlayState.TRANSCRIBING:
            self.tray_manager.set_recording(False)
            self._dismiss_streaming_preview_for_waveform(self.overlay.STATE_TRANSCRIBING)
        elif state is OverlayState.CLEANING:
            self.tray_manager.set_recording(False)
            self._dismiss_streaming_preview_for_waveform(self.overlay.STATE_CLEANING)
        elif state is OverlayState.STT_ENABLED:
            self._show_or_set_overlay(self.overlay.STATE_STT_ENABLE)
        elif state is OverlayState.STT_DISABLED:
            self._show_or_set_overlay(self.overlay.STATE_STT_DISABLE)

    def _dismiss_streaming_preview_for_waveform(self, waveform_state: str) -> None:
        """Clear live preview text and show the classic waveform state.

        Args:
            waveform_state: Waveform overlay state to show (processing/transcribing).
        """
        self.streaming_flow_active = False
        self.hide_caret_paste_indicator()
        self.overlay.clear_streaming_text()
        self._show_or_set_overlay(waveform_state)

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

    # Streaming preview methods (reuse the cursor-anchored waveform overlay)
    def show_streaming_overlay(self):
        """Show live preview on the waveform overlay near the cursor."""
        self.streaming_flow_active = True
        self.overlay.clear_streaming_text()
        self.overlay.show_at_cursor(self.overlay.STATE_STREAMING)
        logger.debug("Streaming preview shown on waveform overlay")

    def update_streaming_text(self, text: str, is_final: bool):
        """Update the streaming text display.

        Args:
            text: The transcription text chunk
            is_final: Whether this chunk is finalized
        """
        self.overlay.update_streaming_text(text, is_final)

    def hide_streaming_overlay(self):
        """Clear streaming preview mode on the waveform overlay."""
        self.streaming_flow_active = False
        self.overlay.clear_streaming_text()
        logger.debug("Streaming preview cleared")

    def set_streaming_overlay_finalizing(self):
        """No-op kept for compatibility; processing uses the waveform states."""
        return

    def show_caret_paste_indicator(self):
        """Show the caret paste indicator."""
        self.caret_paste_indicator.show_indicator()
        logger.debug("Caret paste indicator shown")

    def hide_caret_paste_indicator(self):
        """Hide the caret paste indicator."""
        self.caret_paste_indicator.hide_indicator()
        logger.debug("Caret paste indicator hidden")

    def _start_cancel_animation(self):
        """Show the cancel animation and schedule hide."""
        self.cancel_animation_timer.stop()
        self.hide_caret_paste_indicator()
        self.streaming_flow_active = False
        self.overlay.clear_streaming_text()

        # Use waveform overlay cancel animation
        if not self.overlay.isVisible():
            self.overlay.show_at_cursor(self.overlay.STATE_CANCELING)
        else:
            self.overlay.set_state(self.overlay.STATE_CANCELING)

        self.cancel_animation_timer.start(
            config.CANCELLATION_ANIMATION_DURATION_MS + config.CANCELLATION_GRACE_MS
        )

    def _on_cancel_animation_finished(self):
        """Cleanup after cancel animation completes."""
        if self.overlay.current_state not in {
            self.overlay.STATE_CANCELING,
            self.overlay.STATE_IDLE
        }:
            return
        self.hide_overlay()

    def show_main_window(self):
        """Show the main window."""
        self.main_window.restore_from_tray()

    def hide_main_window(self):
        """Hide the main window."""
        self.main_window.hide()

    def open_settings_dialog(self, focus_hf_policy: bool = False):
        """Open the settings dialog.

        Args:
            focus_hf_policy: When True, open directly to the Advanced tab with
                the Hugging Face download-policy control focused (used by the
                consent dialog's "Open Settings" action).
        """
        dialog = SettingsDialog(self.main_window)
        if focus_hf_policy:
            dialog.focus_hf_policy()
        else:
            dialog.tabs.setCurrentIndex(0)  # Default to general

        # Connect settings changed signal
        def on_settings_changed(settings: dict):
            self.overlay.refresh_streaming_font_size()
            # Keep main-UI AI cleanup checkboxes in sync with Settings.
            self.refresh_cleanup_controls()
            if settings.get('_audio_device_changed', False):
                if self.on_audio_device_changed:
                    new_device_id = settings.get(SettingsKey.AUDIO_INPUT_DEVICE)
                    self.on_audio_device_changed(new_device_id)
            if settings.get('_streaming_settings_changed', False):
                if self.on_streaming_settings_changed:
                    self.on_streaming_settings_changed()
            if settings.get('_hf_policy_changed', False):
                if self.on_hf_policy_changed:
                    self.on_hf_policy_changed(
                        settings.get(SettingsKey.HF_ACCESS_POLICY)
                    )

        dialog.settings_changed.connect(on_settings_changed)
        dialog.exec()

    def refresh_local_engine_controls(self):
        """Re-sync the inline local-engine combos with the persisted settings.

        Signal-safe: the combos are updated with signals blocked, so this never
        triggers another engine reload.
        """
        self.main_window.quick_record_tab.local_engine.load_from_settings()
        self.main_window.upload_file_tab.local_engine.load_from_settings()

    def refresh_cleanup_controls(self):
        """Re-sync the main-UI AI cleanup checkboxes with persisted settings."""
        self.main_window.quick_record_tab.load_cleanup_setting()
        self.main_window.upload_file_tab.load_cleanup_setting()

    def show_hf_consent_dialog(
        self, model_name: str, policy: str, env_blocked: bool = False
    ) -> str:
        """Show the Hugging Face download consent dialog (main thread only).

        Args:
            model_name: Resolved model name that needs downloading.
            policy: Current HuggingFaceAccessPolicy value.
            env_blocked: True when HF_HUB_OFFLINE disables downloads entirely.

        Returns:
            One of the ``ConsentAction`` values chosen by the user.
        """
        from ui_qt.dialogs.hf_consent_dialog import HuggingFaceConsentDialog

        dialog = HuggingFaceConsentDialog(
            model_name, policy, env_blocked=env_blocked, parent=self.main_window
        )
        dialog.exec()
        return dialog.result_action

    def open_model_manager_dialog(self):
        """Show the non-modal Model Manager (single instance, re-raised)."""
        from ui_qt.dialogs.model_manager_dialog import ModelManagerDialog

        if self._model_manager_dialog is None:
            dialog = ModelManagerDialog(
                get_loaded_model=self.get_loaded_local_model,
                parent=self.main_window,
            )
            dialog.on_download_requested = self.on_model_download_requested
            dialog.on_delete_requested = self.on_model_delete_requested
            dialog.on_set_active_requested = self._on_manager_set_active
            self._model_manager_dialog = dialog

        self._model_manager_dialog.refresh()
        self._model_manager_dialog.show()
        self._model_manager_dialog.raise_()
        self._model_manager_dialog.activateWindow()

    def _on_manager_set_active(self, model_name: str):
        """Persist a Model Manager 'Set Active' choice and reload the engine.

        Identical contract to ``LocalEngineControls._on_changed``: write the
        setting, re-sync the inline combos (signal-safe), then fire the same
        debounced background reload the combos use.
        """
        from services.settings import settings_manager

        settings_manager.save_setting(SettingsKey.WHISPER_MODEL, model_name)
        self.refresh_local_engine_controls()
        if self.on_whisper_settings_changed:
            self.on_whisper_settings_changed()

    def refresh_model_manager(self):
        """Refresh the Model Manager if it is open (cache-changed target)."""
        if self._model_manager_dialog is not None and self._model_manager_dialog.isVisible():
            self._model_manager_dialog.refresh()

    def on_model_download_started(self, model_name: str):
        """Reflect a started model download in the Model Manager."""
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.set_downloading(model_name)

    def on_model_download_finished(self, model_name: str, success: bool):
        """Reflect a finished model download in the Model Manager."""
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.finish_download(model_name, success)

    def on_model_deleted(self, model_name: str, success: bool, error: str):
        """Reflect a model delete outcome in the Model Manager."""
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.show_delete_result(model_name, success, error)

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

    def _on_upload_file_transcribe(self, audio_path: str):
        """Handle Transcribe from the Upload File tab."""
        self._transcription_source_tab = TabbedContentWidget.TAB_UPLOAD_FILE
        logger.info(f"Upload tab transcription started: {audio_path}")
        if self.on_upload_audio:
            self.on_upload_audio(audio_path)

    def get_quick_record_tab(self) -> QuickRecordTab:
        """Get the Quick Record tab widget.

        Returns:
            The QuickRecordTab instance
        """
        return self.main_window.quick_record_tab

    def get_upload_file_tab(self) -> UploadFileTab:
        """Get the Upload File tab widget."""
        return self.main_window.upload_file_tab

    def switch_to_tab(self, index: int):
        """Switch to a specific tab.

        Args:
            index: Tab index.
        """
        self.main_window.tabbed_content.set_current_index(index)

    def switch_to_quick_record(self):
        """Switch to the Quick Record tab."""
        self.switch_to_tab(TabbedContentWidget.TAB_QUICK_RECORD)

    def switch_to_upload_file(self):
        """Switch to the Upload File tab."""
        self.switch_to_tab(TabbedContentWidget.TAB_UPLOAD_FILE)

    def update_hotkey_display(self, hotkeys: dict):
        """
        Update the hotkey display in the main window.

        Args:
            hotkeys: Dictionary with hotkey mappings
        """
        record_key = format_hotkey_display(
            hotkeys.get('record_toggle', config.DEFAULT_HOTKEYS['record_toggle'])
        )
        cancel_key = format_hotkey_display(
            hotkeys.get('cancel', config.DEFAULT_HOTKEYS['cancel'])
        )
        enable_disable_key = format_hotkey_display(
            hotkeys.get('enable_disable', config.DEFAULT_HOTKEYS['enable_disable'])
        )
        minimize_key = format_hotkey_display(
            hotkeys.get('minimize_tray', config.DEFAULT_HOTKEYS['minimize_tray'])
        )
        self.main_window.update_hotkeys(
            record_key, cancel_key, enable_disable_key, minimize_key
        )

    def show_about_dialog(self):
        """Show the about dialog."""
        QMessageBox.about(
            self.main_window,
            "About OpenWhisper",
            "<p><b>OpenWhisper - Speech-to-Text Application</b></p>"
            "<p>Record audio and turn it into text. Works offline with local "
            "Whisper or online with OpenAI.</p>"
            "<p>Features:<br>"
            "&bull; Local or cloud transcription<br>"
            "&bull; Global hotkeys (press * to record)<br>"
            "&bull; Cool waveform visualizations<br>"
            "&bull; Auto-pastes text for you<br>"
            "&bull; Runs in the background</p>"
            '<p>Website: <a href="https://openwhisper.fiorilabs.tech/">'
            "openwhisper.fiorilabs.tech</a><br>"
            "Open source and free to use.</p>"
        )

    def get_model_value(self) -> str:
        """Get the selected model value."""
        return self.main_window.get_model_value()

    def refresh_history(self):
        """Refresh the history sidebar."""
        self.main_window.refresh_history()

    def _on_retranscribe_requested(self, audio_path: str):
        """Handle re-transcription request from main window signal."""
        self._request_retranscription(audio_path)

    def _request_retranscription(self, audio_path: str):
        """Request re-transcription for an existing audio file."""
        logger.info("Re-transcribe requested: %s", audio_path)
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
