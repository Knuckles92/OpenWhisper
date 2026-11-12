"""Walkthrough review's buttons, worker gate, clipboard and accessible editors."""
from unittest.mock import Mock

import pytest
from PyQt6.QtTest import QSignalSpy, QTest

from services.settings import WalkthroughCopyFormat
from services.walkthrough import library
from tests.test_walkthrough_library import capture as capture_fixture
from ui_qt.dialogs.walkthrough_library import WalkthroughLibraryDialog
from ui_qt.widgets.walkthrough_ready import WalkthroughReadyCard

capture = capture_fixture


@pytest.fixture
def dialog(capture):
    widget = WalkthroughLibraryDialog(root=capture.root, copy_files=Mock(return_value=True))
    yield widget
    widget.review._dirty = False
    widget.close()


def _wait(dialog):
    for _ in range(300):
        if not dialog.review._busy:
            return
        QTest.qWait(10)
    pytest.fail("Library worker did not finish")


def test_review_loads_section_editors_and_screenshot_choices(dialog):
    assert len(dialog.review._editors) == 3
    assert dialog.review._editors[0].toPlainText() == "first"
    assert [c.isChecked() for c in dialog.review._checks] == [True, True, False]
    assert dialog.review._editors[0].accessibleName() == "Section 1 transcript"
    assert dialog.review.save_button.isEnabled()
    assert not dialog.review.recover_button.isEnabled()


def test_copy_buttons_send_explicit_clipboard_formats(dialog, capture):
    dialog.review.markdown_button.click()
    args, kwargs = dialog.review._copy_files.call_args
    assert args == ([],)
    assert "first" in kwargs["text"]
    dialog.review.pdf_button.click()
    args, kwargs = dialog.review._copy_files.call_args
    assert args == ([capture.result.pdf],)
    assert kwargs == {"text": ""}
    dialog.review.copy_agent_action.trigger()
    args, kwargs = dialog.review._copy_files.call_args
    assert args == ([],)
    assert "carry out the tasks and changes" in kwargs["text"]
    assert str(capture.result.pdf.resolve()) in kwargs["text"]
    assert dialog.review.notice.text() == "Instructions copied. Paste them, then drag the PDF into your chat."


def test_unsaved_edits_cannot_be_shared_or_written_up(dialog):
    dialog.review._editors[0].setPlainText("corrected")
    dialog.review.markdown_button.click()
    dialog.review.copy_agent_action.trigger()
    dialog.review._copy_files.assert_not_called()
    assert "Save" in dialog.review.notice.text()
    dialog.review.retry_button.trigger()
    assert not dialog.review._busy


def test_save_regenerates_and_releases_the_app_gate(dialog, capture):
    dialog.review._acquire = Mock(return_value=None)
    dialog.review._release = Mock()
    dialog.review._editors[0].setPlainText("corrected")
    dialog.review._checks[0].setChecked(False)
    dialog.review.save_button.click()
    assert not dialog.review.markdown_button.isEnabled()
    _wait(dialog)
    dialog.review._acquire.assert_called_once()
    dialog.review._release.assert_called_once()
    assert not dialog.review._dirty
    result = library.load_result(capture.folder)
    assert result.shots == 1
    # The omitted screenshot stays switchable, so the next save can restore it.
    assert [c.isChecked() for c in dialog.review._checks] == [False, True, False]
    assert dialog.review._checks[0].isEnabled()
    dialog.review._checks[0].setChecked(True)
    dialog.review.save_button.click()
    _wait(dialog)
    assert library.load_result(capture.folder).shots == 2
    assert "corrected" in result.clipboard_text


def test_gate_refusal_starts_no_worker(dialog):
    dialog.review._acquire = Mock(return_value="Finish recording first")
    dialog.review.save_button.click()
    assert not dialog.review._busy
    assert dialog.review.notice.text() == "Finish recording first"


def test_executor_submission_failure_releases_the_gate(dialog):
    dialog.review._release = Mock()
    dialog.review._submit = Mock(side_effect=RuntimeError("shutting down"))
    dialog.review.save_button.click()
    assert not dialog.review._busy
    dialog.review._release.assert_called_once()
    assert "shutting down" in dialog.review.notice.text()


def test_cancel_before_a_queued_retry_starts_preserves_the_source(dialog, monkeypatch):
    queued = []
    dialog.review._submit = queued.append
    retry = Mock()
    monkeypatch.setattr(library, "retry_write_up", retry)
    dialog.review.retry_button.trigger()
    assert dialog.review._busy
    dialog.reject()
    assert dialog.review._cancel.is_set()
    queued[0]()
    assert not dialog.review._busy
    retry.assert_not_called()
    assert "canceled" in dialog.review.notice.text()


def test_render_failure_preserves_unsaved_editors_and_button_states(dialog, monkeypatch):
    def fail(*args):
        raise OSError("full disk")
    monkeypatch.setattr(library, "save_review", fail)
    dialog.review._editors[0].setPlainText("keep this correction")
    dialog.review.save_button.click()
    _wait(dialog)
    assert dialog.review._dirty
    assert dialog.review._editors[0].toPlainText() == "keep this correction"
    assert not dialog.review.recover_button.isEnabled()
    assert "full disk" in dialog.review.notice.text()


