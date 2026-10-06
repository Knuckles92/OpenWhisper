"""Reading the selection where the clipboard has no Windows-style counter.

Qt's Cocoa backend never signals another app's copy while OpenWhisper is in
the background, so a capture that waited for dataChanged read nothing and
left the user's clipboard holding the copied selection. macOS now reads
NSPasteboard's change count; without any counter the clipboard is watched
for new text, and the user's original always goes back.

Offscreen only: every clipboard here is a stand-in, never the system one.
"""

import contextlib
import os
import sys
import time
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEventLoop, QMimeData, QObject, QTimer, pyqtSignal

from ui_qt import clipboard as clipboard_module
from ui_qt.clipboard import TemporaryClipboard


class CocoaLikeClipboard(QObject):
    """QCocoaClipboard: own writes signal, another app's copy does not."""

    dataChanged = pyqtSignal()

    def __init__(self, text):
        super().__init__()
        self.change_count = 7
        self.writes = []
        self._mime = QMimeData()
        self._mime.setText(text)

    def mimeData(self):
        return self._mime

    def setMimeData(self, mime):
        self._mime = mime
        self.change_count += 1
        self.writes.append(mime.text())
        self.dataChanged.emit()

    def text(self):
        return self._mime.text()

    def copy_from_other_app(self, text):
        mime = QMimeData()
        mime.setText(text)
        self._mime = mime
        self.change_count += 1


class AppAnswersCopy(QObject):
    """send_copy for an app that puts the selection on the clipboard 30 ms later."""

    def __init__(self, clipboard, text):
        super().__init__()
        self.clipboard, self.text, self.sent = clipboard, text, 0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._land)

    def __call__(self):
        self.sent += 1
        if self.text is not None:
            self._timer.start()

    def _land(self):
        self.clipboard.copy_from_other_app(self.text)


@pytest.fixture(autouse=True)
def qt_app(_session_qt_application):
    return _session_qt_application


def _wait_for(predicate, timeout_s=3.0):
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        time.sleep(0.002)
    return predicate()


def _without_counter(clipboard):
    temporary = TemporaryClipboard(clipboard, sequence_source=lambda: None)
    # Without a counter the constructor would read the real OS one.
    temporary._sequence = None
    return temporary


def _capture(temporary, copy, timeout_ms=700):
    results = []
    temporary.capture_selection(send_copy=copy, callback=results.append, timeout_ms=timeout_ms)
    assert _wait_for(lambda: results)
    return results[0]


def test_with_the_change_count_another_apps_copy_is_read_and_undone():
    clipboard = CocoaLikeClipboard("https://example.com/copied-url")
    temporary = TemporaryClipboard(clipboard, sequence_source=lambda: clipboard.change_count)
    copy = AppAnswersCopy(clipboard, "selected paragraph")

    assert _capture(temporary, copy) == "selected paragraph"
    assert clipboard.writes == ["https://example.com/copied-url"]
    assert clipboard.text() == "https://example.com/copied-url"


def test_without_a_counter_or_a_signal_new_text_is_still_seen_and_undone():
    clipboard = CocoaLikeClipboard("https://example.com/copied-url")
    temporary = _without_counter(clipboard)
    copy = AppAnswersCopy(clipboard, "selected paragraph")
    started = time.monotonic()

    assert _capture(temporary, copy) == "selected paragraph"
    assert time.monotonic() - started < 0.6
    assert clipboard.text() == "https://example.com/copied-url"


def test_without_a_counter_the_original_goes_back_even_when_nothing_was_seen():
    # Nothing proves the copy never landed where Qt can't see it (Wayland).
    clipboard = CocoaLikeClipboard("user text")
    temporary = _without_counter(clipboard)

    assert _capture(temporary, AppAnswersCopy(clipboard, None), timeout_ms=80) == ""
    assert clipboard.writes == ["user text"]


def test_without_a_counter_a_copy_never_sent_writes_nothing():
    clipboard = CocoaLikeClipboard("user text")
    temporary = _without_counter(clipboard)

    def send_copy():
        raise RuntimeError("This Wayland desktop can't copy the selection for OpenWhisper")

    assert _capture(temporary, send_copy) == ""
    assert clipboard.writes == []


def test_macos_reads_the_pasteboard_change_count(monkeypatch):
    counts = iter([41, 42])
    pasteboard = types.SimpleNamespace(changeCount=lambda: next(counts))
    appkit = types.SimpleNamespace(
        NSPasteboard=types.SimpleNamespace(generalPasteboard=lambda: pasteboard))
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "objc", types.SimpleNamespace(
        autorelease_pool=contextlib.nullcontext))
    monkeypatch.setattr(clipboard_module.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard_module, "QGuiApplication",
                        types.SimpleNamespace(platformName=lambda: "cocoa"))

    sequence = clipboard_module.system_clipboard_sequence()

    assert sequence is not None
    assert (sequence(), sequence()) == (41, 42)


def test_macos_without_appkit_yet_has_no_count_and_starts_loading_it(monkeypatch):
    from services.focus_context import _mac

    started = []
    monkeypatch.delitem(sys.modules, "AppKit", raising=False)
    monkeypatch.setattr(_mac, "_appkit", lambda: started.append(True))
    monkeypatch.setattr(clipboard_module.sys, "platform", "darwin")
    monkeypatch.setattr(clipboard_module, "QGuiApplication",
                        types.SimpleNamespace(platformName=lambda: "cocoa"))

    sequence = clipboard_module.system_clipboard_sequence()

    assert sequence is not None and sequence() is None
    assert started == [True]
