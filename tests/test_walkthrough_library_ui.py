"""Walkthrough review's buttons, worker gate, clipboard and accessible editors."""
from unittest.mock import Mock

import pytest
from PyQt6.QtTest import QSignalSpy, QTest

from services.walkthrough import library
from tests.test_walkthrough_library import capture as capture_fixture
from ui_qt.dialogs.walkthrough_library import WalkthroughLibraryDialog
from ui_qt.widgets.walkthrough_ready import WalkthroughReadyCard

capture = capture_fixture


@pytest.fixture
def dialog(capture):
    widget = WalkthroughLibraryDialog(root=capture.root, copy_files=Mock(return_value=True))
    yield widget
    widget._dirty = False
    widget.close()


def _wait(dialog):
    for _ in range(300):
        if not dialog._busy:
            return
        QTest.qWait(10)
    pytest.fail("Library worker did not finish")


def test_review_loads_section_editors_and_screenshot_choices(dialog):
    assert len(dialog._editors) == 3
    assert dialog._editors[0].toPlainText() == "first"
    assert [c.isChecked() for c in dialog._checks] == [True, True, False]
    assert dialog._editors[0].accessibleName() == "Section 1 transcript"
    assert dialog.save_button.isEnabled()
    assert not dialog.recover_button.isEnabled()


def test_copy_buttons_send_explicit_clipboard_formats(dialog, capture):
    dialog.copy_button.click()
    args, kwargs = dialog._copy_files.call_args
    assert args == ([],)
    assert "first" in kwargs["text"]
    dialog.pdf_button.click()
    args, kwargs = dialog._copy_files.call_args
    assert args == ([capture.result.pdf],)
    assert kwargs == {"text": ""}


def test_unsaved_edits_cannot_be_shared_or_written_up(dialog):
    dialog._editors[0].setPlainText("corrected")
    dialog.copy_button.click()
    dialog._copy_files.assert_not_called()
    assert "Save" in dialog.notice.text()
    dialog.retry_button.trigger()
    assert not dialog._busy


def test_save_regenerates_and_releases_the_app_gate(dialog, capture):
    dialog._acquire = Mock(return_value=None)
    dialog._release = Mock()
    dialog._editors[0].setPlainText("corrected")
    dialog._checks[0].setChecked(False)
    dialog.save_button.click()
    assert not dialog.copy_button.isEnabled()
    _wait(dialog)
    dialog._acquire.assert_called_once()
    dialog._release.assert_called_once()
    assert not dialog._dirty
    result = library.load_result(capture.folder)
    assert result.shots == 1
    # The omitted screenshot stays switchable, so the next save can restore it.
    assert [c.isChecked() for c in dialog._checks] == [False, True, False]
    assert dialog._checks[0].isEnabled()
    dialog._checks[0].setChecked(True)
    dialog.save_button.click()
    _wait(dialog)
    assert library.load_result(capture.folder).shots == 2
    assert "corrected" in result.clipboard_text


def test_gate_refusal_starts_no_worker(dialog):
    dialog._acquire = Mock(return_value="Finish recording first")
    dialog.save_button.click()
    assert not dialog._busy
    assert dialog.notice.text() == "Finish recording first"


def test_executor_submission_failure_releases_the_gate(dialog):
    dialog._release = Mock()
    dialog._submit = Mock(side_effect=RuntimeError("shutting down"))
    dialog.save_button.click()
    assert not dialog._busy
    dialog._release.assert_called_once()
    assert "shutting down" in dialog.notice.text()


def test_cancel_before_a_queued_retry_starts_preserves_the_source(dialog, monkeypatch):
    queued = []
    dialog._submit = queued.append
    retry = Mock()
    monkeypatch.setattr(library, "retry_write_up", retry)
    dialog.retry_button.trigger()
    assert dialog._busy
    dialog.reject()
    assert dialog._cancel.is_set()
    queued[0]()
    assert not dialog._busy
    retry.assert_not_called()
    assert "canceled" in dialog.notice.text()


def test_render_failure_preserves_unsaved_editors_and_button_states(dialog, monkeypatch):
    def fail(*args):
        raise OSError("full disk")
    monkeypatch.setattr(library, "save_review", fail)
    dialog._editors[0].setPlainText("keep this correction")
    dialog.save_button.click()
    _wait(dialog)
    assert dialog._dirty
    assert dialog._editors[0].toPlainText() == "keep this correction"
    assert not dialog.recover_button.isEnabled()
    assert "full disk" in dialog.notice.text()


def test_ready_card_exposes_review_and_both_copy_formats(capture):
    card = WalkthroughReadyCard()
    review = QSignalSpy(card.review_requested)
    copied = QSignalSpy(card.copy_requested)
    card.present(capture.result, copied=True)
    card.copy_action.trigger()
    card.copy_pdf_action.trigger()
    assert copied[0] == [capture.result, False]
    assert copied[1] == [capture.result, True]
    card.review_button.click()
    assert review[0] == [capture.folder]
    card.close()


def test_review_write_up_retry_is_off_thread_and_cancelable(dialog, monkeypatch, capture):
    seen = []
    def retry(folder, *, cancel_event):
        seen.append((folder, cancel_event))
        return capture.result
    monkeypatch.setattr(library, "retry_write_up", retry)
    dialog.retry_button.trigger()
    _wait(dialog)
    assert seen[0][0] == capture.folder
    assert seen[0][1] is dialog._cancel


def test_recovery_requests_the_selected_draft(capture):
    library.atomic_json(capture.folder / library.DRAFT_NAME,
                        {"version": 1, "started_at": "2026-10-21T14:32:05", "shots": []})
    (capture.folder / library.AUDIO_NAME).write_bytes(b"audio")
    recover = Mock()
    widget = WalkthroughLibraryDialog(root=capture.root, recover=recover)
    assert widget.recover_button.isEnabled()
    widget.recover_button.click()
    recover.assert_called_once_with(str(capture.folder))
    widget.close()
