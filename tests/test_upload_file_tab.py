import os
import pytest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QLabel, QScrollArea
from PyQt6.QtCore import Qt, QMimeData, QUrl, QPointF
from PyQt6.QtGui import QDragEnterEvent, QDropEvent

from services.audio_processor import AudioFilePreview
from services.batch_upload import BatchRelation
from services.format_utils import format_sample_rate
from ui_qt.overlay_state import OverlayState
from ui_qt.widgets.transcription_progress import (
    ProgressStage,
    TranscriptionProgressPanel,
    format_elapsed,
    stage_for_overlay_state,
)
from services.settings import SettingsKey
from ui_qt.dialogs.batch_relation_dialog import BatchRelationDialog
from ui_qt.widgets.engine_field import EngineStatus
from ui_qt.widgets.decode_label import DecodeLabel
from ui_qt.widgets.upload_file_tab import (
    DropZoneWidget,
    FileInfoCard,
    UploadFileTab,
    _audio_paths_from_mime,
)
from ui_qt.widgets.transcription_tab_base import TranscriptionTabBase


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _inline_threads():
    """Run the preview worker on the calling thread so results land at once."""
    return patch(
        "ui_qt.widgets.upload_file_tab._run_in_thread",
        side_effect=lambda target, name: target(),
    )


def _isolated_settings(saved=None):
    """Keep the tab's relation preset off the developer's real settings file."""
    manager = MagicMock()
    manager.load_all_settings.return_value = {}
    if saved is not None:
        manager.save_setting.side_effect = lambda key, value: saved.append((key, value))
    return patch("ui_qt.widgets.upload_file_tab.settings_manager", manager)


