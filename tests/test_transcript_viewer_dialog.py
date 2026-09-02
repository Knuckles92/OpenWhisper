import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from ui_qt.dialogs.transcript_viewer_dialog import TranscriptViewerDialog, _ReaderView
from ui_qt.utils.markdown_render import READER_STYLE


@pytest.fixture(scope="module", autouse=True)
def qapp():
    yield QApplication.instance() or QApplication([])


def _first_block_heading_level(dialog):
    return dialog.view.document().begin().blockFormat().headingLevel()


class TestTranscriptViewerDialog:
    def test_renders_markdown_and_names_the_source(self):
        dialog = TranscriptViewerDialog()
        dialog.set_transcript("## a.mp3\n\nHello **there**", title="a.mp3")
        assert _first_block_heading_level(dialog) == 2
        assert dialog.view.toPlainText() == "a.mp3\nHello there"
        assert dialog.title_label.text() == "a.mp3"
        assert "a.mp3" in dialog.windowTitle()
        assert dialog.version_toggle.isHidden()

    def test_raw_switch_appears_only_when_raw_differs(self):
        dialog = TranscriptViewerDialog()
        dialog.set_transcript("clean", raw="clean")
        assert dialog.version_toggle.isHidden()

        dialog.set_transcript("clean", raw="raw um")
        assert not dialog.version_toggle.isHidden()
        dialog.raw_btn.click()
        assert dialog.shown_text() == "raw um"
        assert dialog.view.toPlainText() == "raw um"

        # A new transcript lands on Fixed again, whatever was showing before.
        dialog.set_transcript("next", raw="next raw")
        assert dialog.fixed_btn.isChecked()
        assert dialog.shown_text() == "next"

    def test_copy_emits_the_shown_source_text_not_the_rendering(self):
        dialog = TranscriptViewerDialog()
        copied = []
        dialog.copy_requested.connect(copied.append)
        dialog.set_transcript("## head\n\n**bold** body")
        dialog.copy_btn.click()
        assert copied == ["## head\n\n**bold** body"]

    def test_copy_is_disabled_while_empty(self):
        dialog = TranscriptViewerDialog()
        dialog.clear()
        assert not dialog.copy_btn.isEnabled()
        copied = []
        dialog.copy_requested.connect(copied.append)
        dialog._copy_shown_text()
        assert copied == []

    def test_zoom_steps_scale_the_document_and_stop_at_the_ends(self):
        dialog = TranscriptViewerDialog()
        dialog.set_transcript("body")
        base = dialog.view.document().defaultFont().pointSizeF()
        assert base == READER_STYLE.body_pt

        dialog.zoom(1)
        assert dialog.view.document().defaultFont().pointSizeF() > base
        assert dialog.zoom_out_btn.isEnabled()

        for _ in range(len(dialog.ZOOM_STEPS)):
            dialog.zoom(1)
        assert dialog.zoom_factor == dialog.ZOOM_STEPS[-1]
        assert not dialog.zoom_in_btn.isEnabled()

        dialog.reset_zoom()
        assert dialog.zoom_factor == 1.0
        assert dialog.view.document().defaultFont().pointSizeF() == base

    def test_zoom_survives_the_next_transcript(self):
        dialog = TranscriptViewerDialog()
        dialog.set_transcript("one")
        dialog.zoom(1)
        zoomed = dialog.view.document().defaultFont().pointSizeF()
        dialog.set_transcript("two")
        assert dialog.view.document().defaultFont().pointSizeF() == zoomed

    def test_is_a_resizable_non_modal_window(self):
        dialog = TranscriptViewerDialog()
        assert not dialog.isModal()
        assert dialog.minimumWidth() < dialog.width()


class TestReaderView:
    # Qt delivers a hidden widget's resize on show, so the views are shown.
    def test_wide_views_center_a_capped_column(self):
        view = _ReaderView()
        view.resize(1600, 600)
        view.show()
        margins = view.viewportMargins()
        expected = (1600 - _ReaderView.MAX_MEASURE) // 2
        assert margins.left() == margins.right() == expected
        assert view.viewport().width() <= _ReaderView.MAX_MEASURE

    def test_narrow_views_keep_the_minimum_gutter(self):
        view = _ReaderView()
        view.resize(500, 600)
        view.show()
        assert view.viewportMargins().left() == _ReaderView.MIN_SIDE_MARGIN
