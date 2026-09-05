import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional
from PyQt6.QtCore import QTimer, QUrl, pyqtSignal, QObject
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from config import config
from services.app_update import (
    RELEASES_PAGE_URL,
    ApplyMode,
    DownloadPhase,
    UpdateCheckResult,
    UpdateStatus,
    channel_label,
    detect_channel,
    should_auto_notify,
)
from services.app_update_apply import (
    acquire_application_mutex_or_exit,
    consume_apply_error,
    helper_argv,
    helper_exe_for,
    load_journal,
    release_application_mutex_for_setup,
)
from services.update_contract import decode_native_result
from services.hotkey_manager import format_hotkey_display
from ui_qt.clipboard import ClipboardStageResult, TemporaryClipboard
from ui_qt.overlay_state import OverlayState
from ui_qt.main_window import MainWindow
from ui_qt.overlays import WaveformOverlay
from ui_qt.system_tray import SystemTrayManager
from ui_qt.dialogs.app_update_dialog import AppUpdateDialog
from ui_qt.dialogs.settings_dialog import GENERAL, HOTKEYS, SettingsDialog
from ui_qt.utils.font_scale import apply_ui_font_scale
from ui_qt.widgets import TabbedContentWidget
from ui_qt.widgets.transcription_progress import stage_for_overlay_state
from services.settings import SettingsKey, settings_manager

logger = logging.getLogger(__name__)

HANDOFF_EXIT_GRACE_S = 10.0


def _hard_exit() -> None:
    """Leave without running another line of Python: no atexit, no thread joins."""
    os._exit(0)


def arm_handoff_watchdog(grace_s: float = HANDOFF_EXIT_GRACE_S) -> threading.Timer:
    """Hard-exit if this process is still alive ``grace_s`` after a handoff.

    The updater helper waits for this exact process and abandons the update if
    it never leaves, so leaving must not depend on every window agreeing to
    close, on cleanup returning, or on interpreter shutdown joining every
    thread. The timer is a daemon, so a normal exit kills it before it fires.
    """

    def _fire() -> None:
        try:
            logger.error(
                "Still running %.0fs after handing off to the updater; exiting hard",
                grace_s,
            )
            logging.shutdown()
        except Exception:
            pass
        _hard_exit()

    timer = threading.Timer(grace_s, _fire)
    timer.daemon = True
    timer.start()
    return timer


def _start_detached(program: str, arguments: list) -> bool:
    from services.app_update_apply import UpdateApplyError, _launch_exe

    try:
        _launch_exe(program, arguments)
    except UpdateApplyError:
        logger.exception("Could not launch the update")
        return False
    return True