def _drop_event(paths):
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(p) for p in paths])
    event = QDropEvent(
        QPointF(10, 10),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    # The event does not own its mime data; without this the QMimeData is
    # freed when this function returns and the handler reads a dangling pointer.
    event._mime_data = mime_data
    return event


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

    def test_drop_event_emits_files_selected(self):
        widget = DropZoneWidget()
        emitted = []
        widget.files_selected.connect(lambda paths, skipped: emitted.append((paths, skipped)))

        event = _drop_event(["/path/to/sample.mp3"])
        widget.dropEvent(event)

        assert len(emitted) == 1
        paths, skipped = emitted[0]
        assert len(paths) == 1 and "sample.mp3" in paths[0]
        assert skipped == 0
        assert event.isAccepted()

    def test_drop_with_mixed_files_emits_the_audio_paths_in_order_and_counts_the_rest(self):
        widget = DropZoneWidget()
        emitted = []
        widget.files_selected.connect(lambda paths, skipped: emitted.append((paths, skipped)))

        widget.dropEvent(
            _drop_event(["/path/a.mp3", "/path/notes.txt", "/path/b.wav"])
        )

        paths, skipped = emitted[0]
        assert [os.path.basename(p) for p in paths] == ["a.mp3", "b.wav"]
        assert skipped == 1

    def test_drop_with_no_audio_is_ignored(self):
        widget = DropZoneWidget()
        emitted = []
        widget.files_selected.connect(lambda paths, skipped: emitted.append(paths))

        event = _drop_event(["/path/notes.txt"])
        widget.dropEvent(event)

        assert emitted == []
        assert not event.isAccepted()

    def test_audio_paths_from_mime_ignores_non_local_urls(self):
        mime_data = QMimeData()
        mime_data.setUrls([QUrl("https://example.com/a.mp3"), QUrl.fromLocalFile("/x/b.mp3")])

        paths, skipped = _audio_paths_from_mime(mime_data)

        assert [os.path.basename(p) for p in paths] == ["b.mp3"]
        assert skipped == 1


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

    def test_stop_emits_cancel(self):
        card = FileInfoCard()
        canceled = []
        card.cancel_clicked.connect(lambda: canceled.append(True))
        card.set_transcribing(True)
        assert card.progress.stop_btn.isEnabled()

        card.progress.stop_btn.click()
        assert canceled == [True]

    def test_a_canceled_panel_stays_canceled_when_the_job_ends(self):
        """The runtime follows a cancel with an error transcript; that is not a failure."""
        card = FileInfoCard()
        card.set_transcribing(True, with_cleanup=True)
        card.progress.set_stage(ProgressStage.TRANSCRIBING)
        card.progress.apply_overlay_state(OverlayState.CANCELING)

        card.finish_transcribing(success=False)

        assert card.progress.stage is ProgressStage.CANCELED
        assert not card.is_transcribing
        assert card._settle_timer.isActive()

    def test_readiness_gates_transcribe_without_latching(self):
        card = FileInfoCard()
        card.set_ready(False)
        assert not card.transcribe_btn.isEnabled()

        card.set_transcribing(True)
        card.finish_transcribing(success=True)
        assert not card.transcribe_btn.isEnabled()

        card.set_ready(True)
        assert card.transcribe_btn.isEnabled()

    def test_single_mode_shows_the_header_and_queue_mode_swaps_it(self):
        card = FileInfoCard()
        assert card.mode == "single"
        assert card.queue.isHidden()

        card.set_mode("queue")
        assert card.header_row.isHidden()
        assert not card.queue.isHidden()

        card.set_mode("single")
        assert not card.header_row.isHidden()
        assert card.queue.isHidden()


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

    def test_batch_position_sets_the_header_label_and_a_determinate_fraction(self):
        panel = TranscriptionProgressPanel()
        panel.start(with_cleanup=True, total_files=3)
        assert panel.is_batch
        assert not panel.bar.is_indeterminate
        assert not panel.batch_label.isHidden()

        panel.set_batch_position(2, 3, "b.mp3")
        assert panel.batch_label.text() == "File 2 of 3  ·  b.mp3"
        assert panel.bar.fraction == pytest.approx(1 / 3)

    def test_stage_changes_do_not_rearm_the_sweep_during_a_batch(self):
        panel = TranscriptionProgressPanel()
        panel.start(with_cleanup=True, total_files=2)

        panel.set_stage(ProgressStage.TRANSCRIBING)
        assert not panel.bar.is_indeterminate
        assert panel.bar.fraction == pytest.approx(0.2 / 2)

        panel.set_batch_position(2, 2, "b.mp3")
        panel.set_stage(ProgressStage.CLEANING)
        assert panel.bar.fraction == pytest.approx((1 + 0.85) / 2)

    def test_single_file_bar_still_sweeps(self):
        panel = TranscriptionProgressPanel()
        panel.start(with_cleanup=False)
        assert not panel.is_batch
        assert panel.batch_label.isHidden()

        panel.set_stage(ProgressStage.TRANSCRIBING)
        assert panel.bar.is_indeterminate

    def test_stop_is_disabled_once_the_job_ends(self):
        panel = TranscriptionProgressPanel()
        panel.start(with_cleanup=False)
        assert panel.stop_btn.isEnabled()

        panel.set_stopping()
        assert not panel.stop_btn.isEnabled()
        assert panel.detail_label.text() == "Canceling…"

        panel.start(with_cleanup=False)
        panel.finish(success=True)
        assert not panel.stop_btn.isEnabled()


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
        # The drop zone, the card, and the progress panel each say their own
        # state; the shared status line stays out of this tab.
        assert tab.status_label.isHidden()
        assert tab.drop_zone.notice.isHidden()
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
        with _inline_threads():
            tab._on_file_selected("recording.wav")

        assert tab.drop_zone.isHidden()
        assert not tab.file_info_card.isHidden()
        assert tab.file_info_card.transcribe_btn.isEnabled()
        assert tab._audio_path == "recording.wav"
        assert tab.file_info_card.mode == "single"

    def test_single_file_card_appears_before_its_preview_lands(self):
        """Reading the file happens off the UI thread; the card must not wait for it."""
        tab = UploadFileTab()
        pending = []
        with patch(
            "ui_qt.widgets.upload_file_tab._run_in_thread",
            side_effect=lambda target, name: pending.append(target),
        ):
            tab._on_file_selected("recording.wav")

        assert not tab.file_info_card.isHidden()
        assert tab.file_info_card.filename_label.text() == "recording.wav"
        assert not tab.file_info_card.transcribe_btn.isEnabled()

        with patch(
            "ui_qt.widgets.upload_file_tab.audio_processor.preview_file",
            return_value=_preview(file_name="recording.wav"),
        ):
            pending[0]()

        assert tab.file_info_card.transcribe_btn.isEnabled()

    def test_unreadable_single_file_returns_to_the_drop_zone_with_the_reason(self):
        tab = UploadFileTab()
        with _inline_threads(), patch(
            "ui_qt.widgets.upload_file_tab.audio_processor.preview_file",
            side_effect=ValueError("bad header"),
        ):
            tab._on_file_selected("broken.wav")

        assert not tab.drop_zone.isHidden()
        assert tab.file_info_card.isHidden()
        assert not tab.drop_zone.notice.isHidden()
        assert tab.drop_zone.notice.text() == "Invalid audio file: bad header"
        assert tab._audio_path is None

        with _inline_threads(), patch(
            "ui_qt.widgets.upload_file_tab.audio_processor.preview_file",
            return_value=_preview(file_name="good.wav"),
        ):
            tab._on_file_selected("good.wav")
        assert tab.drop_zone.notice.isHidden()

    def test_clear_file_flow(self):
        tab = UploadFileTab()
        tab._audio_path = "some_file.wav"
        tab.drop_zone.hide()
        tab.file_info_card.show()

        tab.clear_file()
        assert tab._audio_path is None
        assert not tab.drop_zone.isHidden()
        assert tab.file_info_card.isHidden()

    def test_transcript_display_and_copy_signal(self):
        tab = UploadFileTab()
        copied = []
        tab.copy_requested.connect(copied.append)

        tab.set_transcript("Hello world transcription")
        assert tab.transcript_text.toPlainText() == "Hello world transcription"
        assert tab.file_info_card.copy_btn._active

        tab._on_copy()
        assert copied == ["Hello world transcription"]

    def test_transcript_renders_markdown_but_copies_the_source(self):
        tab = UploadFileTab()
        copied = []
        tab.copy_requested.connect(copied.append)

        tab.set_transcript("## a.mp3\n\nHello **world**")
        first = tab.transcript_text.document().begin()
        assert first.blockFormat().headingLevel() == 2
        assert tab.transcript_text.toPlainText() == "a.mp3\nHello world"

        tab._on_copy()
        assert copied == ["## a.mp3\n\nHello **world**"]

    def test_raw_view_renders_the_same_way(self):
        tab = UploadFileTab()
        tab.set_transcript("## a.mp3\n\nclean", raw="## a.mp3\n\nraw um")
        tab.raw_btn.click()
        assert tab.transcript_text.toPlainText() == "a.mp3\nraw um"
        assert tab.transcript_text.document().begin().blockFormat().headingLevel() == 2
        assert tab.shown_transcript() == "## a.mp3\n\nraw um"

    def test_expand_button_sits_in_the_pane_corner_only_while_there_is_a_transcript(self):
        tab = UploadFileTab()
        tab.resize(700, 600)
        tab.show()
        assert tab.expand_btn.isHidden()

        tab.set_transcript("Hello")
        assert not tab.expand_btn.isHidden()
        assert tab.expand_btn.parent() is tab.transcript_pane
        pane = tab.transcript_pane
        assert tab.expand_btn.geometry().right() == (
            pane.width() - 1 - pane.CORNER_INSET_X
        )
        assert tab.expand_btn.y() == pane.CORNER_INSET_Y

        tab.set_transcript("Error: Transcription canceled")
        assert tab.expand_btn.isHidden()
        tab.set_transcript("Hello again")
        tab.clear_transcription()
        assert tab.expand_btn.isHidden()

    def test_viewer_opens_on_the_current_transcript_and_follows_changes(self, tmp_path):
        tab = self._tab_with_file(tmp_path)
        tab.set_transcript("## recording.wav\n\nfirst", raw="first raw")
        tab.open_transcript_viewer()
        viewer = tab._viewer
        assert viewer.isVisible()
        assert viewer.shown_text() == "## recording.wav\n\nfirst"
        assert viewer.title_label.text() == "recording.wav"
        assert not viewer.version_toggle.isHidden()

        tab.set_transcript("second")
        assert viewer.shown_text() == "second"
        assert viewer.version_toggle.isHidden()

        tab.open_transcript_viewer()
        assert tab._viewer is viewer

        tab.clear_transcription()
        assert viewer.shown_text() == ""

    def test_viewer_receives_completed_batch_structure(self, tmp_path):
        from services.batch_upload import (
            BatchItem,
            BatchItemResult,
            BatchResult,
            BatchUploadRequest,
        )

        tab, paths = self._tab_with_files(tmp_path)
        request = BatchUploadRequest(
            items=tuple(BatchItem(path) for path in paths),
            relation=BatchRelation.SEPARATE,
        )
        result = BatchResult(
            request=request,
            items=tuple(
                BatchItemResult(item, text=f"Text {position}")
                for position, item in enumerate(request.items, start=1)
            ),
        )
        tab.set_transcript("## a.wav\n\nText 1\n\n## b.wav\n\nText 2")
        tab.set_batch_result(result)
        tab.open_transcript_viewer()

        viewer = tab._viewer
        assert [
            viewer.page_tabs.tabText(index)
            for index in range(viewer.page_tabs.count())
        ] == ["Overview", "Trans. 1", "Trans. 2"]
        viewer.page_tabs.setCurrentIndex(2)
        assert viewer.shown_text() == "## b.wav\n\nText 2"

    def test_viewer_copy_goes_through_the_tab(self):
        tab = UploadFileTab()
        copied = []
        tab.copy_requested.connect(copied.append)
        tab.set_transcript("body text")
        tab.open_transcript_viewer()
        tab._viewer.copy_btn.click()
        assert copied == ["body text"]

    def _tab_with_file(self, tmp_path):
        audio = tmp_path / "recording.wav"
        audio.write_bytes(b"\0" * 64)
        tab = UploadFileTab()
        with _inline_threads(), patch(
            "ui_qt.widgets.upload_file_tab.audio_processor.preview_file",
            return_value=_preview(file_path=str(audio), file_name="recording.wav"),
        ):
            tab._on_file_selected(str(audio))
        return tab

    @staticmethod
    def _preview_for(path):
        return _preview(file_path=path, file_name=os.path.basename(path))

    def _tab_with_files(self, tmp_path, names=("a.wav", "b.wav"), saved=None):
        paths = []
        for name in names:
            audio = tmp_path / name
            audio.write_bytes(b"\0" * 64)
            paths.append(str(audio))
        with _isolated_settings(saved):
            tab = UploadFileTab()
        with _inline_threads(), patch(
            "ui_qt.widgets.upload_file_tab.audio_processor.preview_file",
            side_effect=self._preview_for,
        ):
            tab._on_files_selected(paths, 0)
        return tab, paths

    def test_two_files_show_the_queue_with_separate_recordings_selected(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)
        card = tab.file_info_card

        assert card.mode == "queue"
        assert card.header_row.isHidden()
        assert not card.queue.isHidden()
        assert card.queue.picker.relation() == BatchRelation.SEPARATE
        assert [row.name_label.text() for row in card.queue.rows()] == ["a.wav", "b.wav"]
        assert card.queue.title_label.text() == "2 files"
        assert card.queue.note_label.isHidden()
        assert card.copy_btn.text() == "Copy all"
        assert card.transcribe_btn.isEnabled()
        assert tab._audio_path is None

    def test_two_files_emit_one_batch_request_in_order(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)
        single, batch = [], []
        tab.upload_requested.connect(lambda path, duration: single.append(path))
        tab.upload_files_requested.connect(batch.append)

        tab._on_transcribe()

        assert single == []
        request = batch[0]
        assert [item.audio_path for item in request.items] == paths
        assert all(item.duration_seconds == 60.0 for item in request.items)
        assert request.relation == BatchRelation.SEPARATE
        assert request.combine is False
        assert tab.is_transcribing
        assert tab.file_info_card.progress.is_batch
        assert all(row.state == "pending" for row in tab.file_info_card.queue.rows())

    def test_transcribe_waits_until_every_preview_has_arrived(self, tmp_path):
        pending = []
        paths = [str(tmp_path / n) for n in ("a.wav", "b.wav")]
        with _isolated_settings():
            tab = UploadFileTab()
        with patch(
            "ui_qt.widgets.upload_file_tab._run_in_thread",
            side_effect=lambda target, name: pending.append(target),
        ):
            tab._on_files_selected(paths, 0)

        assert not tab.file_info_card.transcribe_btn.isEnabled()
        assert all(row.state == "reading" for row in tab.file_info_card.queue.rows())

        with patch(
            "ui_qt.widgets.upload_file_tab.audio_processor.preview_file",
            side_effect=self._preview_for,
        ):
            pending[0]()

        assert tab.file_info_card.transcribe_btn.isEnabled()

    def test_previews_arriving_out_of_order_land_on_the_right_row(self, tmp_path):
        paths = [str(tmp_path / n) for n in ("a.wav", "b.wav")]
        with _isolated_settings():
            tab = UploadFileTab()
        with patch("ui_qt.widgets.upload_file_tab._run_in_thread"):
            tab._on_files_selected(paths, 0)

        tab._on_preview_ready(paths[1], _preview(file_name="b.wav", duration_seconds=120.0))
        tab._on_preview_ready(paths[0], _preview(file_name="a.wav", duration_seconds=60.0))

        queue = tab.file_info_card.queue
        assert queue.row_for(paths[0]).duration_chip.text() == "1m 0s"
        assert queue.row_for(paths[1]).duration_chip.text() == "2m 0s"
        assert [row.name_label.text() for row in queue.rows()] == ["a.wav", "b.wav"]

    def test_a_failed_preview_marks_its_row_and_removing_it_unblocks_transcribe(self, tmp_path):
        paths = [str(tmp_path / n) for n in ("a.wav", "b.wav")]
        with _isolated_settings():
            tab = UploadFileTab()
        with patch("ui_qt.widgets.upload_file_tab._run_in_thread"):
            tab._on_files_selected(paths, 0)

        tab._on_preview_ready(paths[0], _preview(file_name="a.wav"))
        tab._on_preview_ready(paths[1], "Invalid audio file: bad header")

        queue = tab.file_info_card.queue
        assert queue.row_for(paths[1]).state == "failed"
        assert not tab.file_info_card.transcribe_btn.isEnabled()
        assert "1 could not be read" in queue.note_label.text()
        assert queue.note_label.property("tone") == "warn"

        tab._remove(paths[1])

        assert tab.file_info_card.mode == "single"
        assert tab._audio_path == paths[0]
        assert tab.file_info_card.transcribe_btn.isEnabled()
        assert queue.note_label.isHidden()

    def test_reorder_buttons_swap_rows_and_the_request_order(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path, names=("a.wav", "b.wav", "c.wav"))
        queue = tab.file_info_card.queue
        assert not queue.rows()[0].up_btn.isEnabled()
        assert not queue.rows()[-1].down_btn.isEnabled()

        queue.row_for(paths[0]).down_btn.click()
        assert tab.queued_paths == [paths[1], paths[0], paths[2]]

        queue.row_for(paths[2]).up_btn.click()
        assert tab.queued_paths == [paths[1], paths[2], paths[0]]

        requests = []
        tab.upload_files_requested.connect(requests.append)
        tab._on_transcribe()
        assert [item.audio_path for item in requests[0].items] == [paths[1], paths[2], paths[0]]

    def test_removing_down_to_one_file_collapses_back_to_the_single_card(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)

        tab._remove(paths[1])

        card = tab.file_info_card
        assert card.mode == "single"
        assert not card.header_row.isHidden()
        assert card.filename_label.text() == "a.wav"
        assert card.copy_btn.text() == "Copy"
        assert tab._audio_path == paths[0]

    def test_removing_the_last_file_returns_to_the_drop_zone(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)

        tab._remove(paths[0])
        tab._remove(paths[1])

        assert not tab.drop_zone.isHidden()
        assert tab.file_info_card.isHidden()
        assert tab.queued_paths == []

    def test_dropping_on_the_card_appends_to_the_queue(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)
        extra = tmp_path / "c.wav"
        extra.write_bytes(b"\0" * 64)

        event = _drop_event([str(extra), str(tmp_path / "notes.txt")])
        with _inline_threads(), patch(
            "ui_qt.widgets.upload_file_tab.audio_processor.preview_file",
            side_effect=self._preview_for,
        ):
            tab.file_info_card.dropEvent(event)

        assert event.isAccepted()
        assert [os.path.basename(p) for p in tab.queued_paths] == ["a.wav", "b.wav", "c.wav"]
        note = tab.file_info_card.queue.note_label
        assert not note.isHidden()
        assert note.text() == "·  1 skipped (not audio)"
        assert note.property("tone") == ""

    def test_duplicate_paths_are_added_once(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)

        with _inline_threads():
            tab._on_files_selected([paths[0]], 0)

        assert tab.queued_paths == paths
        assert "1 already queued" in tab.file_info_card.queue.note_label.text()

    def test_a_drop_with_no_audio_leaves_the_drop_zone_up(self):
        with _isolated_settings():
            tab = UploadFileTab()

        tab._on_files_selected([], 3)

        assert not tab.drop_zone.isHidden()
        assert tab.drop_zone.notice.text() == "None of the dropped items are audio files"

    def test_adding_files_is_refused_while_a_job_runs(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)
        tab._on_transcribe()

        tab._on_files_selected([str(tmp_path / "c.wav")], 0)

        assert tab.queued_paths == paths

    def test_sequential_preset_implies_combine_and_is_remembered(self, tmp_path):
        saved = []
        tab, paths = self._tab_with_files(tmp_path, saved=saved)
        picker = tab.file_info_card.queue.picker
        requests = []
        tab.upload_files_requested.connect(requests.append)

        with _isolated_settings(saved):
            picker.combo.setCurrentIndex(picker.combo.findData(BatchRelation.SEQUENTIAL))
        tab._on_transcribe()

        assert tab.relation == BatchRelation.SEQUENTIAL
        assert requests[0].combine is True
        assert (SettingsKey.TRANSCRIPT_BATCH_RELATION, "sequential") in saved
        assert "Stitched" in picker.hint.text()

    def test_custom_relation_opens_the_dialog_and_stores_the_description(self, tmp_path):
        saved = []
        tab, paths = self._tab_with_files(tmp_path, saved=saved)
        picker = tab.file_info_card.queue.picker
        requests = []
        tab.upload_files_requested.connect(requests.append)

        with _isolated_settings(saved), patch.object(
            BatchRelationDialog, "exec", return_value=1
        ), patch.object(
            BatchRelationDialog, "instructions_text", return_value="Two halves of one call."
        ), patch.object(BatchRelationDialog, "combine_checked", return_value=True):
            picker.combo.setCurrentIndex(picker.combo.findData(BatchRelation.CUSTOM))
        tab._on_transcribe()

        assert tab.relation == BatchRelation.CUSTOM
        request = requests[0]
        assert request.custom_instructions == "Two halves of one call."
        assert request.custom_combine is True
        assert request.combine is True
        assert (SettingsKey.TRANSCRIPT_BATCH_RELATION, "custom") in saved
        assert (SettingsKey.TRANSCRIPT_BATCH_CUSTOM_INSTRUCTIONS, "Two halves of one call.") in saved
        assert (SettingsKey.TRANSCRIPT_BATCH_CUSTOM_COMBINE, True) in saved
        assert picker.hint.text().startswith("One combined transcript")
        assert not picker.edit_btn.isHidden()

    def test_rejecting_the_custom_dialog_reverts_the_combo(self, tmp_path):
        saved = []
        tab, paths = self._tab_with_files(tmp_path, saved=saved)
        picker = tab.file_info_card.queue.picker

        with _isolated_settings(saved), patch.object(
            BatchRelationDialog, "exec", return_value=0
        ):
            picker.combo.setCurrentIndex(picker.combo.findData(BatchRelation.CUSTOM))

        assert tab.relation == BatchRelation.SEPARATE
        assert picker.relation() == BatchRelation.SEPARATE
        assert saved == []
        assert picker.edit_btn.isHidden()

    def test_batch_progress_marks_rows_and_the_panel(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)
        tab._on_transcribe()
        queue = tab.file_info_card.queue
        panel = tab.file_info_card.progress

        tab.set_batch_progress(1, 2, "a.wav")
        assert queue.rows()[0].state == "active"
        assert panel.batch_label.text() == "File 1 of 2  ·  a.wav"

        tab.set_batch_item_finished(1, True)
        tab.set_batch_progress(2, 2, "b.wav")
        assert [row.state for row in queue.rows()] == ["done", "active"]
        assert panel.bar.fraction == pytest.approx(0.5)

        tab.set_batch_item_finished(2, False)
        assert queue.rows()[1].state == "failed"

    def test_finished_files_get_their_own_copy_button(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)
        copied = []
        tab.copy_requested.connect(copied.append)
        queue = tab.file_info_card.queue
        tab._on_transcribe()
        assert all(row.copy_btn.isHidden() for row in queue.rows())

        tab.set_batch_item_finished(1, True, "Hello from a")
        assert not queue.row_for(paths[0]).copy_btn.isHidden()
        assert queue.row_for(paths[1]).copy_btn.isHidden()

        queue.row_for(paths[0]).copy_btn.click()
        assert copied == ["Hello from a"]

        tab.set_batch_item_finished(2, False, "")
        assert queue.row_for(paths[1]).copy_btn.isHidden()

    def test_row_copy_survives_the_job_ending_and_clears_when_the_next_starts(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)
        queue = tab.file_info_card.queue
        tab._on_transcribe()
        tab.set_batch_item_finished(1, True, "Hello from a")
        tab.set_transcript("## a.wav\n\nHello from a\n\n## b.wav\n\nThere")
        assert not queue.row_for(paths[0]).copy_btn.isHidden()
        assert not queue.row_for(paths[0]).remove_btn.isHidden()

        tab._on_transcribe()
        assert queue.row_for(paths[0]).copy_btn.isHidden()
        # A combined job reports no per-file text, so the row stays plain.
        tab.set_batch_item_finished(1, True, "")
        assert queue.row_for(paths[0]).copy_btn.isHidden()

    def test_missing_files_at_transcribe_time_are_noted_in_the_queue_header(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path, names=("a.wav", "b.wav", "c.wav"))
        os.remove(paths[1])
        requests = []
        tab.upload_files_requested.connect(requests.append)

        tab._on_transcribe()

        assert requests == []
        assert tab.queued_paths == [paths[0], paths[2]]
        note = tab.file_info_card.queue.note_label
        assert "1 file no longer exists and was removed" in note.text()

        tab._on_transcribe()
        assert len(requests) == 1
        assert note.isHidden()

    def test_fixed_raw_switch_lives_inside_the_transcript_pane(self):
        tab = UploadFileTab()
        assert tab.version_toggle.parent() is tab.transcript_pane
        assert tab.transcript_text.parent() is tab.transcript_pane
        assert tab.version_toggle.isHidden()

        tab.set_transcript("Fixed text", raw="raw text")
        assert not tab.version_toggle.isHidden()
        assert tab.transcript_text.property("headed") is True

        tab.set_transcript("Only text")
        assert tab.version_toggle.isHidden()
        assert not tab.transcript_text.property("headed")

    def test_stop_emits_cancel_requested_and_keeps_the_tab_locked(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)
        canceled = []
        tab.cancel_requested.connect(lambda: canceled.append(True))
        tab._on_transcribe()

        tab.file_info_card.progress.stop_btn.click()

        assert canceled == [True]
        assert tab.is_transcribing
        assert not tab.file_info_card.progress.stop_btn.isEnabled()
        assert tab.file_info_card.progress.detail_label.text() == "Canceling…"

    def test_a_canceled_job_stays_canceled_when_the_error_transcript_lands(self, tmp_path):
        tab = self._tab_with_file(tmp_path)
        tab._on_transcribe()

        tab.set_progress_state(OverlayState.CANCELING)
        tab.set_transcript("Error: Transcription canceled")

        assert tab.file_info_card.progress.stage is ProgressStage.CANCELED
        assert not tab.is_transcribing
        assert tab.model_combo.isEnabled()
        assert tab.file_info_card._settle_timer.isActive()

    def test_queue_controls_hide_while_a_job_runs(self, tmp_path):
        tab, paths = self._tab_with_files(tmp_path)
        queue = tab.file_info_card.queue

        tab._on_transcribe()
        assert not queue.add_btn.isEnabled()
        assert not queue.picker.combo.isEnabled()
        assert queue.rows()[0].remove_btn.isHidden()

        tab.set_transcript("## a.wav\n\nHello\n\n## b.wav\n\nThere")
        assert queue.add_btn.isEnabled()
        assert queue.picker.combo.isEnabled()
        assert not queue.rows()[0].remove_btn.isHidden()
        assert tab.file_info_card.copy_btn._active

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

    def test_transcript_finishes_the_job_and_returns_the_actions(self, tmp_path):
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
        assert tab.status_label.isHidden()

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

    def test_transcription_stats_includes_cleanup_time_when_provided(self, tmp_path):
        tab = self._tab_with_file(tmp_path)
        tab._on_transcribe()
        tab.set_transcript("Hello there")

        tab.set_transcription_stats(24.7, 73.3, 875 * 1024, cleanup_time=4.2)

        note = tab.file_info_card.result_label
        assert not note.isHidden()
        assert "24.7s" in note.text()
        assert "3.0×" in note.text()
        assert "Cleaned in" in note.text()
        assert "4.2s" in note.text()

    def test_transcription_stats_omits_cleanup_time_when_none_or_zero(self, tmp_path):
        tab = self._tab_with_file(tmp_path)
        tab._on_transcribe()
        tab.set_transcript("Hello there")

        tab.set_transcription_stats(24.7, 73.3, 875 * 1024, cleanup_time=None)
        note = tab.file_info_card.result_label
        assert not note.isHidden()
        assert "Cleaned in" not in note.text()

        tab.set_transcription_stats(24.7, 73.3, 875 * 1024, cleanup_time=0.0)
        assert "Cleaned in" not in note.text()

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
