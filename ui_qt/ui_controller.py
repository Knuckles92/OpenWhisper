import logging
from typing import Any, Callable, Dict, List, Optional
from PyQt6.QtCore import QTimer, QUrl, pyqtSignal, QObject
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QMessageBox

from config import config
from services.app_update import (
    UpdateCheckResult,
    UpdateStatus,
    channel_label,
    detect_channel,
    should_auto_notify,
)
from services.hotkey_manager import format_hotkey_display
from ui_qt.overlay_state import OverlayState
from ui_qt.main_window import MainWindow
from ui_qt.overlays import CaretPasteIndicator, WaveformOverlay
from ui_qt.system_tray import SystemTrayManager
from ui_qt.dialogs.app_update_dialog import AppUpdateDialog
from ui_qt.dialogs.settings_dialog import SettingsDialog
from ui_qt.dialogs.hotkey_dialog import HotkeyDialog
from ui_qt.widgets import TabbedContentWidget
from services.settings import SettingsKey, settings_manager

logger = logging.getLogger(__name__)


class UIController(QObject):
    record_started = pyqtSignal()
    record_stopped = pyqtSignal()
    transcription_received = pyqtSignal(str, object)  # fixed text, optional raw
    status_changed = pyqtSignal(str)
    audio_levels_updated = pyqtSignal(list)

    def __init__(self):
        super().__init__()

        self.main_window = MainWindow()
        self.overlay = WaveformOverlay()
        self.caret_paste_indicator = CaretPasteIndicator()
        self.tray_manager = SystemTrayManager(self.main_window)

        self.is_recording = False
        self.audio_levels: List[float] = [0.0] * 20
        self.streaming_flow_active = False
        self._transcription_source_tab: int = TabbedContentWidget.TAB_QUICK_RECORD

        self.on_record_start: Optional[Callable] = None
        self.on_record_stop: Optional[Callable] = None
        self.on_record_cancel: Optional[Callable] = None
        self.on_model_changed: Optional[Callable] = None
        self.on_hotkeys_changed: Optional[Callable] = None
        self.on_retranscribe: Optional[Callable] = None
        self.on_upload_audio: Optional[Callable] = None
        self.on_whisper_settings_changed: Optional[Callable] = None
        self.on_audio_device_changed: Optional[Callable] = None
        self.on_streaming_settings_changed: Optional[Callable] = None
        self.on_hf_policy_changed: Optional[Callable] = None
        self.on_model_download_requested: Optional[Callable] = None
        self.on_model_delete_requested: Optional[Callable] = None
        self.on_dictation_transcribe: Optional[Callable] = None
        self.get_loaded_local_model: Optional[Callable] = None

        self.on_component_install: Optional[Callable] = None
        self.on_component_cancel: Optional[Callable] = None
        self.on_component_remove: Optional[Callable] = None
        self.on_check_for_updates: Optional[Callable] = None
        self.on_update_download: Optional[Callable] = None
        self._last_update_result: Optional[UpdateCheckResult] = None
        self._update_dialog: Optional[AppUpdateDialog] = None

        self.on_meeting_start: Optional[Callable] = None  # (cloud: Optional[bool])
        self.on_meeting_start_demo: Optional[Callable] = None  # (cloud: Optional[bool])
        self.on_meeting_end: Optional[Callable] = None
        self.on_meeting_pause: Optional[Callable] = None
        self.on_meeting_resume: Optional[Callable] = None
        self.on_meeting_open_dashboard: Optional[Callable] = None
        self.on_meeting_open_past: Optional[Callable] = None  # (meeting_id: str)
        self.on_meeting_copy_guest_link: Optional[Callable] = None
        self.on_meeting_toggle_cloud: Optional[Callable] = None  # (enabled: bool)
        self.on_meeting_retry_insights: Optional[Callable] = None
        self.on_meeting_retry_speakers: Optional[Callable] = None
        self.on_meeting_retry_step: Optional[Callable] = None  # (step_id: str)
        self.on_meeting_defer_insights: Optional[Callable] = None
        self.on_meeting_start_new: Optional[Callable] = None  # (cloud: Optional[bool])
        self.get_meeting_active: Optional[Callable] = None  # Provider: meeting running?
        self._meeting_active = False
        self._meeting_urls: dict = {}

        self._model_manager_dialog = None

        self.cancel_animation_timer = QTimer()
        self.cancel_animation_timer.setSingleShot(True)
        self.cancel_animation_timer.timeout.connect(self._on_cancel_animation_finished)

        self._setup_connections()

    def _setup_connections(self):
        self.main_window.record_toggled.connect(self._on_record_toggled)
        self.main_window.record_canceled.connect(self.cancel_recording)
        self.main_window.model_changed.connect(self._on_model_changed)
        self.main_window.whisper_engine_changed.connect(self._on_whisper_engine_changed)
        self.main_window.settings_requested.connect(self.open_settings_dialog)
        self.main_window.model_manager_requested.connect(self.open_model_manager_dialog)
        self.main_window.hotkeys_requested.connect(self.open_hotkey_dialog)
        self.main_window.about_requested.connect(self.show_about_dialog)
        self.main_window.check_for_updates_requested.connect(
            self._on_check_for_updates_requested
        )
        self.main_window.retranscribe_requested.connect(self._on_retranscribe_requested)
        self.main_window.upload_file_requested.connect(self._on_upload_file_transcribe)
        self.main_window.meeting_dashboard_requested.connect(
            self._on_meeting_open_dashboard
        )
        self.main_window.past_meeting_requested.connect(
            self._on_past_meeting_requested
        )

        meeting_tab = self.main_window.meeting_mode_tab
        meeting_tab.start_requested.connect(self._on_meeting_start_requested)
        meeting_tab.demo_requested.connect(self._on_meeting_demo_requested)
        meeting_tab.pause_requested.connect(
            lambda: self.on_meeting_pause and self.on_meeting_pause()
        )
        meeting_tab.resume_requested.connect(
            lambda: self.on_meeting_resume and self.on_meeting_resume()
        )
        meeting_tab.end_requested.connect(
            lambda: self.on_meeting_end and self.on_meeting_end()
        )
        meeting_tab.open_dashboard_requested.connect(self._on_meeting_open_dashboard)
        meeting_tab.copy_guest_link_requested.connect(
            lambda: self.on_meeting_copy_guest_link and self.on_meeting_copy_guest_link()
        )
        meeting_tab.cloud_toggled.connect(self._on_meeting_cloud_toggled)
        meeting_tab.retry_insights_requested.connect(
            self._on_meeting_retry_insights
        )
        meeting_tab.retry_speakers_requested.connect(
            self._on_meeting_retry_speakers
        )
        meeting_tab.retry_step_requested.connect(self._on_meeting_retry_step)
        meeting_tab.defer_insights_requested.connect(
            self._on_meeting_defer_insights
        )
        meeting_tab.start_new_meeting_requested.connect(
            self._on_meeting_start_new_requested
        )

        self.main_window.on_show_copied_animation = self.show_copied_animation

        self.tray_manager.show_requested.connect(self._on_tray_show)
        self.tray_manager.hide_requested.connect(self._on_tray_hide)
        self.tray_manager.exit_requested.connect(self._on_tray_exit)
        self.tray_manager.toggle_recording.connect(self._on_tray_toggle_recording)
        self.tray_manager.meeting_toggle_requested.connect(
            self._on_tray_meeting_toggle
        )
        self.tray_manager.meeting_dashboard_requested.connect(
            self._on_meeting_open_dashboard
        )

        self.overlay.state_changed.connect(self._on_overlay_state_changed)

        self.record_started.connect(self._show_recording_overlay)
        self.record_stopped.connect(self._show_processing_overlay)
        self.transcription_received.connect(self._display_transcript)
        self.status_changed.connect(self._apply_status_to_main_window)
        self.audio_levels_updated.connect(self._apply_audio_levels_to_overlay)

    def _on_record_toggled(self, is_recording: bool):
        if is_recording:
            self.start_recording()
        else:
            self.stop_recording()

    def _on_model_changed(self, model_name: str):
        logger.info(f"Model changed to: {model_name}")
        if self.on_model_changed:
            self.on_model_changed(model_name)

    def _on_whisper_engine_changed(self):
        """Handle a local-engine (model/device/quant) change from the main GUI.

        Reuses the same reload hook the Settings dialog fires; the controller's
        handler runs the reload on a background thread.
        """
        logger.info("Local engine settings changed via main GUI")
        if self.on_whisper_settings_changed:
            self.on_whisper_settings_changed()

    def _on_tray_show(self):
        self.main_window.restore_from_tray()
        logger.debug("Window shown from tray")

    def _on_tray_hide(self):
        self.main_window.hide()
        logger.debug("Window hidden to tray")

    def _on_tray_exit(self):
        logger.info("Exit requested from tray")

    def _on_tray_toggle_recording(self):
        if self.is_recording:
            self.stop_recording()
        else:
            self.start_recording()

    def _on_overlay_state_changed(self, state: str):
        logger.debug(f"Overlay state changed to: {state}")

    def _show_recording_overlay(self):
        self.tray_manager.set_recording(True)
        if not self.overlay.isVisible():
            self.overlay.show_at_cursor(self.overlay.STATE_RECORDING)
        else:
            self.overlay.set_state(self.overlay.STATE_RECORDING)

    def _show_processing_overlay(self):
        self.tray_manager.set_recording(False)
        if not self.overlay.isVisible():
            self.overlay.show_at_cursor(self.overlay.STATE_PROCESSING)
        else:
            self.overlay.set_state(self.overlay.STATE_PROCESSING)

    def _display_transcript(self, text: str, raw=None):
        if self._transcription_source_tab == TabbedContentWidget.TAB_UPLOAD_FILE:
            self.main_window.upload_file_tab.set_transcript(text, raw=raw)
        else:
            self.main_window.set_transcript(text, raw=raw)
        self.hide_overlay()

    def _apply_status_to_main_window(self, status: str):
        if self._transcription_source_tab == TabbedContentWidget.TAB_UPLOAD_FILE:
            self.main_window.upload_file_tab.set_status(status)
        else:
            self.main_window.set_status(status)

    def _apply_audio_levels_to_overlay(self, levels: List[float]):
        self.overlay.update_audio_levels(levels)

    def start_recording(self):
        """Start recording.

        Returns:
            False when the application layer refused the start (e.g. Meeting
            Mode is active) and the recording UI was rolled back; True
            otherwise.
        """
        self.is_recording = True
        self._transcription_source_tab = TabbedContentWidget.TAB_QUICK_RECORD
        logger.info("Recording started")

        if not self.main_window.is_recording:
            self.main_window.is_recording = True
            self.main_window._update_recording_state()

        if self.on_record_start:
            if self.on_record_start() is False:
                self._revert_refused_record_start()
                return False
        else:
            self.record_started.emit()
        return True

    def _revert_refused_record_start(self):
        """Undo the recording UI after a refused start.

        The record button, window, and tab lock all flip to "recording" before
        the callback runs, so a refusal two layers down has to be unwound here
        — otherwise the UI shows a red, captureless recording state.
        """
        logger.info("Recording start refused; reverting recording state")
        self.is_recording = False
        self.main_window.is_recording = False
        self.main_window._update_recording_state()

    def stop_recording(self):
        self.is_recording = False
        logger.info("Recording stopped")

        if self.main_window.is_recording:
            self.main_window.is_recording = False
            self.main_window._update_recording_state()
            logger.info("Main window recording state updated")

        if self.on_record_stop:
            self.on_record_stop()
        else:
            self.record_stopped.emit()

    def cancel_recording(self):
        self.is_recording = False
        logger.info("Recording canceled")

        if self.main_window.is_recording:
            self.main_window.is_recording = False
            self.main_window._update_recording_state()
            logger.info("Main window recording state updated")

        if self.on_record_cancel:
            self.on_record_cancel()
            logger.info("Record cancel callback called")

        self.main_window.clear_transcription()

    def set_transcript(self, text: str, raw=None):
        self.transcription_received.emit(text, raw)

    def set_device_info(self, device_info: str):
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
        if self._transcription_source_tab == TabbedContentWidget.TAB_UPLOAD_FILE:
            self.main_window.upload_file_tab.set_transcription_stats(
                transcription_time, audio_duration, file_size
            )
        else:
            self.main_window.set_transcription_stats(
                transcription_time, audio_duration, file_size
            )

    def clear_transcription_stats(self):
        self.main_window.clear_transcription_stats()
        self.main_window.upload_file_tab.clear_transcription_stats()

    def set_status(self, status: str):
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
        self.streaming_flow_active = False
        self.hide_caret_paste_indicator()
        self.overlay.clear_streaming_text()
        self._show_or_set_overlay(waveform_state)

    def _show_or_set_overlay(self, overlay_state: str) -> None:
        if not self.overlay.isVisible():
            self.overlay.show_at_cursor(overlay_state)
        else:
            self.overlay.set_state(overlay_state)

    def update_audio_levels(self, levels: List[float]):
        self.audio_levels = levels
        self.audio_levels_updated.emit(levels)

    def hide_overlay(self):
        self.overlay.hide()

    def show_copied_animation(self):
        self.overlay.show_at_cursor(self.overlay.STATE_COPIED)

    def show_streaming_overlay(self):
        self.streaming_flow_active = True
        self.overlay.clear_streaming_text()
        self.overlay.show_at_cursor(self.overlay.STATE_STREAMING)
        logger.debug("Streaming preview shown on waveform overlay")

    def update_streaming_text(self, text: str, is_final: bool):
        self.overlay.update_streaming_text(text, is_final)

    def hide_streaming_overlay(self):
        self.streaming_flow_active = False
        self.overlay.clear_streaming_text()
        logger.debug("Streaming preview cleared")

    def show_caret_paste_indicator(self):
        self.caret_paste_indicator.show_indicator()
        logger.debug("Caret paste indicator shown")

    def hide_caret_paste_indicator(self):
        self.caret_paste_indicator.hide_indicator()
        logger.debug("Caret paste indicator hidden")

    def _start_cancel_animation(self):
        self.cancel_animation_timer.stop()
        self.hide_caret_paste_indicator()
        self.streaming_flow_active = False
        self.overlay.clear_streaming_text()

        if not self.overlay.isVisible():
            self.overlay.show_at_cursor(self.overlay.STATE_CANCELING)
        else:
            self.overlay.set_state(self.overlay.STATE_CANCELING)

        self.cancel_animation_timer.start(
            config.CANCELLATION_ANIMATION_DURATION_MS + config.CANCELLATION_GRACE_MS
        )

    def _on_cancel_animation_finished(self):
        if self.overlay.current_state not in {
            self.overlay.STATE_CANCELING,
            self.overlay.STATE_IDLE
        }:
            return
        self.hide_overlay()

    def show_main_window(self):
        self.main_window.restore_from_tray()

    def open_settings_dialog(self, focus_hf_policy: bool = False):
        """Open the settings dialog.

        Args:
            focus_hf_policy: When True, open directly to the Advanced tab with
                the Hugging Face download-policy control focused (used by the
                consent dialog's "Open Settings" action).
        """
        dialog = SettingsDialog(self.main_window)
        dialog.on_dictation_transcribe = self.on_dictation_transcribe
        dialog.get_meeting_active = self.get_meeting_active
        if focus_hf_policy:
            dialog.focus_hf_policy()
        else:
            dialog.tabs.setCurrentIndex(0)  # Default to general

        def on_settings_changed(settings: dict):
            self.overlay.refresh_streaming_font_size()
            self.refresh_cleanup_controls()
            self.main_window.meeting_mode_tab.set_developer_mode(
                bool(settings.get(SettingsKey.DEVELOPER_MODE, False))
            )
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
        # Open only after Settings' modal loop ends — showing the non-modal
        # Model Manager during exec() stacks behind the main window on Windows.
        manager_tab = dialog.open_model_manager_on_close
        if manager_tab:
            QTimer.singleShot(
                0, lambda tab=manager_tab: self.open_model_manager_dialog(tab=tab)
            )

    def refresh_local_engine_controls(self):
        """Re-sync the inline local-engine combos with the persisted settings.

        Signal-safe: the combos are updated with signals blocked, so this never
        triggers another engine reload.
        """
        self.main_window.quick_record_tab.local_engine.load_from_settings()
        self.main_window.upload_file_tab.local_engine.load_from_settings()

    def refresh_cleanup_controls(self):
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

    def open_model_manager_dialog(self, tab: str = "ondemand"):
        """Show the non-modal Model Manager (single instance, re-raised).

        Args:
            tab: ``\"ondemand\"``, ``\"meeting\"``, ``\"library\"``, or the
                legacy values ``\"voice\"`` (Library) and ``\"text\"``
                (On-demand, scrolled to cleanup).
        """
        from ui_qt.dialogs.model_manager_dialog import ModelManagerDialog

        if self._model_manager_dialog is None:
            dialog = ModelManagerDialog(
                get_loaded_model=self.get_loaded_local_model,
                parent=self.main_window,
            )
            dialog.on_download_requested = self.on_model_download_requested
            dialog.on_delete_requested = self.on_model_delete_requested
            dialog.on_set_active_requested = self._on_manager_set_active
            dialog.on_backend_changed = self.select_transcription_backend
            dialog.on_runtime_settings_changed = self._on_manager_runtime_changed
            dialog.component_install_requested.connect(
                self.on_component_install_requested
            )
            dialog.component_cancel_requested.connect(
                self.on_component_cancel_requested
            )
            dialog.component_remove_requested.connect(
                self.on_component_remove_requested
            )
            self._model_manager_dialog = dialog

        self._model_manager_dialog.refresh()
        if tab in ("text", "ondemand"):
            if tab == "text":
                self._model_manager_dialog.show_text_tab()
            else:
                self._model_manager_dialog.show_ondemand_tab()
        elif tab == "meeting":
            self._model_manager_dialog.show_meeting_tab()
        elif tab in ("library", "voice"):
            self._model_manager_dialog.show_library_tab()
        self._model_manager_dialog.show()
        self._model_manager_dialog.raise_()
        self._model_manager_dialog.activateWindow()

    def select_transcription_backend(self, display_name: str) -> None:
        """Select a dictation backend through the main-window combo path.

        Args:
            display_name: A ``config.MODEL_CHOICES`` label such as
                ``\"Local Whisper\"``.
        """
        tabs = getattr(self.main_window, "transcription_tabs", None)
        if not display_name or not tabs:
            return
        tab = tabs[0]
        if tab.model_combo.currentText() == display_name:
            return
        tab.model_combo.setCurrentText(display_name)

    def _on_manager_runtime_changed(self) -> None:
        self.refresh_local_engine_controls()
        if self.on_whisper_settings_changed:
            self.on_whisper_settings_changed()

    def _on_manager_set_active(self, model_name: str):
        """Persist a Model Manager Whisper assignment and reload the engine.

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
        if self._model_manager_dialog is not None and self._model_manager_dialog.isVisible():
            self._model_manager_dialog.refresh()

    def on_model_download_started(self, model_name: str):
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.set_downloading(model_name)

    def on_model_download_finished(self, model_name: str, success: bool):
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.finish_download(model_name, success)

    def on_model_deleted(self, model_name: str, success: bool, error: str):
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.show_delete_result(model_name, success, error)

    def on_component_install_requested(self, component_id: str):
        if self.on_component_install:
            self.on_component_install(component_id)

    def on_component_cancel_requested(self, component_id: str):
        if self.on_component_cancel:
            self.on_component_cancel(component_id)

    def on_component_remove_requested(self, component_id: str):
        if self.on_component_remove:
            self.on_component_remove(component_id)

    def on_component_progress(
        self, component_id: str, phase: str, done: int, total: int
    ):
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.set_component_progress(
                component_id, phase, done, total
            )

    def on_component_state_changed(self):
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.refresh_components()

    def on_component_install_finished(
        self, component_id: str, success: bool, message: str
    ):
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.finish_component_install(
                component_id, success, message
            )

    def ensure_meeting_platform_ack(self) -> bool:
        """Require the unsupported-platform warning before Meeting Mode.

        Windows returns immediately. On macOS/Linux the first call shows the
        acknowledgement dialog; later calls reuse the persisted answer.

        Returns:
            True when a meeting may start or the Meeting Mode tab may open.
        """
        from ui_qt.dialogs.meeting_unsupported_dialog import (
            acknowledge_unsupported_meeting_mode,
        )

        if not acknowledge_unsupported_meeting_mode(self.main_window):
            return False
        self.main_window.tabbed_content.unlock_meeting_tab()
        return True

    def _on_meeting_start_requested(self, cloud_enabled: bool):
        if not self.ensure_meeting_platform_ack():
            return
        if self.on_meeting_start:
            self.on_meeting_start(cloud_enabled)

    def _on_meeting_demo_requested(self, cloud_enabled: bool):
        if not self.ensure_meeting_platform_ack():
            return
        if self.on_meeting_start_demo:
            self.on_meeting_start_demo(cloud_enabled)

    def _on_meeting_cloud_toggled(self, enabled: bool):
        if self.on_meeting_toggle_cloud:
            self.on_meeting_toggle_cloud(enabled)

    def _on_meeting_open_dashboard(self):
        if self.on_meeting_open_dashboard:
            self.on_meeting_open_dashboard()

    def _on_meeting_retry_insights(self):
        if self.on_meeting_retry_insights:
            self.on_meeting_retry_insights()

    def _on_meeting_retry_speakers(self):
        if self.on_meeting_retry_speakers:
            self.on_meeting_retry_speakers()

    def _on_meeting_retry_step(self, step_id: str):
        if self.on_meeting_retry_step:
            self.on_meeting_retry_step(step_id)

    def _on_meeting_defer_insights(self):
        if self.on_meeting_defer_insights:
            self.on_meeting_defer_insights()

    def _on_meeting_start_new_requested(self, cloud_enabled: bool):
        if not self.ensure_meeting_platform_ack():
            return
        if self.on_meeting_start_new:
            self.on_meeting_start_new(cloud_enabled)

    def _on_past_meeting_requested(self, meeting_id: str) -> None:
        if self.on_meeting_open_past:
            self.on_meeting_open_past(meeting_id)

    def _on_tray_meeting_toggle(self):
        if self._meeting_active:
            if self.on_meeting_end:
                self.on_meeting_end()
        elif self.ensure_meeting_platform_ack() and self.on_meeting_start:
            self.on_meeting_start(None)

    def on_meeting_state_changed(self, payload: Any) -> None:
        """Apply a meeting-state payload to the Meeting Mode tab and tray.

        Args:
            payload: Partial meeting-state dict from MeetingRuntime
                (``active``, ``paused``, ``status``, ``cloud_enabled``,
                ``elapsed_s``, ``finalization``). Dashboard URLs are retained
                after capture ends so Open Dashboard stays available during
                and after finalization.
        """
        if not isinstance(payload, dict):
            return
        has_dashboard = bool(
            self._meeting_urls.get("host_url") or self._meeting_urls.get("url")
        )
        # Always tell the tab whether Open Dashboard can work, including during
        # post-meeting finalization when capture is already inactive.
        tab_payload = dict(payload)
        tab_payload.setdefault("dashboard_available", has_dashboard)
        self.main_window.meeting_mode_tab.set_meeting_state(tab_payload)
        if (
            payload.get("active") is False
            and str(payload.get("status") or "")
            in {"ended", "failed", "needs_recovery", "canceled"}
        ):
            self.main_window.refresh_past_meetings()
        if "active" in payload:
            self._meeting_active = bool(payload["active"])
            self.tray_manager.set_meeting_active(
                self._meeting_active, dashboard_available=has_dashboard
            )
            if self._meeting_active:
                self.switch_to_meeting_mode()
                self.main_window.tabbed_content.set_recording_state(
                    True, TabbedContentWidget.TAB_MEETING_MODE
                )
            else:
                # Unlock other tabs once capture ends; finalization stays on
                # the Meeting Mode tab without blocking Quick Record.
                if not self.main_window.is_recording:
                    self.main_window.tabbed_content.set_recording_state(False, -1)
        elif has_dashboard:
            self.tray_manager.set_meeting_active(
                self._meeting_active, dashboard_available=True
            )

    def set_meeting_status(self, status: str) -> None:
        if not status:
            return
        self.main_window.meeting_mode_tab.set_status_text(status)
        self.set_status(status)

    def on_meeting_error(self, message: str) -> None:
        """Surface a meeting error in status and a modal warning.

        Args:
            message: Error description from MeetingRuntime.
        """
        text = message or "Meeting error"
        self.set_meeting_status(text)
        QMessageBox.warning(self.main_window, "Meeting Error", text)

    def on_meeting_server_started(self, result: Any) -> None:
        """Store session URLs after the meeting web server starts.

        Args:
            result: Dict with ``meeting_id``, ``url``, ``host_url``,
                ``guest_url`` from MeetingEngine.start().
        """
        if not isinstance(result, dict):
            return
        self._meeting_urls = dict(result)
        has_dashboard = bool(
            self._meeting_urls.get("host_url") or self._meeting_urls.get("url")
        )
        self.tray_manager.set_meeting_active(
            self._meeting_active, dashboard_available=has_dashboard
        )
        self.main_window.meeting_mode_tab.set_dashboard_available(has_dashboard)
        if self.on_meeting_open_dashboard:
            self.on_meeting_open_dashboard()

    def show_meeting_consent_dialog(self) -> bool:
        """Show the one-time cloud-intelligence consent dialog.

        Returns:
            True when the user enables cloud intelligence.
        """
        from ui_qt.dialogs.meeting_consent_dialog import MeetingConsentDialog

        destination = None
        remote = None
        try:
            from services.settings import (
                resolve_meeting_llm_profile,
                settings_manager,
            )
            from services.text_llm import (
                consent_destination,
                destination_is_remote,
            )

            profile = resolve_meeting_llm_profile(
                settings_manager.load_all_settings()
            )
            destination = consent_destination(profile)
            remote = destination_is_remote(profile)
        except Exception:
            logger.debug(
                "Could not resolve meeting endpoint for consent copy",
                exc_info=True,
            )

        dialog = MeetingConsentDialog(
            parent=self.main_window,
            destination=destination,
            remote=remote,
        )
        dialog.exec()
        return dialog.result_action == MeetingConsentDialog.RESULT_ENABLE

    def show_meeting_recovery_dialog(
        self,
        meetings: List[Dict[str, Any]],
        on_finalize: Optional[Callable[[str], None]] = None,
        on_discard: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Show the interrupted-meeting recovery dialog.

        Args:
            meetings: Interrupted meeting dicts from the recovery scan.
            on_finalize: Callback receiving a meeting id to finalize.
            on_discard: Callback receiving a meeting id to discard.
        """
        from ui_qt.dialogs.meeting_recovery_dialog import MeetingRecoveryDialog

        dialog = MeetingRecoveryDialog(list(meetings or []), parent=self.main_window)
        dialog.on_finalize = on_finalize
        dialog.on_discard = on_discard
        dialog.exec()

    def copy_meeting_guest_link(self, url: str) -> None:
        """Copy the guest dashboard URL to the clipboard.

        Args:
            url: Guest join URL for the active meeting.
        """
        if not url:
            return
        QApplication.clipboard().setText(url)
        self._meeting_urls["guest_url"] = url
        self.set_meeting_status("Guest link copied")
        self.show_copied_animation()

    def open_hotkey_dialog(self):
        dialog = HotkeyDialog(self.main_window)

        def on_hotkeys_save(hotkeys):
            if self.on_hotkeys_changed:
                self.on_hotkeys_changed(hotkeys)
            self.update_hotkey_display(hotkeys)

        dialog.on_hotkeys_save = on_hotkeys_save
        dialog.exec()

    def _on_upload_file_transcribe(self, audio_path: str):
        self._transcription_source_tab = TabbedContentWidget.TAB_UPLOAD_FILE
        logger.info(f"Upload tab transcription started: {audio_path}")
        if self.on_upload_audio:
            self.on_upload_audio(audio_path)

    def switch_to_tab(self, index: int):
        self.main_window.tabbed_content.set_current_index(index)

    def switch_to_meeting_mode(self):
        self.switch_to_tab(TabbedContentWidget.TAB_MEETING_MODE)

    def update_hotkey_display(self, hotkeys: dict):
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

    def _on_check_for_updates_requested(self) -> None:
        """Help → Check for Updates. Always hits GitHub."""
        if self.on_check_for_updates:
            self.on_check_for_updates()

    def on_update_check_finished(
        self,
        result: Optional[UpdateCheckResult],
        error: str = "",
        manual: bool = False,
    ) -> None:
        """Show the update dialog for a manual check or an allowed auto-notify."""
        if result is not None:
            self._last_update_result = result
        if not manual:
            if result is None or error:
                return
            latest = result.release.version if result.release else ""
            if not should_auto_notify(
                result.status, latest, settings_manager.load_all_settings()
            ):
                return
            if self.is_recording or (
                self.get_meeting_active and self.get_meeting_active()
            ):
                return
        self.show_app_update_dialog(result, error=error)

    def show_app_update_dialog(
        self,
        result: Optional[UpdateCheckResult],
        error: str = "",
    ) -> None:
        """Open the update dialog and handle Download / Open release notes."""
        dialog = AppUpdateDialog(result, error=error, parent=self.main_window)
        self._update_dialog = dialog
        dialog.on_download_requested = self._on_update_download_requested
        dialog.exec()
        if (
            dialog.result_action == AppUpdateDialog.RESULT_PRIMARY
            and result is not None
            and result.release is not None
            and not result.can_apply
        ):
            QDesktopServices.openUrl(QUrl(result.release.html_url))
        if self._update_dialog is dialog and dialog.result_action != (
            AppUpdateDialog.RESULT_PRIMARY
        ):
            self._update_dialog = None

    def _on_update_download_requested(self, result: UpdateCheckResult) -> None:
        if self.on_update_download:
            self.on_update_download(result)

    def on_update_download_progress(self, phase: str, done: int, total: int) -> None:
        """Forward installer-download progress to the open dialog."""
        if self._update_dialog is not None:
            self._update_dialog.set_progress(phase, done, total)

    def on_update_download_finished(self, path: str, error: str) -> None:
        """Launch the verified Inno setup and quit, or show the error."""
        if error:
            if self._update_dialog is not None:
                self._update_dialog.set_error(error)
            else:
                QMessageBox.warning(
                    self.main_window, "Update failed", error
                )
            return
        from PyQt6.QtCore import QProcess

        launched = QProcess.startDetached(path, [])
        if not launched:
            message = "The installer could not be started."
            if self._update_dialog is not None:
                self._update_dialog.set_error(message)
            else:
                QMessageBox.warning(self.main_window, "Update failed", message)
            return
        QApplication.instance().quit()

    def show_about_dialog(self):
        channel = channel_label(detect_channel())
        status_line = ""
        result = self._last_update_result
        if result is not None:
            if (
                result.status == UpdateStatus.UPDATE_AVAILABLE
                and result.release is not None
            ):
                status_line = f"Update available: {result.release.version}"
            elif result.status == UpdateStatus.DEVELOPMENT:
                latest = result.release.version if result.release else ""
                status_line = (
                    f"Development build, newer than {latest}"
                    if latest
                    else "Development build"
                )
            else:
                status_line = "Up to date"
        status_html = (
            f"<p>Update status: {status_line}</p>" if status_line else ""
        )
        QMessageBox.about(
            self.main_window,
            "About OpenWhisper",
            "<p><b>OpenWhisper - Speech-to-Text Application</b></p>"
            f"<p>Version {config.VERSION}<br>"
            f"Install: {channel}</p>"
            f"{status_html}"
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

    def refresh_history(self):
        self.main_window.refresh_history()

    def _on_retranscribe_requested(self, audio_path: str):
        self._request_retranscription(audio_path)

    def _request_retranscription(self, audio_path: str):
        logger.info("Re-transcribe requested: %s", audio_path)
        if self.on_retranscribe:
            self.on_retranscribe(audio_path)

    def cleanup(self):
        logger.info("Starting UI Controller cleanup...")

        try:
            if self.cancel_animation_timer.isActive():
                self.cancel_animation_timer.stop()
        except Exception as e:
            logger.debug(f"Error stopping cancel animation timer: {e}")

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

        try:
            self.tray_manager.hide()
            self.tray_manager.setParent(None)
        except Exception as e:
            logger.debug(f"Error hiding system tray: {e}")

        try:
            self.main_window._force_quit = True
            self.main_window.close()
        except Exception as e:
            logger.debug(f"Error closing main window: {e}")

        logger.info("UI Controller cleaned up")
