import os
import pytest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QScrollArea
from PyQt6.QtCore import Qt, QMimeData, QUrl, QPointF
from PyQt6.QtGui import QDragEnterEvent, QDropEvent

from services.audio_processor import AudioFilePreview
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


class TestFileInfoCard:
    def test_set_preview(self):
        card = FileInfoCard()
        preview = AudioFilePreview(
            file_path="sample.wav",
            file_name="sample.wav",
            file_size_mb=1.0,
            duration_seconds=60.0,
            sample_rate=44100,
            channels=2,
            needs_splitting=False,
            estimated_chunks=1,
        )
        card.set_preview(preview)

        assert card.filename_label.text() == "sample.wav"
        assert "1.0 MB" in card.details_label.text()
        assert "1m 0s" in card.details_label.text()
        assert "44100 Hz, Stereo" in card.audio_info_label.text()
        assert not card.chunk_label.isHidden()

    def test_set_transcribing_toggles_button_states(self):
        card = FileInfoCard()
        card.set_transcribing(True)
        assert not card.transcribe_btn.isEnabled()
        assert not card.remove_btn.isEnabled()
        assert card.transcribe_btn.text() == "Transcribing..."

        card.set_transcribing(False)
        assert card.transcribe_btn.isEnabled()
        assert card.remove_btn.isEnabled()
        assert card.transcribe_btn.text() == "Transcribe"

    def test_set_copy_enabled(self):
        card = FileInfoCard()
        card.set_copy_enabled(True)
        assert card.copy_btn._active

        card.set_copy_enabled(False)
        assert not card.copy_btn._active


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

    def test_scroll_area_handles_constrained_height(self):
        """Scroll area preserves layout and avoids overlapping under short window."""
        tab = UploadFileTab()
        tab.resize(500, 350)
        tab.show()
        QApplication.processEvents()

        assert tab.scroll_area.isVisible()
        assert tab.content_layout.count() > 0
        assert tab.drop_zone.minimumHeight() == 150

    def test_expand_and_collapse_sections_with_scroll_area(self):
        """Toggling sections maintains clean layout without errors."""
        tab = UploadFileTab()
        tab.resize(605, 580)
        tab.show()
        QApplication.processEvents()

        # Expand engine settings
        tab.set_engine_settings_collapsed(False)
        QApplication.processEvents()
        assert not tab.local_engine.is_collapsed

        # Expand transcription
        tab.set_transcription_collapsed(False)
        QApplication.processEvents()
        assert not tab.is_transcription_collapsed()

        # Collapse again
        tab.set_engine_settings_collapsed(True)
        tab.set_transcription_collapsed(True)
        QApplication.processEvents()
        assert tab.local_engine.is_collapsed
        assert tab.is_transcription_collapsed()


class TestMainWindowUploadTabIntegration:
    @patch.object(TranscriptionTabBase, "load_cleanup_setting")
    def test_main_window_upload_tab_at_minimum_height(self, _mock_setting):
        from ui_qt.main_window import MainWindow
        from ui_qt.widgets.tabbed_content import TabbedContentWidget
        from config import config

        window = MainWindow()
        window.resize(config.MAIN_WINDOW_DEFAULT_WIDTH, config.MAIN_WINDOW_MIN_HEIGHT)
        window.tabbed_content.set_current_index(TabbedContentWidget.TAB_UPLOAD_FILE)
        window.show()
        QApplication.processEvents()

        upload_tab = window.upload_file_tab
        assert upload_tab.isVisibleTo(window)
        assert upload_tab.scroll_area.isVisibleTo(upload_tab)
        assert not upload_tab.drop_zone.isHidden()

        window._force_quit = True
        window.close()
        QApplication.processEvents()

