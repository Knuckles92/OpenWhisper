"""Files on the clipboard: a walkthrough's PDF with its Markdown as text.

Offscreen only: every clipboard here is a stand-in, never the system one.
"""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QMimeData
from PyQt6.QtGui import QColor, QImage

from ui_qt.clipboard import ClipboardRestoreOutcome, TemporaryClipboard


@pytest.fixture(autouse=True)
def qt_app(_session_qt_application):
    return _session_qt_application


class FakeClipboard:
    """A QClipboard stand-in; ``fail_writes`` makes every write raise."""

    def __init__(self, text="user text"):
        self._mime = QMimeData()
        self._mime.setText(text)
        self.fail_writes = False

    def mimeData(self):
        return self._mime

    def setMimeData(self, mime):
        if self.fail_writes:
            raise RuntimeError("clipboard write failed")
        self._mime = mime

    def setText(self, text):
        mime = QMimeData()
        mime.setText(text)
        self.setMimeData(mime)

    def text(self):
        return self._mime.text()


def _files(mime: QMimeData) -> list[Path]:
    assert all(url.isLocalFile() for url in mime.urls())
    return [Path(url.toLocalFile()) for url in mime.urls()]


def test_files_and_their_text_go_on_together(tmp_path):
    pdf, markdown = tmp_path / "walkthrough.pdf", tmp_path / "walkthrough.md"
    clipboard = FakeClipboard()
    temporary = TemporaryClipboard(clipboard)

    assert temporary.write_files([pdf, markdown], "# Walkthrough\n")

    mime = clipboard.mimeData()
    assert _files(mime) == [pdf, markdown]
    assert mime.text() == "# Walkthrough\n"
    assert not mime.hasImage()


def test_without_text_only_the_files_go_on(tmp_path):
    clipboard = FakeClipboard()
    temporary = TemporaryClipboard(clipboard)

    assert temporary.write_files([tmp_path / "walkthrough.pdf"])

    mime = clipboard.mimeData()
    assert _files(mime) == [tmp_path / "walkthrough.pdf"]
    assert "text/plain" not in mime.formats()


def test_an_image_goes_on_beside_the_file_and_its_path(tmp_path):
    shot = tmp_path / "2026-10-21_14-32-05.png"
    image = QImage(4, 3, QImage.Format.Format_RGB32)
    image.fill(QColor("#ff4f3a"))
    clipboard = FakeClipboard()
    temporary = TemporaryClipboard(clipboard)

    assert temporary.write_files([shot], str(shot), image=image)

    mime = clipboard.mimeData()
    pasted = mime.imageData()
    assert _files(mime) == [shot]
    assert mime.text() == str(shot)
    assert isinstance(pasted, QImage) and pasted.size() == image.size()
    assert pasted.pixelColor(0, 0) == QColor("#ff4f3a")


def test_a_pending_paste_restore_is_dropped(tmp_path):
    clipboard = FakeClipboard("user text")
    temporary = TemporaryClipboard(clipboard)
    stage = temporary.stage_text("dictated text")
    assert temporary.schedule_restore(stage.lease, 60_000)

    assert temporary.write_files([tmp_path / "walkthrough.pdf"], "notes")

    # The same state write_text clears: no lease left, no restore timed.
    assert temporary._pending is None
    assert not temporary._restore_timer.isActive()
    assert clipboard.text() == "notes"


def test_a_selection_capture_in_flight_ends_first(tmp_path):
    clipboard = FakeClipboard("user text")
    temporary = TemporaryClipboard(clipboard)
    results = []
    temporary.capture_selection(
        send_copy=lambda: None, callback=results.append, timeout_ms=60_000
    )

    assert temporary.write_files([tmp_path / "walkthrough.pdf"], "notes")

    assert results == [""]
    assert clipboard.text() == "notes"


def test_no_clipboard_writes_nothing_like_write_text(tmp_path):
    temporary = TemporaryClipboard(None)

    assert not temporary.write_text("notes")
    assert not temporary.write_files([tmp_path / "walkthrough.pdf"], "notes")


def test_a_failed_write_keeps_the_pending_restore(tmp_path):
    clipboard = FakeClipboard("user text")
    temporary = TemporaryClipboard(clipboard)
    stage = temporary.stage_text("dictated text")
    clipboard.fail_writes = True

    assert not temporary.write_text("notes")
    assert not temporary.write_files([tmp_path / "walkthrough.pdf"], "notes")

    clipboard.fail_writes = False
    assert temporary.restore_now(stage.lease) is ClipboardRestoreOutcome.RESTORED
    assert clipboard.text() == "user text"