def test_ready_card_exposes_review_and_every_copy_format(capture):
    card = WalkthroughReadyCard()
    review = QSignalSpy(card.review_requested)
    copied = QSignalSpy(card.copy_requested)
    card.present(capture.result, copied=True)
    assert card.copy_word_action.isEnabled()
    card.copy_pdf_action.trigger()
    card.copy_word_action.trigger()
    card.copy_markdown_action.trigger()
    card.copy_agent_action.trigger()
    assert [list(args) for args in copied] == [
        [capture.result, WalkthroughCopyFormat.PDF],
        [capture.result, WalkthroughCopyFormat.WORD],
        [capture.result, WalkthroughCopyFormat.AGENT],
        [capture.result, WalkthroughCopyFormat.AGENT_TASK],
    ]
    card.review_button.click()
    assert review[0] == [capture.folder]
    card.close()


def test_review_write_up_retry_is_off_thread_and_cancelable(dialog, monkeypatch, capture):
    seen = []
    def retry(folder, *, instructions, cancel_event):
        seen.append((folder, instructions, cancel_event))
        return capture.result
    monkeypatch.setattr(library, "retry_write_up", retry)
    dialog.review.retry_button.trigger()
    _wait(dialog)
    # The plain redo uses Settings' prompt.
    assert seen == [(capture.folder, None, dialog.review._cancel)]


def test_recovery_requests_the_selected_draft(capture):
    library.atomic_json(capture.folder / library.DRAFT_NAME,
                        {"version": 1, "started_at": "2026-10-21T14:32:05", "shots": []})
    (capture.folder / library.AUDIO_NAME).write_bytes(b"audio")
    recover = Mock()
    widget = WalkthroughLibraryDialog(root=capture.root, recover=recover)
    assert widget.review.recover_button.isEnabled()
    widget.review.recover_button.click()
    recover.assert_called_once_with(str(capture.folder))
    widget.close()


def _yes(monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    asked = Mock(return_value=QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "question", asked)
    return asked


def test_a_failed_redo_after_saving_shows_what_was_saved(dialog, monkeypatch, capture):
    monkeypatch.setattr(library, "written_up", lambda folder: False)
    monkeypatch.setattr(library, "retry_write_up", Mock(side_effect=RuntimeError("agent missing")))
    review = dialog.review
    review._editors[0].setPlainText("corrected")
    review._save(redo=True)
    _wait(dialog)
    # The words are on disk, so the board is reloaded and nothing is unsaved:
    # a second save would otherwise repeat the same edit and reordering.
    assert not review._dirty
    assert review._editors[0].toPlainText() == "corrected"
    assert review.notice.text() == "Saved, but the write-up wasn't redone: agent missing"


def test_closing_with_discarded_edits_really_drops_them(dialog, monkeypatch):
    _yes(monkeypatch)
    dialog.show()
    dialog.review._editors[0].setPlainText("EDITED")
    dialog.reject()
    assert not dialog.isVisible()
    assert not dialog.review._dirty
    assert dialog.review._editors[0].toPlainText() == "first"


def test_refresh_asks_about_unsaved_edits_only_once(dialog, monkeypatch):
    asked = _yes(monkeypatch)
    dialog.review._editors[0].setPlainText("EDITED")
    dialog._refresh_requested()
    asked.assert_called_once()
    assert dialog.list.currentRow() == 0
    assert dialog.review._editors[0].toPlainText() == "first"


def test_keeping_forever_asks_before_dropping_unsaved_edits(dialog, monkeypatch, capture):
    from PyQt6.QtWidgets import QMessageBox

    library.set_retention(capture.root, 30)
    dialog._show_retention()
    monkeypatch.setattr(QMessageBox, "question", Mock(return_value=QMessageBox.StandardButton.No))
    dialog.review._editors[0].setPlainText("EDITED")
    dialog.keep.setCurrentIndex(dialog.keep.findData(0))
    assert not dialog.review._busy
    assert dialog.review._editors[0].toPlainText() == "EDITED"
    assert library.retention_days(capture.root) == 30
    assert dialog.keep.currentData() == 30


def test_delete_does_not_say_saved(dialog, monkeypatch):
    _yes(monkeypatch)
    dialog.review._delete()
    _wait(dialog)
    assert "Saved" not in dialog.review.notice.text()


def test_a_draft_shows_that_it_is_recovering(capture):
    (capture.folder / library.DRAFT_NAME).write_text("{}", encoding="utf-8")
    (capture.folder / library.AUDIO_NAME).write_bytes(b"RIFF")
    (capture.folder / "walkthrough.json").unlink()
    recover = Mock()
    widget = WalkthroughLibraryDialog(root=capture.root, recover=recover)
    try:
        widget.show()
        review = widget.review
        assert review.recover_button.isEnabled()
        review.recover_button.click()
        recover.assert_called_once()
        assert review.notice.isVisible() and "Recovering" in review.notice.text()
        assert not review.recover_button.isEnabled()
    finally:
        widget.close()
