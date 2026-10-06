"""Reading the selection through a synthetic copy keeps the user's clipboard.

Offscreen only: every clipboard here is a stand-in, never the system one.
"""

import os
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEventLoop, QMimeData, QObject, QTimer, pyqtSignal

from ui_qt.clipboard import TemporaryClipboard


class FakeClipboard(QObject):
    """A QClipboard stand-in with a Windows-style change counter."""

    dataChanged = pyqtSignal()

    def __init__(self, text=None):
        super().__init__()
        self.sequence = 1
        self.writes = []
        self._mime = QMimeData()
        if text is not None:
            self._mime.setText(text)

    def mimeData(self):
        return self._mime

    def setMimeData(self, mime):
        self._mime = mime
        self.sequence += 1
        self.writes.append(mime.text())
        self.dataChanged.emit()

    def setText(self, text):
        mime = QMimeData()
        mime.setText(text)
        self.setMimeData(mime)

    def text(self):
        return self._mime.text()

    def copy_from_app(self, text):
        """What another app's copy does: empty the clipboard, then fill it."""
        self._mime = QMimeData()
        self.sequence += 1
        mime = QMimeData()
        mime.setText(text)
        self._mime = mime
        self.sequence += 1
        self.dataChanged.emit()


class DelayedCopy(QObject):
    """send_copy for an app that answers the copy shortcut a little later."""

    def __init__(self, clipboard, text, delay_ms=30):
        super().__init__()
        self.clipboard = clipboard
        self.text = text
        self.sent = 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._land)

    def __call__(self):
        self.sent += 1
        self._timer.start()

    def _land(self):
        self.clipboard.copy_from_app(self.text)


@pytest.fixture(autouse=True)
def qt_app(_session_qt_application):
    return _session_qt_application


def _wait_for(predicate, timeout_s=2.0):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(0.002)
    return predicate()


def _temporary(clipboard, *, sequence=True):
    source = (lambda: clipboard.sequence) if sequence else None
    temporary = TemporaryClipboard(clipboard, sequence_source=source)
    if not sequence:
        # Without a counter the constructor would read the real OS one.
        temporary._sequence = None
    return temporary


class Result:
    def __init__(self, clipboard):
        self.clipboard = clipboard
        self.calls = []

    def __call__(self, text):
        # Record what the user's clipboard held when the text arrived.
        self.calls.append((text, self.clipboard.text(), threading.current_thread().name))


@pytest.mark.parametrize("sequence", [True, False])
def test_selection_is_read_and_the_clipboard_put_back_first(sequence):
    clipboard = FakeClipboard("user text")
    temporary = _temporary(clipboard, sequence=sequence)
    copy = DelayedCopy(clipboard, "selected words")
    result = Result(clipboard)

    temporary.capture_selection(send_copy=copy, callback=result, timeout_ms=700)

    assert _wait_for(lambda: result.calls)
    assert copy.sent == 1
    assert result.calls == [("selected words", "user text", "MainThread")]
    assert clipboard.writes == ["user text"]


def test_the_restored_clipboard_is_kept_for_the_next_paste():
    clipboard = FakeClipboard("user text")
    temporary = _temporary(clipboard)
    result = Result(clipboard)

    # Kept referenced: the app's reply timer must outlive this call.
    copy = DelayedCopy(clipboard, "selected")
    temporary.capture_selection(send_copy=copy, callback=result, timeout_ms=700)
    assert _wait_for(lambda: result.calls)

    prefetched = temporary._prefetched
    assert prefetched is not None and prefetched.snapshot.text == "user text"
    assert prefetched.sequence == clipboard.sequence

    stage = temporary.stage_text("rewritten")
    assert stage.lease is not None and stage.lease.snapshot.text == "user text"


def test_no_copy_within_the_timeout_reads_nothing_and_writes_nothing():
    clipboard = FakeClipboard("user text")
    temporary = _temporary(clipboard)
    result = Result(clipboard)
    started = time.monotonic()

    temporary.capture_selection(send_copy=lambda: None, callback=result, timeout_ms=80)

    assert _wait_for(lambda: result.calls)
    assert time.monotonic() - started >= 0.07
    assert result.calls[0][:2] == ("", "user text")
    assert clipboard.writes == []


def test_a_failing_copy_shortcut_reads_nothing():
    clipboard = FakeClipboard("user text")
    temporary = _temporary(clipboard)
    result = Result(clipboard)

    def send_copy():
        raise OSError("SendInput failed")

    temporary.capture_selection(send_copy=send_copy, callback=result, timeout_ms=700)

    assert result.calls and result.calls[0][:2] == ("", "user text")
    assert clipboard.writes == []


def test_a_valid_prefetch_is_the_original_that_comes_back():
    clipboard = FakeClipboard("user text")
    temporary = _temporary(clipboard)
    temporary.request_prefetch(0)
    assert _wait_for(lambda: temporary._prefetched is not None)
    # Same counter, so the prefetch still counts as the user's clipboard.
    clipboard._mime.setText("changed without a write")
    result = Result(clipboard)

    # Kept referenced: the app's reply timer must outlive this call.
    copy = DelayedCopy(clipboard, "selected")
    temporary.capture_selection(send_copy=copy, callback=result, timeout_ms=700)

    assert _wait_for(lambda: result.calls)
    assert result.calls[0][:2] == ("selected", "user text")


def test_a_pending_paste_restore_runs_before_the_original_is_kept():
    clipboard = FakeClipboard("user text")
    temporary = _temporary(clipboard)
    stage = temporary.stage_text("dictated")
    assert stage.lease is not None and clipboard.text() == "dictated"
    result = Result(clipboard)

    # Kept referenced: the app's reply timer must outlive this call.
    copy = DelayedCopy(clipboard, "selected")
    temporary.capture_selection(send_copy=copy, callback=result, timeout_ms=700)

    assert _wait_for(lambda: result.calls)
    assert result.calls[0][:2] == ("selected", "user text")


def test_a_capture_requested_off_the_qt_thread_runs_on_it():
    clipboard = FakeClipboard("user text")
    temporary = _temporary(clipboard)
    copy = DelayedCopy(clipboard, "selected")
    result = Result(clipboard)
    sent_on = []

    def send_copy():
        sent_on.append(threading.current_thread().name)
        copy()

    worker = threading.Thread(
        target=temporary.capture_selection,
        kwargs=dict(send_copy=send_copy, callback=result, timeout_ms=700),
        name="hotkeys",
    )
    worker.start()
    worker.join()

    assert _wait_for(lambda: result.calls)
    assert sent_on == ["MainThread"]
    assert result.calls == [("selected", "user text", "MainThread")]


def test_one_capture_at_a_time_and_a_paste_ends_one_in_flight():
    clipboard = FakeClipboard("user text")
    temporary = _temporary(clipboard)
    first, second = Result(clipboard), Result(clipboard)

    temporary.capture_selection(send_copy=lambda: clipboard.copy_from_app("partial"),
                                callback=first, timeout_ms=700)
    temporary.capture_selection(send_copy=lambda: None, callback=second, timeout_ms=700)
    assert second.calls and second.calls[0][0] == ""

    # A paste arriving mid-capture puts the user's clipboard back first.
    stage = temporary.stage_text("dictated")

    assert first.calls and first.calls[0][:2] == ("", "user text")
    assert stage.lease is not None and stage.lease.snapshot.text == "user text"
