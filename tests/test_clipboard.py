import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QByteArray, QEventLoop, QMimeData, QTimer, QUrl
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from ui_qt.clipboard import (
    AUTO_PASTE_MARKER_FORMAT,
    ClipboardRestoreOutcome,
    TemporaryClipboard,
)


@pytest.fixture(scope="module", autouse=True)
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


class FakeClipboard:
    def __init__(self, mime_data=None):
        self._mime_data = mime_data or QMimeData()
        self.raise_on_read = False
        self.fail_set_mime_call = None
        self.set_mime_calls = 0

    def mimeData(self):
        if self.raise_on_read:
            raise RuntimeError("clipboard read failed")
        return self._mime_data

    def setMimeData(self, mime_data):
        self.set_mime_calls += 1
        if self.set_mime_calls == self.fail_set_mime_call:
            raise RuntimeError("clipboard write failed")
        self._mime_data = mime_data

    def setText(self, text):
        mime_data = QMimeData()
        mime_data.setText(text)
        self._mime_data = mime_data

    def text(self):
        return self._mime_data.text()


def _text_clipboard(text):
    mime_data = QMimeData()
    mime_data.setText(text)
    return FakeClipboard(mime_data)


@pytest.mark.parametrize("initial_text", [None, "", " \n\t "])
def test_blank_clipboard_leaves_transcript(initial_text):
    clipboard = FakeClipboard()
    if initial_text is not None:
        clipboard.setText(initial_text)
    temporary = TemporaryClipboard(clipboard)

    stage = temporary.stage_text("dictated text")

    assert stage.written
    assert stage.lease is None
    assert not stage.restore_unavailable
    assert clipboard.text() == "dictated text"
    assert not clipboard.mimeData().hasFormat(AUTO_PASTE_MARKER_FORMAT)


def test_nonblank_text_is_restored():
    clipboard = _text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard)

    stage = temporary.stage_text("dictated text")

    assert stage.written
    assert stage.lease is not None
    assert clipboard.text() == "dictated text"
    assert clipboard.mimeData().hasFormat(AUTO_PASTE_MARKER_FORMAT)
    assert temporary.restore_now(stage.lease) is ClipboardRestoreOutcome.RESTORED
    assert clipboard.text() == "previous text"


def test_rich_and_custom_formats_are_restored():
    mime_data = QMimeData()
    mime_data.setText("previous text")
    mime_data.setHtml("<b>previous text</b>")
    mime_data.setData("application/x-openwhisper-test", QByteArray(b"payload"))
    mime_data.setUrls([QUrl.fromLocalFile("/tmp/example.wav")])
    image = QImage(2, 2, QImage.Format.Format_ARGB32)
    image.fill(QColor("red"))
    mime_data.setImageData(image)
    clipboard = FakeClipboard(mime_data)
    temporary = TemporaryClipboard(clipboard)

    stage = temporary.stage_text("dictated text")
    outcome = temporary.restore_now(stage.lease)
    restored = clipboard.mimeData()

    assert outcome is ClipboardRestoreOutcome.RESTORED
    assert restored.text() == "previous text"
    assert restored.html() == "<b>previous text</b>"
    assert bytes(restored.data("application/x-openwhisper-test")) == b"payload"
    assert restored.urls()[0].toLocalFile() == "/tmp/example.wav"
    restored_image = restored.imageData()
    assert isinstance(restored_image, QImage)
    assert restored_image.pixelColor(0, 0) == QColor("red")


def test_user_clipboard_change_is_not_overwritten():
    clipboard = _text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard)
    stage = temporary.stage_text("dictated text")
    clipboard.setText("new user copy")

    outcome = temporary.restore_now(stage.lease)

    assert outcome is ClipboardRestoreOutcome.SKIPPED
    assert clipboard.text() == "new user copy"


def test_consecutive_stages_restore_the_original_clipboard():
    clipboard = _text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard)
    first = temporary.stage_text("first transcript")

    second = temporary.stage_text("second transcript")

    assert first.lease is not None
    assert second.lease is not None
    assert clipboard.text() == "second transcript"
    temporary.restore_now(second.lease)
    assert clipboard.text() == "previous text"


def test_cleanup_restores_pending_clipboard():
    clipboard = _text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard)
    stage = temporary.stage_text("dictated text")
    assert stage.lease is not None

    temporary.cleanup()

    assert clipboard.text() == "previous text"


def test_scheduled_restore_runs_after_event_loop_turn():
    clipboard = _text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard)
    stage = temporary.stage_text("dictated text")

    assert temporary.schedule_restore(stage.lease, 0)
    assert clipboard.text() == "dictated text"
    loop = QEventLoop()
    QTimer.singleShot(20, loop.quit)
    loop.exec()

    assert clipboard.text() == "previous text"


def test_snapshot_failure_keeps_transcript_and_reports_unavailable():
    clipboard = _text_clipboard("previous text")
    clipboard.raise_on_read = True
    temporary = TemporaryClipboard(clipboard)

    stage = temporary.stage_text("dictated text")

    assert stage.written
    assert stage.lease is None
    assert stage.restore_unavailable
    assert clipboard.text() == "dictated text"


def test_clipboard_stage_write_failure_prevents_paste():
    clipboard = _text_clipboard("previous text")
    clipboard.fail_set_mime_call = 1
    temporary = TemporaryClipboard(clipboard)

    stage = temporary.stage_text("dictated text")

    assert not stage.written
    assert stage.lease is None
    assert clipboard.text() == "previous text"


def test_restore_failure_emits_warning():
    clipboard = _text_clipboard("previous text")
    clipboard.fail_set_mime_call = 2
    temporary = TemporaryClipboard(clipboard)
    failures = []
    temporary.restore_failed.connect(failures.append)
    stage = temporary.stage_text("dictated text")

    outcome = temporary.restore_now(stage.lease)

    assert outcome is ClipboardRestoreOutcome.FAILED
    assert failures == ["clipboard write failed"]
    assert clipboard.text() == "dictated text"
