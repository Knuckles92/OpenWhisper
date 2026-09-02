import os
import pytest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QScrollArea
from PyQt6.QtCore import Qt, QMimeData, QUrl, QPointF
from PyQt6.QtGui import QDragEnterEvent, QDropEvent

from services.audio_processor import AudioFilePreview
from services.format_utils import format_sample_rate
from ui_qt.overlay_state import OverlayState
from ui_qt.widgets.transcription_progress import (
    ProgressStage,
    TranscriptionProgressPanel,
    format_elapsed,
    stage_for_overlay_state,
)
from services.settings import SettingsKey
from ui_qt.widgets.engine_field import EngineStatus
from ui_qt.widgets.decode_label import DecodeLabel
from ui_qt.widgets.upload_file_tab import UploadFileTab, DropZoneWidget, FileInfoCard
from ui_qt.widgets.transcription_tab_base import TranscriptionTabBase


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class TestDropZoneWidget:
    def test_init_properties(self):
        widget = DropZoneWidget()
        assert widget.acceptDrops()
        assert widget.minimumHeight() == 150
        assert widget.objectName() == "dropZone"

    def test_is_valid_audio(self):
        widget = DropZoneWidget()
        assert widget._is_valid_audio("sample.wav")
        assert widget._is_valid_audio("audio.MP3")
        assert widget._is_valid_audio("voice.m4a")
        assert widget._is_valid_audio("track.ogg")
        assert widget._is_valid_audio("music.flac")
        assert widget._is_valid_audio("test.wma")
        assert not widget._is_valid_audio("document.pdf")
        assert not widget._is_valid_audio("image.png")
        assert not widget._is_valid_audio("video.mp4")

    def test_drag_enter_accepts_valid_audio(self):
        widget = DropZoneWidget()
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile("/path/to/test.wav")])

        event = QDragEnterEvent(
            QPointF(10, 10).toPoint(),
            Qt.DropAction.CopyAction,
            mime_data,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        widget.dragEnterEvent(event)
        assert event.isAccepted()

    def test_drag_enter_rejects_invalid_file(self):
        widget = DropZoneWidget()
        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile("/path/to/test.txt")])

        event = QDragEnterEvent(
            QPointF(10, 10).toPoint(),
            Qt.DropAction.CopyAction,
            mime_data,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        widget.dragEnterEvent(event)
        assert not event.isAccepted()

    def test_drop_event_emits_file_selected(self):
        widget = DropZoneWidget()
        emitted = []
        widget.file_selected.connect(emitted.append)

        mime_data = QMimeData()
        mime_data.setUrls([QUrl.fromLocalFile("/path/to/sample.mp3")])

        event = QDropEvent(
            QPointF(10, 10),
            Qt.DropAction.CopyAction,
            mime_data,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        widget.dropEvent(event)
        assert len(emitted) == 1
        assert "sample.mp3" in emitted[0]


def _preview(**overrides):
    values = dict(
        file_path="sample.wav",
        file_name="sample.wav",
        file_size_mb=1.0,
        duration_seconds=60.0,
        sample_rate=44100,
        channels=2,
        needs_splitting=False,
        estimated_chunks=1,
    )
    values.update(overrides)
    return AudioFilePreview(**values)


class TestFileInfoCard:
    def test_set_preview_fills_the_chips(self):
        card = FileInfoCard()
        card.set_preview(_preview())

        assert card.filename_label.text() == "sample.wav"
        assert card.size_chip.text() == "1.0 MB"
        assert card.duration_chip.text() == "1m 0s"
        assert card.rate_chip.text() == "44.1 kHz"
        assert card.channels_chip.text() == "Stereo"
        assert card.chunk_label.text() == "One pass"
        assert card.chunk_label.property("tone") == "ok"

    def test_large_file_shows_the_chunk_count_as_a_warning(self):
        card = FileInfoCard()
        card.set_preview(_preview(needs_splitting=True, estimated_chunks=5))

        assert card.chunk_label.text() == "5 chunks"
        assert card.chunk_label.property("tone") == "warn"

    def test_set_transcribing_swaps_the_footer_for_progress(self):
        card = FileInfoCard()
        card.set_transcribing(True, with_cleanup=True)
        assert card.is_transcribing
        assert card.is_progress_shown
        assert not card.transcribe_btn.isEnabled()
        assert not card.remove_btn.isEnabled()
        assert card.progress.stage is ProgressStage.PREPARING
        assert not card.progress.steps[2].isHidden()

        card.set_transcribing(False)
        assert not card.is_transcribing
        assert not card.is_progress_shown
        assert card.transcribe_btn.isEnabled()
        assert card.remove_btn.isEnabled()

    def test_cleanup_step_only_appears_when_cleanup_is_on(self):
        card = FileInfoCard()
        card.set_transcribing(True, with_cleanup=False)
        assert card.progress.steps[2].isHidden()

    def test_finish_holds_the_result_before_the_actions_return(self):
        """The user should see Done land, not have the panel vanish under them."""
        card = FileInfoCard()
        shown = []
        card.progress_shown.connect(shown.append)
        card.set_transcribing(True, with_cleanup=True)

        card.finish_transcribing(success=True)
        assert not card.is_transcribing
        assert card.is_progress_shown
        assert card.progress.stage is ProgressStage.DONE
        assert all(chip.state == "done" for chip in card.progress.steps)
        assert card._settle_timer.isActive()

        card._settle_timer.timeout.emit()
        assert not card.is_progress_shown
        assert shown == [True, False]

    def test_starting_again_cancels_a_pending_return_to_actions(self):
        card = FileInfoCard()
        card.set_transcribing(True)
        card.finish_transcribing(success=True)

        card.set_transcribing(True)
        assert not card._settle_timer.isActive()
        assert card.progress.stage is ProgressStage.PREPARING

    def test_set_copy_enabled(self):
        card = FileInfoCard()
        card.set_copy_enabled(True)
        assert card.copy_btn._active

        card.set_copy_enabled(False)
        assert not card.copy_btn._active


class TestTranscriptionProgressPanel:
    def test_overlay_states_map_onto_stages(self):
        assert stage_for_overlay_state(OverlayState.PROCESSING) is ProgressStage.PREPARING
        assert stage_for_overlay_state(OverlayState.TRANSCRIBING) is ProgressStage.TRANSCRIBING
        assert stage_for_overlay_state(OverlayState.CLEANING) is ProgressStage.CLEANING
        assert stage_for_overlay_state(OverlayState.CANCELING) is ProgressStage.CANCELED
        assert stage_for_overlay_state(OverlayState.RECORDING) is None
        assert stage_for_overlay_state(OverlayState.NONE) is None

    def test_steps_follow_the_stage(self):
        panel = TranscriptionProgressPanel()
        panel.start(with_cleanup=True)
        assert [chip.state for chip in panel.steps] == ["active", "pending", "pending"]

        panel.set_stage(ProgressStage.TRANSCRIBING)
        assert [chip.state for chip in panel.steps] == ["done", "active", "pending"]

        panel.set_stage(ProgressStage.CLEANING)
        assert [chip.state for chip in panel.steps] == ["done", "done", "active"]

    def test_cancel_marks_the_step_that_was_running(self):
        panel = TranscriptionProgressPanel()
        panel.start(with_cleanup=True)
        panel.set_stage(ProgressStage.TRANSCRIBING)

        panel.apply_overlay_state(OverlayState.CANCELING)
        assert panel.stage is ProgressStage.CANCELED
        assert not panel.is_running
        assert [chip.state for chip in panel.steps] == ["done", "failed", "pending"]

    def test_large_file_notice_names_the_size(self):
        panel = TranscriptionProgressPanel()
        panel.start(with_cleanup=False)

        panel.set_large_file(30.0, is_splitting=True)
        assert panel.stage is ProgressStage.SPLITTING
        assert panel.detail_label.text() == "30.0 MB file"

    def test_bar_sweeps_while_running_and_fills_when_done(self):
        panel = TranscriptionProgressPanel()
        panel.start(with_cleanup=False)
        assert panel.bar.is_indeterminate

        panel.finish(success=True)
        assert not panel.bar.is_indeterminate
        assert panel.bar.fraction == 1.0

    def test_elapsed_format(self):
        assert format_elapsed(0) == "0:00"
        assert format_elapsed(65.9) == "1:05"


class TestDecodeLabel:
    SEGMENTS = [("Transcribed in ", False), ("6.9s", True)]

    def test_immediate_text_renders_emphasis(self):
        label = DecodeLabel()
        label.set_segments(self.SEGMENTS, animate=False)
        assert "Transcribed in " in label.text()
        assert "<span style='color:#f5f5f7; font-weight:600'>6.9s</span>" in label.text()
        assert not label.is_revealing

    def test_reveal_waits_until_the_label_is_shown(self):
        """A result arriving behind the progress panel must not spend its animation unseen."""
        label = DecodeLabel()
        label.set_segments(self.SEGMENTS)
        assert not label.is_revealing
        assert "6.9s" in label.text()

        label.show()
        QApplication.processEvents()
        assert label.is_revealing

    def test_partial_frame_scrambles_ahead_of_the_locked_run(self):
        label = DecodeLabel()
        label.set_segments(self.SEGMENTS, animate=False)
        frame = label._render(locked=5, scramble_head=3)
        assert frame.startswith("Trans")
        assert frame.count("color:#0a84ff") == 3
        assert "6.9s" not in frame

    def test_finished_frame_matches_the_immediate_text(self):
        label = DecodeLabel()
        label.set_segments(self.SEGMENTS, animate=False)
        assert label._render(locked=len("Transcribed in 6.9s")) == label.text()

    def test_hiding_mid_reveal_lands_on_the_final_text(self):
        label = DecodeLabel()
        label.set_segments(self.SEGMENTS)
        label.show()
        QApplication.processEvents()
        label.hide()
        assert not label.is_revealing
        assert QLabel.text(label) == label.text()


def test_format_sample_rate():
    assert format_sample_rate(44100) == "44.1 kHz"
    assert format_sample_rate(16000) == "16 kHz"
    assert format_sample_rate(48000) == "48 kHz"
    assert format_sample_rate(0) == ""


class TestUploadFileTab:
    def test_init_structure(self):
        tab = UploadFileTab()
        assert isinstance(tab.scroll_area, QScrollArea)
        assert tab.scroll_area.widgetResizable()
        assert not tab.drop_zone.isHidden()
        assert tab.file_info_card.isHidden()
        assert tab.status_label.text() == "Select an audio file to transcribe"
        assert tab.is_transcription_collapsed()

    @patch("ui_qt.widgets.upload_file_tab.audio_processor.preview_file")
    def test_file_selected_flow(self, mock_preview):
        mock_preview.return_value = AudioFilePreview(
            file_path="recording.wav",
            file_name="recording.wav",
            file_size_mb=0.002,
            duration_seconds=5.0,
            sample_rate=16000,
            channels=1,
            needs_splitting=False,
            estimated_chunks=1,
        )

        tab = UploadFileTab()
        tab._on_file_selected("recording.wav")

        assert tab.drop_zone.isHidden()
        assert not tab.file_info_card.isHidden()
        assert tab.status_label.text() == "Ready to transcribe"
        assert tab._audio_path == "recording.wav"

    def test_clear_file_flow(self):
        tab = UploadFileTab()
        tab._audio_path = "some_file.wav"
        tab.drop_zone.hide()
        tab.file_info_card.show()

        tab.clear_file()
        assert tab._audio_path is None
        assert not tab.drop_zone.isHidden()
        assert tab.file_info_card.isHidden()
        assert tab.status_label.text() == "Select an audio file to transcribe"

    def test_transcript_display_and_copy_signal(self):
        tab = UploadFileTab()
        copied = []
        tab.copy_requested.connect(copied.append)

        tab.set_transcript("Hello world transcription")
        assert tab.transcript_text.toPlainText() == "Hello world transcription"
        assert tab.file_info_card.copy_btn._active

        tab._on_copy()
        assert copied == ["Hello world transcription"]

    def _tab_with_file(self, tmp_path):
        audio = tmp_path / "recording.wav"
        audio.write_bytes(b"\0" * 64)
        tab = UploadFileTab()
        with patch(
            "ui_qt.widgets.upload_file_tab.audio_processor.preview_file",
            return_value=_preview(file_path=str(audio), file_name="recording.wav"),
        ):
            tab._on_file_selected(str(audio))
        return tab

    def test_transcribe_locks_the_tab_and_shows_progress_in_the_card(self, tmp_path):
        tab = self._tab_with_file(tmp_path)
        requested = []
        tab.upload_requested.connect(lambda path, duration: requested.append((path, duration)))

        tab._on_transcribe()

        assert requested and requested[0][1] == 60.0
        assert tab.is_transcribing
        assert tab.file_info_card.is_progress_shown
        assert tab.status_label.isHidden()
        assert not tab.model_combo.isEnabled()

    def test_overlay_states_and_status_text_land_in_the_panel(self, tmp_path):
        tab = self._tab_with_file(tmp_path)
        tab._on_transcribe()
        panel = tab.file_info_card.progress

        tab.set_large_file_stage(30.0, is_splitting=True)
        assert panel.stage is ProgressStage.SPLITTING

        tab.set_progress_state(OverlayState.TRANSCRIBING)
        assert panel.stage is ProgressStage.TRANSCRIBING

        tab.set_status("Transcribing chunk 1/3...")
        assert panel.detail_label.text() == "Transcribing chunk 1/3..."
        assert tab.status_label.text() == "Transcribing chunk 1/3..."

    def test_transcript_finishes_the_job_and_restores_the_status_line(self, tmp_path):
        tab = self._tab_with_file(tmp_path)
        tab._on_transcribe()

        tab.set_transcript("Hello there")
        assert not tab.is_transcribing
        assert tab.file_info_card.progress.stage is ProgressStage.DONE
        assert tab.model_combo.isEnabled()
        assert tab.file_info_card.copy_btn._active

        # A NONE routed after the result is a no-op: the job is already over.
        tab.set_progress_state(OverlayState.NONE)
        assert tab.file_info_card.progress.stage is ProgressStage.DONE

        tab.file_info_card._settle_timer.timeout.emit()
        assert not tab.file_info_card.is_progress_shown
        assert not tab.status_label.isHidden()

    def test_stats_land_in_the_card_not_the_strip(self, tmp_path):
        """Duration and size are already chips; only the time is new information."""
        tab = self._tab_with_file(tmp_path)
        tab._on_transcribe()
        tab.set_transcript("Hello there")

        tab.set_transcription_stats(6.9, 10.2, 875 * 1024)

        note = tab.file_info_card.result_label
        assert not note.isHidden()
        assert "6.9s" in note.text()
        assert "1.5\u00d7" in note.text()
        assert tab.stats_widget.isHidden()

        tab.clear_transcription_stats()
        assert note.isHidden()

    def test_starting_a_job_clears_the_previous_result_note(self, tmp_path):
        tab = self._tab_with_file(tmp_path)
        tab.set_transcription_stats(6.9, 10.2, 1024)

        tab._on_transcribe()
        assert tab.file_info_card.result_label.isHidden()

    def test_error_transcript_marks_the_job_failed(self, tmp_path):
        tab = self._tab_with_file(tmp_path)
        tab._on_transcribe()

        tab.set_transcript("Error: model not downloaded")
        assert tab.file_info_card.progress.stage is ProgressStage.FAILED
        assert not tab.file_info_card.copy_btn._active

    def test_none_while_running_releases_the_card(self, tmp_path):
        """A job refused before it produced anything must not leave the tab locked."""
        tab = self._tab_with_file(tmp_path)
        tab._on_transcribe()

        tab.set_progress_state(OverlayState.NONE)
        assert not tab.is_transcribing
        assert tab.model_combo.isEnabled()
        assert tab.file_info_card.progress.stage is ProgressStage.FAILED

    def test_progress_state_is_ignored_when_nothing_is_running(self):
        tab = UploadFileTab()
        tab.set_progress_state(OverlayState.TRANSCRIBING)
        assert tab.file_info_card.progress.stage is None

    def test_scroll_area_handles_constrained_height(self):
        """Scroll area preserves layout and avoids overlapping under short window."""
        tab = UploadFileTab()
        tab.resize(500, 350)
        tab.show()
        QApplication.processEvents()

        assert tab.scroll_area.isVisible()
        assert tab.content_layout.count() > 0
        assert tab.drop_zone.minimumHeight() == 150

    def test_expand_and_collapse_transcription_with_scroll_area(self):
        """Toggling the transcript maintains clean layout without errors."""
        tab = UploadFileTab()
        tab.resize(605, 580)
        tab.show()
        QApplication.processEvents()

        engine_height = tab.engine_card.height()

        tab.set_transcription_collapsed(False)
        QApplication.processEvents()
        assert not tab.is_transcription_collapsed()

        tab.set_transcription_collapsed(True)
        QApplication.processEvents()
        assert tab.is_transcription_collapsed()
        assert tab.engine_card.height() == engine_height


class TestEngineCard:
    def test_card_height_is_stable_across_backends(self):
        """Switching backends must not resize the card, and so not the window."""
        tab = UploadFileTab()
        tab.resize(605, 580)
        tab.show()
        QApplication.processEvents()

        local_height = tab.engine_card.height()

        tab.set_local_engine_visible(False)
        QApplication.processEvents()
        assert tab.engine_card.height() == local_height
        assert not tab.local_engine.isVisible()

        tab.set_local_engine_visible(True)
        QApplication.processEvents()
        assert tab.engine_card.height() == local_height
        assert tab.local_engine.isVisible()

    def test_cleanup_toggle_stays_reachable_on_api_backend(self):
        """AI cleanup applies to every backend, so it outlives the local fields."""
        tab = UploadFileTab()
        tab.show()
        tab.set_local_engine_visible(False)
        QApplication.processEvents()

        assert tab.cleanup_check.isVisible()
        assert tab.manage_models_button.isVisible()

    def test_card_does_not_raise_the_content_width_floor(self):
        """Four fields in one row have to fit the column's floor, not widen it.

        Asserted against the column rather than a pixel budget because the
        fields size to their text, which varies with the system font.
        """
        tab = UploadFileTab()
        column = tab.engine_card.parentWidget()

        assert tab.engine_card.minimumSizeHint().width() <= column.minimumWidth()

    def test_resolved_readout_clears_for_api_backends(self):
        tab = UploadFileTab()
        tab.set_device_info("turbo | cuda (float16)", True)
        assert tab.resolved_label.text() == "turbo | cuda (float16)"

        tab.set_device_info("")
        assert tab.resolved_label.text() == ""

    def test_backend_reflects_selection_without_emitting(self):
        tab = UploadFileTab()
        announced = []
        tab.model_changed.connect(announced.append)

        tab.set_backend("API: Whisper")
        assert tab.current_backend() == "API: Whisper"
        assert tab.model_combo.currentText() == "API: Whisper"
        assert announced == []

        tab.choose_backend("Local Whisper")
        assert announced == ["Local Whisper"]

    def test_choosing_the_active_backend_is_a_no_op(self):
        tab = UploadFileTab()
        tab.set_backend("Local Whisper")
        announced = []
        tab.model_changed.connect(announced.append)

        tab.choose_backend("Local Whisper")
        assert announced == []

    def test_backend_locks_while_transcribing(self):
        tab = UploadFileTab()
        tab.set_backend_enabled(False)
        assert not tab.model_combo.isEnabled()

        tab.set_backend_enabled(True)
        assert tab.model_combo.isEnabled()


class TestEngineStatusDots:
    """The dots claim the engine is usable, so they must follow real state."""

    def test_ready_engine_reads_green(self):
        tab = UploadFileTab()
        tab.set_device_info("turbo | cuda (float16)", True)
        assert tab.status_dot.status() is EngineStatus.READY

    def test_unusable_engine_reads_as_attention(self):
        """A missing model is the case the dot exists for."""
        tab = UploadFileTab()
        tab.set_device_info("large-v3 | not downloaded", False)
        assert tab.status_dot.status() is EngineStatus.ATTENTION

    def test_unreported_engine_stays_neutral(self):
        """Nothing has loaded yet at startup; green would be a guess."""
        tab = UploadFileTab()
        tab.set_device_info("turbo | cuda (float16)", True)
        tab.set_device_info("Not initialized")
        assert tab.status_dot.status() is EngineStatus.UNKNOWN

    def test_dots_go_with_the_local_engine(self):
        """An API backend has no local engine for the dots to report on."""
        tab = UploadFileTab()
        tab.show()

        tab.set_local_engine_visible(False)
        QApplication.processEvents()
        assert not tab.status_dot.isVisible()

        tab.set_local_engine_visible(True)
        QApplication.processEvents()
        assert tab.status_dot.isVisible()


class TestLocalEngineFields:
    """Three fields, each of which has to reach settings and trigger a reload."""

    def _controls(self, saved):
        from ui_qt.widgets.local_engine_controls import LocalEngineControls

        stored = {
            SettingsKey.WHISPER_MODEL: "base",
            SettingsKey.WHISPER_DEVICE: "auto",
            SettingsKey.WHISPER_COMPUTE_TYPE: "auto",
        }
        with patch(
            "ui_qt.widgets.local_engine_controls.settings_manager"
        ) as manager:
            manager.load_all_settings.side_effect = lambda: dict(stored)
            manager.save_all_settings.side_effect = saved.append
            controls = LocalEngineControls()
        return controls

    @pytest.mark.parametrize(
        "field, key, value",
        [
            ("model_combo", SettingsKey.WHISPER_MODEL, "large-v3"),
            ("device_combo", SettingsKey.WHISPER_DEVICE, "cpu"),
            ("compute_combo", SettingsKey.WHISPER_COMPUTE_TYPE, "int8"),
        ],
    )
    def test_changing_a_field_persists_it_and_asks_for_a_reload(
        self, field, key, value
    ):
        saved = []
        controls = self._controls(saved)
        reloads = []
        controls.engine_settings_changed.connect(lambda: reloads.append(True))

        with patch(
            "ui_qt.widgets.local_engine_controls.settings_manager"
        ) as manager:
            manager.load_all_settings.return_value = {}
            manager.update_settings.side_effect = (
                lambda updates: saved.append(dict(updates)) or dict(updates)
            )
            getattr(controls, field).setCurrentText(value)

        assert saved[-1][key] == value
        assert reloads == [True]

    def test_a_field_change_persists_all_three_values(self):
        """The reload reads settings, so a partial write would revert the rest."""
        saved = []
        controls = self._controls(saved)
        controls.set_values("small", "cuda", "int8")

        with patch(
            "ui_qt.widgets.local_engine_controls.settings_manager"
        ) as manager:
            manager.load_all_settings.return_value = {}
            manager.update_settings.side_effect = (
                lambda updates: saved.append(dict(updates)) or dict(updates)
            )
            controls.model_combo.setCurrentText("medium")

        assert saved[-1] == {
            SettingsKey.WHISPER_MODEL: "medium",
            SettingsKey.WHISPER_DEVICE: "cuda",
            SettingsKey.WHISPER_COMPUTE_TYPE: "int8",
        }

    def test_set_values_is_silent(self):
        controls = self._controls([])
        reloads = []
        controls.engine_settings_changed.connect(lambda: reloads.append(True))

        controls.set_values("small", "cuda", "float16")
        assert reloads == []
        assert controls.model_combo.currentText() == "small"
        assert controls.device_combo.currentText() == "cuda"
        assert controls.compute_combo.currentText() == "float16"

    def test_busy_locks_every_field(self):
        controls = self._controls([])
        fields = (
            controls.model_combo,
            controls.device_combo,
            controls.compute_combo,
        )

        controls.set_busy(True)
        assert not any(field.isEnabled() for field in fields)

        controls.set_busy(False)
        assert all(field.isEnabled() for field in fields)


class TestMainWindowUploadTabIntegration:
    @patch.object(TranscriptionTabBase, "load_cleanup_setting")
    def test_transcription_tabs_do_not_scroll_at_minimum_height(self, _mock_setting):
        from ui_qt.main_window import MainWindow
        from ui_qt.widgets.tabbed_content import TabbedContentWidget
        from config import config

        window = MainWindow()
        window.resize(config.MAIN_WINDOW_DEFAULT_WIDTH, config.MAIN_WINDOW_MIN_HEIGHT)
        window.show()
        QApplication.processEvents()

        for index, tab in (
            (TabbedContentWidget.TAB_QUICK_RECORD, window.quick_record_tab),
            (TabbedContentWidget.TAB_UPLOAD_FILE, window.upload_file_tab),
        ):
            window.tabbed_content.set_current_index(index)
            QApplication.processEvents()
            assert tab.isVisibleTo(window)
            assert tab.scroll_area.verticalScrollBar().maximum() == 0

        window._force_quit = True
        window.close()
        QApplication.processEvents()