class UIController(QObject):
    record_started = pyqtSignal()
    record_stopped = pyqtSignal()
    transcription_received = pyqtSignal(str, object)  # fixed text, optional raw
    status_changed = pyqtSignal(str)
    audio_levels_updated = pyqtSignal(list)

    def __init__(self):
        super().__init__()

        self._temporary_clipboard = TemporaryClipboard(
            QApplication.clipboard(), self
        )
        self._temporary_clipboard.restore_failed.connect(
            self._on_clipboard_restore_failed
        )

        self.main_window = MainWindow()
        self.overlay = WaveformOverlay()
        self.tray_manager = SystemTrayManager(self.main_window)
        self.main_window.set_tray_available(self.tray_manager.available)

        self.is_recording = False
        self.audio_levels: List[float] = [0.0] * 20
        self.streaming_flow_active = False
        self._transcription_source_tab: int = TabbedContentWidget.TAB_QUICK_RECORD

        self.on_record_start: Optional[Callable] = None
        self.on_record_stop: Optional[Callable] = None
        self.on_record_cancel: Optional[Callable] = None
        self.on_model_changed: Optional[Callable] = None
        self.on_hotkeys_changed: Optional[Callable] = None
        self.on_recording_trigger_mode_changed: Optional[Callable] = None
        self.on_retranscribe: Optional[Callable] = None
        self.on_upload_audio: Optional[Callable] = None
        self.on_upload_audio_files: Optional[Callable] = None
        self.on_upload_cancel: Optional[Callable] = None
        self.on_whisper_settings_changed: Optional[Callable] = None
        self.on_audio_device_changed: Optional[Callable] = None
        self.on_streaming_settings_changed: Optional[Callable] = None
        self.on_hf_policy_changed: Optional[Callable] = None
        self.on_api_keys_changed: Optional[Callable] = None
        self.on_model_download_requested: Optional[Callable] = None
        self.on_model_delete_requested: Optional[Callable] = None
        self.on_model_batch_download: Optional[Callable] = None
        self.on_model_batch_stop: Optional[Callable] = None
        self.on_dictation_transcribe: Optional[Callable] = None
        self.get_loaded_local_model: Optional[Callable] = None
        self.get_missing_local_runtime: Optional[Callable[[], Optional[str]]] = None

        self.on_component_install: Optional[Callable] = None
        self.on_component_cancel: Optional[Callable] = None
        self.on_component_remove: Optional[Callable] = None
        self.on_check_for_updates: Optional[Callable] = None
        self.on_update_download: Optional[Callable] = None
        self.on_update_cancel: Optional[Callable] = None
        self.on_update_abandon: Optional[Callable] = None
        self.get_transcribing: Optional[Callable[[], bool]] = None
        self.get_component_installing: Optional[Callable[[], bool]] = None
        self._last_update_result: Optional[UpdateCheckResult] = None
        self._update_dialog: Optional[AppUpdateDialog] = None
        self._update_canceled = False

        self.on_meeting_start: Optional[Callable] = None  # (cloud: Optional[bool])
        self.on_meeting_start_demo: Optional[Callable] = None  # (cloud: Optional[bool])
        self.on_meeting_end: Optional[Callable] = None
        self.on_meeting_pause: Optional[Callable] = None
        self.on_meeting_resume: Optional[Callable] = None
        self.on_meeting_open_dashboard: Optional[Callable] = None
        self.on_meeting_open_past: Optional[Callable] = None  # (meeting_id: str)
        self.on_meeting_copy_transcript: Optional[Callable] = None  # (id) -> str|None
        self.on_meeting_delete_past: Optional[Callable] = None  # (id, delete_recordings)
        self.on_meeting_clear_past: Optional[Callable] = None  # (delete_recordings: bool)
        self.on_meeting_open_report: Optional[Callable] = None
        self.on_meeting_copy_guest_link: Optional[Callable] = None
        self.on_meeting_toggle_cloud: Optional[Callable] = None  # (enabled: bool)
        self.on_meeting_retry_insights: Optional[Callable] = None
        self.on_meeting_retry_speakers: Optional[Callable] = None
        self.on_meeting_retry_step: Optional[Callable] = None  # (step_id: str)
        self.on_meeting_background: Optional[Callable] = None
        self.on_meeting_defer_insights: Optional[Callable] = None
        self.on_meeting_start_new: Optional[Callable] = None  # (cloud: Optional[bool])
        self.get_meeting_active: Optional[Callable] = None  # Provider: meeting running?
        self._meeting_active = False
        self._meeting_urls: dict = {}

        self._model_manager_dialog = None
        self._settings_dialog = None
        self._downloads_dialog = None
        self._download_progress_dialog = None

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
        self.main_window.hotkeys_requested.connect(self.open_hotkey_settings)
        self.main_window.about_requested.connect(self.show_about_dialog)
        self.main_window.check_for_updates_requested.connect(
            self._on_check_for_updates_requested
        )
        self.main_window.retranscribe_requested.connect(self._on_retranscribe_requested)
        self.main_window.upload_file_requested.connect(self._on_upload_file_transcribe)
        self.main_window.upload_files_requested.connect(self._on_upload_files_transcribe)
        self.main_window.upload_cancel_requested.connect(self._on_upload_cancel)
        self.main_window.upload_copy_requested.connect(self._on_upload_copy)
        self.main_window.quick_record_copy_requested.connect(self._on_quick_record_copy)
        self.main_window.meeting_dashboard_requested.connect(
            self._on_meeting_open_dashboard
        )
        self.main_window.past_meeting_requested.connect(
            self._on_past_meeting_requested
        )
        self.main_window.past_meeting_copy_requested.connect(
            self._on_past_meeting_copy_requested
        )
        self.main_window.past_meeting_delete_requested.connect(
            self._on_past_meeting_delete_requested
        )
        self.main_window.past_meetings_clear_requested.connect(
            self._on_past_meetings_clear_requested
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
        meeting_tab.end_requested.connect(self._on_meeting_end_requested)
        meeting_tab.open_dashboard_requested.connect(self._on_meeting_open_dashboard)
        meeting_tab.open_report_requested.connect(self._on_meeting_open_report)
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
        meeting_tab.background_requested.connect(
            lambda: self.on_meeting_background and self.on_meeting_background()
        )
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
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.refresh_engine_selection()

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
        # Must go through the window: a bare QApplication.quit() is vetoed by
        # the minimize-to-tray closeEvent, which only hid the window again.
        self.main_window.quit_application()

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
            tab = self.main_window.upload_file_tab
            tab.set_transcript(text, raw=raw)
        else:
            tab = self.main_window.quick_record_tab
            self.main_window.set_transcript(text, raw=raw)
        tab.expand_transcription()
        self.hide_overlay()

    def _apply_status_to_main_window(self, status: str):
        if self.main_window.quick_record_tab.engine_loading:
            self.main_window.set_status(status)
            self.main_window.upload_file_tab.set_engine_message(status)
        elif self._transcription_source_tab == TabbedContentWidget.TAB_UPLOAD_FILE:
            self.main_window.upload_file_tab.set_status(status)
        else:
            self.main_window.set_status(status)

    def _apply_audio_levels_to_overlay(self, levels: List[float]):
        self.overlay.update_audio_levels(levels)

    def start_recording(self):
        """Request a recording start; UI flips only after the stream opens."""
        self._transcription_source_tab = TabbedContentWidget.TAB_QUICK_RECORD
        if self.on_record_start:
            if self.on_record_start() is False:
                self._revert_refused_record_start()
                return False
        else:
            self.record_started.emit()
        return True

    def _revert_refused_record_start(self):
        """Undo optimistic Recording chrome after a refused start."""
        logger.info("Recording start refused; reverting recording state")
        self.is_recording = False
        self.main_window.is_recording = False
        self.main_window._update_recording_state()

    def stop_recording(self):
        """Request a recording stop; UI flips via recording_state_changed."""
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

    def set_device_info(self, device_info: str, ready: Optional[bool] = None):
        self.main_window.set_device_info(device_info, ready)

    def set_engine_busy(self, busy: bool):
        """Disable/enable the inline local-engine combos during a reload.

        When the engine becomes idle again, refresh the Model Manager so its
        Delete lock tracks the newly loaded model — not the previous one left
        from the pre-reload refresh after Set Active.

        Args:
            busy: True to disable combos while the engine reloads, else False.
        """
        self.main_window.quick_record_tab.set_engine_busy(busy)
        self.main_window.upload_file_tab.set_engine_busy(busy)
        if not busy:
            self.refresh_model_manager()

    def set_transcription_stats(
        self,
        transcription_time: float,
        audio_duration: float,
        file_size: int,
        cleanup_time: Optional[float] = None,
    ):
        if self._transcription_source_tab == TabbedContentWidget.TAB_UPLOAD_FILE:
            self.main_window.upload_file_tab.set_transcription_stats(
                transcription_time, audio_duration, file_size, cleanup_time
            )
        else:
            self.main_window.set_transcription_stats(
                transcription_time, audio_duration, file_size, cleanup_time
            )

    def clear_transcription_stats(self):
        self.main_window.clear_transcription_stats()
        self.main_window.upload_file_tab.clear_transcription_stats()

    def set_status(self, status: str):
        self.status_changed.emit(status)

    def set_overlay_state(self, state: OverlayState) -> None:
        """Route an explicit overlay-state change to the correct overlay component.

        Centralizes all "show waveform vs streaming overlay vs hide everything"
        logic in one place. A job the user started from the Upload File tab
        reports inside that tab instead: the overlay is feedback for a hotkey
        pressed while another application is in front.
        """
        if self._upload_job_active() and self._route_upload_progress(state):
            return

        self.main_window.quick_record_tab.set_activity_state(state)

        if state is OverlayState.CANCELING:
            self.tray_manager.set_recording(False)
            self._start_cancel_animation()
            return

        if state is OverlayState.NONE:
            self.tray_manager.set_recording(False)
            self.hide_overlay()
            self.hide_streaming_overlay()
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

    def _upload_job_active(self) -> bool:
        return (
            self._transcription_source_tab == TabbedContentWidget.TAB_UPLOAD_FILE
            and self.main_window.upload_file_tab.is_transcribing
        )

    def _route_upload_progress(self, state: OverlayState) -> bool:
        """Send an upload job's stage to the Upload File card.

        Returns:
            True when the state was handled inline and the overlay must stay
            out of it. ``NONE`` is passed to the card as well but returns
            False so the shared teardown still runs.
        """
        tab = self.main_window.upload_file_tab
        if state is OverlayState.NONE:
            tab.set_progress_state(state)
            return False
        if stage_for_overlay_state(state) is None:
            return False
        self.tray_manager.set_recording(False)
        self.hide_overlay()
        tab.set_progress_state(state)
        return True

    def show_large_file_state(self, file_size_mb: float, is_splitting: bool) -> None:
        """Announce a large file: inline for an upload job, on the overlay otherwise."""
        if self._upload_job_active():
            self.main_window.upload_file_tab.set_large_file_stage(
                file_size_mb, is_splitting
            )
            return
        self.overlay.set_large_file_info(file_size_mb)
        if is_splitting:
            self.overlay.show_at_cursor(self.overlay.STATE_LARGE_FILE_SPLITTING)
        else:
            self.overlay.show_at_cursor(self.overlay.STATE_LARGE_FILE_PROCESSING)

    def _dismiss_streaming_preview_for_waveform(self, waveform_state: str) -> None:
        self.streaming_flow_active = False
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

    def copy_to_clipboard(self, text: str) -> bool:
        """Copy text to the Qt clipboard. Returns True if the write succeeded."""
        return self._temporary_clipboard.write_text(text)

    def stage_transcript_for_paste(self, text: str) -> ClipboardStageResult:
        return self._temporary_clipboard.stage_text(text)

    def schedule_clipboard_restore(self, stage: ClipboardStageResult) -> bool:
        if stage.lease is None:
            return False
        return self._temporary_clipboard.schedule_restore(
            stage.lease, config.AUTO_PASTE_CLIPBOARD_RESTORE_DELAY_MS
        )

    def commit_transcript_clipboard(
        self, stage: ClipboardStageResult, text: str
    ) -> bool:
        if stage.lease is None:
            return stage.written
        return self._temporary_clipboard.commit_text(stage.lease, text)

    def _on_clipboard_restore_failed(self, _message: str) -> None:
        self.set_status("Ready (Pasted; clipboard restore failed)")

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

    def _start_cancel_animation(self):
        self.cancel_animation_timer.stop()
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
        self.main_window.quick_record_tab.set_activity_state(OverlayState.NONE)

    def show_main_window(self):
        self.main_window.restore_from_tray()

    def open_settings_dialog(self, focus_hf_policy: bool = False):
        """Show the non-modal Settings window (single instance, re-raised).

        Args:
            focus_hf_policy: When True, open Advanced with the Hugging Face
                download-policy control focused (used by the consent dialog's
                "Open Settings" action).
        """
        dialog = self._prepare_settings_dialog()
        dialog.refresh()
        if focus_hf_policy:
            dialog.focus_hf_policy()
        else:
            dialog.select_destination(GENERAL)
        self._raise_dialog(dialog)

    def open_hotkey_settings(self) -> None:
        """Show the singleton Settings window on its Hotkeys destination."""
        dialog = self._prepare_settings_dialog()
        dialog.refresh()
        dialog.select_destination(HOTKEYS)
        self._raise_dialog(dialog)

    def _prepare_settings_dialog(self) -> SettingsDialog:
        dialog = self._ensure_settings_dialog()
        dialog.on_dictation_transcribe = self.on_dictation_transcribe
        dialog.get_meeting_active = self.get_meeting_active
        dialog.on_audio_device_changed = self.on_audio_device_changed
        dialog.on_streaming_settings_changed = self.on_streaming_settings_changed
        dialog.on_streaming_font_changed = self.overlay.refresh_streaming_font_size
        dialog.on_ui_font_scale_changed = self._apply_ui_font_scale
        dialog.on_hf_policy_changed = self.on_hf_policy_changed
        dialog.on_api_keys_changed = self._on_api_keys_changed
        dialog.on_developer_mode_changed = (
            self.main_window.meeting_mode_tab.set_developer_mode
        )
        dialog.on_cleanup_changed = self.refresh_cleanup_controls
        dialog.on_hotkeys_changed = self._on_settings_hotkeys_changed
        dialog.on_recording_trigger_mode_changed = (
            self._on_settings_recording_trigger_mode_changed
        )
        return dialog

    def _ensure_settings_dialog(self):
        if self._settings_dialog is None:
            dialog = SettingsDialog(self.main_window)
            dialog.model_manager_requested.connect(
                self.open_model_manager_dialog
            )
            self._settings_dialog = dialog
        return self._settings_dialog

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

    def _apply_ui_font_scale(self, percent: int) -> None:
        apply_ui_font_scale(percent)
        self.main_window.quick_record_tab.redraw_transcript()
        self.main_window.upload_file_tab.redraw_transcript()
        viewer = getattr(self.main_window.upload_file_tab, "_viewer", None)
        if viewer is not None:
            viewer.refresh_typography()

    def _on_api_keys_changed(self):
        """A key was saved or removed: rebuild clients, then redraw key status."""
        if self.on_api_keys_changed:
            self.on_api_keys_changed()
        self.refresh_model_manager()

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
            tab: Rail destination alias — ``\"ondemand\"``, ``\"text\"``,
                ``\"meeting\"``, or ``\"runtime\"``. ``\"downloads\"`` and the
                legacy ``\"library\"`` / ``\"voice\"`` open the Downloads
                window, which now owns the catalog and components.
                ``"engine_downloads"`` also focuses the selected engine's
                missing runtime, when one has been reported.
        """
        if tab == "engine_downloads":
            component_id = (
                self.get_missing_local_runtime()
                if self.get_missing_local_runtime else None
            )
            self.open_downloads_dialog(component_id=component_id)
            return
        if tab in ("downloads", "library", "voice"):
            self.open_downloads_dialog()
            return

        dialog = self._ensure_model_manager_dialog()
        dialog.refresh()
        if tab == "text":
            dialog.show_text_tab()
        elif tab == "meeting":
            dialog.show_meeting_tab()
        elif tab == "runtime":
            dialog.show_runtime()
        else:
            dialog.show_ondemand_tab()
        self._raise_dialog(dialog)

    def _ensure_model_manager_dialog(self):
        from ui_qt.dialogs.model_manager_dialog import ModelManagerDialog

        if self._model_manager_dialog is None:
            dialog = ModelManagerDialog(
                get_loaded_model=self.get_loaded_local_model,
                parent=self.main_window,
            )
            dialog.on_set_active_requested = self._on_manager_set_active
            dialog.on_backend_changed = self.select_transcription_backend
            dialog.on_runtime_settings_changed = self._on_manager_runtime_changed
            dialog.downloads_requested.connect(self.open_downloads_dialog)
            self._model_manager_dialog = dialog
        return self._model_manager_dialog

    def open_downloads_dialog(self, component_id: Optional[str] = None):
        """Show the non-modal Downloads window (single instance, re-raised)."""
        from ui_qt.dialogs.downloads_dialog import DownloadsDialog

        if self._downloads_dialog is None:
            dialog = DownloadsDialog(
                get_loaded_model=self.get_loaded_local_model,
                parent=self.main_window,
            )
            dialog.on_download_requested = self.on_model_download_requested
            dialog.on_delete_requested = self.on_model_delete_requested
            dialog.on_batch_download_requested = self.on_model_batch_download
            dialog.on_batch_cancel_requested = self.request_model_batch_stop
            dialog.component_install_requested.connect(
                self.on_component_install_requested
            )
            dialog.component_cancel_requested.connect(
                self.on_component_cancel_requested
            )
            dialog.component_remove_requested.connect(
                self.on_component_remove_requested
            )
            self._downloads_dialog = dialog

        self._downloads_dialog.refresh()
        self._raise_dialog(self._downloads_dialog)
        if component_id:
            self._downloads_dialog.focus_component(component_id)

    @staticmethod
    def _raise_dialog(dialog) -> None:
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def select_transcription_backend(self, display_name: str) -> None:
        """Select a dictation backend through the main-window engine-bar path.

        Args:
            display_name: A ``config.MODEL_CHOICES`` label such as
                ``\"Local Whisper\"``.
        """
        tabs = getattr(self.main_window, "transcription_tabs", None)
        if not display_name or not tabs:
            return
        tabs[0].choose_backend(display_name)

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
        if self._downloads_dialog is not None and self._downloads_dialog.isVisible():
            self._downloads_dialog.refresh()

    def on_model_download_started(self, model_name: str):
        self.main_window.quick_record_tab.set_model_downloading(model_name, True)
        self.main_window.upload_file_tab.set_model_downloading(model_name, True)
        if self._downloads_dialog is not None:
            self._downloads_dialog.set_downloading(model_name)
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.set_downloading(model_name)
        downloads_visible = (
            self._downloads_dialog is not None
            and self._downloads_dialog.isVisible()
        )
        manager_visible = (
            self._model_manager_dialog is not None
            and self._model_manager_dialog.isVisible()
        )
        if not downloads_visible and not manager_visible:
            self._show_download_progress(model_name)

    def on_model_download_progress(self, model_name: str, done: int, total: int):
        if self._downloads_dialog is not None and hasattr(
            self._downloads_dialog, "set_download_progress"
        ):
            self._downloads_dialog.set_download_progress(model_name, done, total)
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.set_download_progress(model_name, done, total)
        self._update_download_progress(model_name, done, total)

    def on_model_download_finished(self, model_name: str, success: bool):
        self.main_window.quick_record_tab.set_model_downloading(model_name, False)
        self.main_window.upload_file_tab.set_model_downloading(model_name, False)
        if self._downloads_dialog is not None:
            self._downloads_dialog.finish_download(model_name, success)
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.finish_download(model_name, success)
        self._hide_download_progress()
        self.refresh_model_manager()

    def on_model_batch_planned(self, model_names):
        if self._downloads_dialog is not None:
            self._downloads_dialog.begin_batch(list(model_names))

    def on_model_batch_finished(self, completed: int, planned: int):
        if self._downloads_dialog is not None:
            self._downloads_dialog.finish_batch(completed, planned)

    def request_model_batch_stop(self):
        if self.on_model_batch_stop:
            self.on_model_batch_stop()

    def _show_download_progress(self, model_name: str) -> None:
        dialog = QProgressDialog(
            f'Downloading "{model_name}"…',
            None,
            0,
            0,
            self.main_window,
        )
        dialog.setWindowTitle("Downloading model")
        dialog.setMinimumDuration(0)
        dialog.setCancelButton(None)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        dialog.setMinimumWidth(360)
        dialog.show()
        self._download_progress_dialog = dialog

    def _update_download_progress(
        self, model_name: str, done: int, total: int
    ) -> None:
        dialog = self._download_progress_dialog
        if dialog is None:
            return
        if total <= 0:
            dialog.setRange(0, 0)
            dialog.setLabelText(f'Downloading "{model_name}"…')
            return
        dialog.setRange(0, 100)
        dialog.setValue(int(done * 100 / total))
        from services.format_utils import format_size_bytes

        dialog.setLabelText(
            f'Downloading "{model_name}"… '
            f"{format_size_bytes(done)} of {format_size_bytes(total)}"
        )

    def _hide_download_progress(self) -> None:
        dialog = self._download_progress_dialog
        self._download_progress_dialog = None
        if dialog is not None:
            dialog.close()

    def on_model_deleted(self, model_name: str, success: bool, error: str):
        if self._downloads_dialog is not None:
            self._downloads_dialog.show_delete_result(model_name, success, error)
            self._downloads_dialog.refresh()
        self.refresh_model_manager()

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
        if self._downloads_dialog is not None:
            self._downloads_dialog.set_component_progress(
                component_id, phase, done, total
            )

    def on_component_state_changed(self):
        if self._downloads_dialog is not None:
            self._downloads_dialog.refresh_components()
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.refresh_component_state()

    def on_component_install_finished(
        self, component_id: str, success: bool, message: str
    ):
        if self._downloads_dialog is not None:
            self._downloads_dialog.finish_component_install(
                component_id, success, message
            )
        if self._model_manager_dialog is not None:
            self._model_manager_dialog.refresh_component_state()

    def ensure_meeting_platform_ack(self) -> bool:
        """Clear platform access gates before Meeting Mode opens or starts.

        Unsupported OSes still use the acknowledgement dialog. Supported
        platforms unlock the tab. Linux system-audio readiness is handled
        separately by :meth:`ensure_meeting_start_readiness` so a missing
        monitor never greys out the whole tab.

        Returns:
            True when Meeting Mode may open on this platform.
        """
        from ui_qt.dialogs.meeting_unsupported_dialog import (
            acknowledge_unsupported_meeting_mode,
        )

        if not acknowledge_unsupported_meeting_mode(self.main_window):
            return False
        self.main_window.tabbed_content.unlock_meeting_tab()
        return True

    def ensure_meeting_start_readiness(self) -> Optional[str]:
        """Return the session system-audio policy for a meeting start.

        Returns:
            ``auto``, ``required``, or ``disabled`` when the start may
            proceed; ``None`` when the user cancelled.
        """
        import sys

        from meeting.platform import linux_meeting_implementation_ready
        from ui_qt.dialogs.meeting_linux_audio_dialog import (
            MeetingLinuxAudioDialog,
            ensure_meeting_linux_system_audio,
        )
        from ui_qt.dialogs.meeting_system_audio_dialog import (
            ensure_meeting_system_audio_permission,
        )
        from ui_qt.dialogs.meeting_unsupported_dialog import (
            acknowledge_unsupported_meeting_mode,
        )

        if not acknowledge_unsupported_meeting_mode(self.main_window):
            return None
        if not ensure_meeting_system_audio_permission(self.main_window):
            return None
        self.main_window.tabbed_content.unlock_meeting_tab()

        # Linux implementation is complete for x86_64/aarch64 even while public
        # promotion remains gated; still run remediation after the ack.
        if sys.platform.startswith("linux") and linux_meeting_implementation_ready():
            decision = ensure_meeting_linux_system_audio(self.main_window)
            if decision == MeetingLinuxAudioDialog.RESULT_CANCEL:
                return None
            if decision == MeetingLinuxAudioDialog.RESULT_MICROPHONE_ONLY:
                return "disabled"
            return "required"
        return "auto"

    def _on_meeting_start_requested(self, cloud_enabled: bool):
        policy = self.ensure_meeting_start_readiness()
        if policy is None:
            return
        if self.on_meeting_start:
            self.on_meeting_start(cloud_enabled, system_audio_policy=policy)

    def _on_meeting_demo_requested(self, cloud_enabled: bool):
        # Demo mode seeds canned transcript data and never opens capture. Keep
        # the platform-preview disclosure, but do not probe or remediate audio.
        if not self.ensure_meeting_platform_ack():
            return
        if self.on_meeting_start_demo:
            self.on_meeting_start_demo(
                cloud_enabled,
                system_audio_policy="disabled",
            )

    def _on_meeting_cloud_toggled(self, enabled: bool):
        if self.on_meeting_toggle_cloud:
            self.on_meeting_toggle_cloud(enabled)

    def _on_meeting_open_dashboard(self):
        if self.on_meeting_open_dashboard:
            self.on_meeting_open_dashboard()

    def _on_meeting_open_report(self):
        if self.on_meeting_open_report:
            self.on_meeting_open_report()

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
        policy = self.ensure_meeting_start_readiness()
        if policy is None:
            return
        if self.on_meeting_start_new:
            self.on_meeting_start_new(cloud_enabled, system_audio_policy=policy)

    def _on_past_meeting_requested(self, meeting_id: str) -> None:
        if self.on_meeting_open_past:
            self.on_meeting_open_past(meeting_id)

    def _on_past_meeting_copy_requested(self, meeting_id: str) -> None:
        if not self.on_meeting_copy_transcript:
            return
        text = self.on_meeting_copy_transcript(meeting_id)
        if text and self.copy_to_clipboard(text):
            self.set_meeting_status("Transcript copied")
            self.show_copied_animation()

    def _on_past_meeting_delete_requested(
        self, meeting_id: str, delete_recordings: bool = True
    ) -> None:
        if self.on_meeting_delete_past:
            self.on_meeting_delete_past(meeting_id, delete_recordings)

    def _on_past_meetings_clear_requested(self, delete_recordings: bool) -> None:
        if self.on_meeting_clear_past:
            self.on_meeting_clear_past(delete_recordings)

    def _on_tray_meeting_toggle(self):
        if self._meeting_active:
            self._on_meeting_end_requested()
            return
        policy = self.ensure_meeting_start_readiness()
        if policy is None:
            return
        if self.on_meeting_start:
            self.on_meeting_start(None, system_audio_policy=policy)

    def _on_meeting_end_requested(self) -> None:
        """Confirm the irreversible capture stop before ending a meeting."""
        if not self._meeting_active or not self.on_meeting_end:
            return
        answer = QMessageBox.question(
            self.main_window,
            "End meeting?",
            "End this meeting now? Audio capture will stop immediately. "
            "OpenWhisper will keep working in the background to finish the "
            "transcript and report.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.on_meeting_end()

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
        if payload.get("dashboard_available") is False:
            self._meeting_urls = {}
        has_dashboard = bool(
            self._meeting_urls.get("host_url") or self._meeting_urls.get("url")
        )
        # Always tell the tab whether Open Dashboard can work, including during
        # post-meeting finalization when capture is already inactive.
        tab_payload = dict(payload)
        tab_payload.setdefault("dashboard_available", has_dashboard)
        self.main_window.meeting_mode_tab.set_meeting_state(tab_payload)
        if payload.get("active"):
            self.main_window.history_sidebar.set_selected_past_meeting(None)
        elif "meeting_id" in payload:
            meeting_id = payload.get("meeting_id")
            self.main_window.history_sidebar.set_selected_past_meeting(
                str(meeting_id) if meeting_id else None
            )
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

    def _on_settings_hotkeys_changed(self, hotkeys: dict) -> None:
        if self.on_hotkeys_changed:
            self.on_hotkeys_changed(hotkeys)
        else:
            settings_manager.save_hotkey_settings(hotkeys)
        self.update_hotkey_display(hotkeys)

    def _on_settings_recording_trigger_mode_changed(self, mode: str) -> None:
        if self.on_recording_trigger_mode_changed:
            self.on_recording_trigger_mode_changed(mode)
        else:
            settings_manager.save_setting(
                SettingsKey.RECORDING_TRIGGER_MODE, mode
            )

    def _on_upload_file_transcribe(
        self, audio_path: str, duration_seconds: float = 0.0
    ):
        self._transcription_source_tab = TabbedContentWidget.TAB_UPLOAD_FILE
        logger.info(f"Upload tab transcription started: {audio_path}")
        if self.on_upload_audio:
            self.on_upload_audio(audio_path, duration_seconds)

    def _on_upload_files_transcribe(self, request) -> None:
        self._transcription_source_tab = TabbedContentWidget.TAB_UPLOAD_FILE
        logger.info(
            "Upload tab batch transcription started: %d files", len(request.items)
        )
        if self.on_upload_audio_files:
            self.on_upload_audio_files(request)

    def _on_upload_cancel(self) -> None:
        """Stop an upload job without touching the recording state.

        ``cancel_recording`` is for the Quick Record tab: it flips the recording
        flag and clears that tab's transcript, neither of which an upload owns.
        """
        if not self._upload_job_active():
            return
        callback = self.on_upload_cancel or self.on_record_cancel
        if callback:
            callback()

    def set_batch_progress(self, position: int, total: int, source_name: str) -> None:
        if self._upload_job_active():
            self.main_window.upload_file_tab.set_batch_progress(
                position, total, source_name
            )

    def set_batch_item_finished(
        self, position: int, success: bool, transcript: str = ""
    ) -> None:
        if self._upload_job_active():
            self.main_window.upload_file_tab.set_batch_item_finished(
                position, success, transcript
            )

    def set_batch_result(self, result) -> None:
        """Pass completed batch structure to the Upload File reading window."""
        if self._transcription_source_tab == TabbedContentWidget.TAB_UPLOAD_FILE:
            self.main_window.upload_file_tab.set_batch_result(result)

    def _on_quick_record_copy(self, text: str):
        succeeded = self.copy_to_clipboard(text)
        self.main_window.quick_record_tab.show_copy_result(succeeded)

    def _on_upload_copy(self, text: str):
        """Copy Upload File transcript text through the shared clipboard path."""
        if self.copy_to_clipboard(text):
            self.main_window.upload_file_tab.set_status("Copied to clipboard")
            self.show_copied_animation()
        else:
            self.main_window.upload_file_tab.set_status("Copy failed")

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
        if self._update_dialog is not None:
            self._update_dialog.raise_()
            self._update_dialog.activateWindow()
            return
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
        dialog.on_cancel_requested = self._on_update_cancel_requested
        dialog.on_setup_requested = self._on_update_setup_requested
        dialog.exec()
        if dialog.result_action == AppUpdateDialog.RESULT_PRIMARY:
            if (
                result is not None
                and result.release is not None
                and not result.can_apply
            ):
                QDesktopServices.openUrl(QUrl(result.release.html_url))
            elif result is None:
                QDesktopServices.openUrl(QUrl(RELEASES_PAGE_URL))
        # exec() has returned, so the dialog is closed: a download the user
        # walked away from must not report into a dead widget.
        if self._update_dialog is dialog:
            self._update_dialog = None

    def _update_work_is_busy(self) -> Optional[str]:
        if self.is_recording:
            return "a recording"
        if self.get_meeting_active and self.get_meeting_active():
            return "a meeting"
        if self.get_transcribing and self.get_transcribing():
            return "transcription"
        if self.get_component_installing and self.get_component_installing():
            return "a component install"
        return None

    def _confirm_update_while_busy(self) -> bool:
        busy = self._update_work_is_busy()
        if not busy:
            return True
        answer = QMessageBox.question(
            self.main_window,
            "Install update?",
            f"OpenWhisper is busy with {busy}. Install the update anyway? "
            "Unsaved work in that task may be lost.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _on_update_download_requested(self, result: UpdateCheckResult) -> None:
        if not self._confirm_update_while_busy():
            if self._update_dialog is not None:
                self._update_dialog.set_error("The update was not started.")
            return
        self._update_canceled = False
        if self.on_update_download:
            self.on_update_download(result)

    def _on_update_setup_requested(self, result: UpdateCheckResult) -> None:
        if not self._confirm_update_while_busy():
            if self._update_dialog is not None:
                self._update_dialog.set_error("The update was not started.")
            return
        self._update_canceled = False
        if self.on_update_download:
            self.on_update_download(result, True)

    def _on_update_cancel_requested(self) -> None:
        self._update_canceled = True
        if self.on_update_cancel:
            self.on_update_cancel()

    def on_update_download_progress(self, phase: str, done: int, total: int) -> None:
        """Forward installer-download progress to the open dialog."""
        if self._update_dialog is not None:
            self._update_dialog.set_progress(phase, done, total)

    def on_update_download_finished(self, path: str, error: str) -> None:
        """Start the native helper or setup exe and quit, or show the error."""
        if error:
            offer_setup = False
            result = self._last_update_result
            if (
                result is not None
                and result.apply_mode == ApplyMode.NATIVE
                and result.release is not None
                and result.release.setup_asset is not None
                and result.release.setup_asset.sha256
            ):
                offer_setup = True
            if self._update_dialog is not None:
                self._update_dialog.set_error(error, offer_setup=offer_setup)
            elif self._update_canceled:
                # The user asked to stop and closed the dialog; a modal
                # "Update failed" box for their own cancel is just noise.
                logger.info("Update stopped after cancel: %s", error)
                self.set_status("Update canceled")
            else:
                QMessageBox.warning(
                    self.main_window, "Update failed", error
                )
            return
        if self._update_canceled:
            abandon = getattr(self, "on_update_abandon", None)
            if abandon:
                abandon(path)
            if self._update_dialog is not None:
                self._update_dialog.set_error("The update was canceled.")
            return

        if not self._confirm_update_while_busy():
            abandon = getattr(self, "on_update_abandon", None)
            if abandon:
                abandon(path)
            if self._update_dialog is not None:
                self._update_dialog.set_error("The update was not installed. Your work is still open.")
            return

        transaction_id = decode_native_result(path)
        if transaction_id:
            try:
                journal = load_journal(transaction_id)
                exe = helper_exe_for(journal)
                args = helper_argv(journal)
            except Exception as exc:
                abandon = getattr(self, "on_update_abandon", None)
                if abandon:
                    abandon(path)
                message = str(exc) or "The updater helper could not be prepared."
                if self._update_dialog is not None:
                    self._update_dialog.set_error(message, offer_setup=True)
                else:
                    QMessageBox.warning(self.main_window, "Update failed", message)
                return
            if self._update_dialog is not None:
                self._update_dialog.set_progress(DownloadPhase.RESTARTING, 1, 1)
        else:
            from services.app_update_apply import discover_install_registration, running_app_dir
            from services.app_update import updates_dir

            exe = path
            registration = discover_install_registration()
            scope = "/ALLUSERS" if registration and registration.hive == "HKLM" else "/CURRENTUSER"
            args = [
                "/SP-", "/SILENT", "/NORESTART", "/OPENWHISPERUPDATE=1", scope,
                "/DIR=" + running_app_dir(),
                "/LOG=" + os.path.join(updates_dir(), "setup.log"),
            ]
            release_application_mutex_for_setup()

        launched = _start_detached(exe, args)
        if not launched:
            if transaction_id:
                abandon = getattr(self, "on_update_abandon", None)
                if abandon:
                    abandon(path)
            if not transaction_id:
                acquire_application_mutex_or_exit()
            message = (
                "The updater could not be started."
                if transaction_id
                else "The installer could not be started."
            )
            if self._update_dialog is not None:
                self._update_dialog.set_error(
                    message, offer_setup=bool(transaction_id)
                )
            else:
                QMessageBox.warning(self.main_window, "Update failed", message)
            return
        self.exit_for_update()

    def exit_for_update(self) -> None:
        """Leave immediately so the launched updater can replace this install.

        ``QApplication.quit()`` is only a request: Qt asks every window to
        close first and abandons the quit if one refuses. Both the open update
        dialog (busy, so it declines) and the main window (minimize-to-tray
        ignores the close) veto it, which left the app running while the
        updater waited for a process that would never exit. ``exit()`` cannot
        be vetoed.
        """
        if self._update_dialog is not None:
            self._update_dialog.mark_handed_off()
        try:
            self.main_window._force_quit = True
        except Exception as exc:
            logger.debug("Could not mark the main window for force quit: %s", exc)
        arm_handoff_watchdog()
        QApplication.instance().exit(0)

    def show_apply_error_if_any(self) -> None:
        """Show a leftover native-update error from the previous launch."""
        message = consume_apply_error()
        if not message:
            return
        QMessageBox.warning(self.main_window, "Update failed", message)

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
            self._temporary_clipboard.cleanup()
        except Exception as e:
            logger.debug(f"Error restoring clipboard during cleanup: {e}")

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
