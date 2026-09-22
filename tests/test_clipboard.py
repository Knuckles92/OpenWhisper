import os
import threading
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QByteArray, QEventLoop, QMimeData, QTimer, QUrl
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from ui_qt import clipboard as clipboard_module
from ui_qt.clipboard import (
    AUTO_PASTE_MARKER_FORMAT,
    ClipboardRestoreOutcome,
    ClipboardSnapshot,
    TemporaryClipboard,
    system_clipboard_sequence,
    system_text_renderer,
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


# --- Snapshot prefetch ------------------------------------------------------


class SequencedClipboard(FakeClipboard):
    """FakeClipboard with a Windows-style change counter.

    Every write bumps the counter, whoever makes it; reads never do.
    """

    def __init__(self, mime_data=None):
        super().__init__(mime_data)
        self.counter = 1
        self.read_threads = set()

    def sequence(self):
        return self.counter

    def mimeData(self):
        self.read_threads.add(threading.current_thread())
        return super().mimeData()

    def setMimeData(self, mime_data):
        super().setMimeData(mime_data)
        self.counter += 1

    def setText(self, text):
        super().setText(text)
        self.counter += 1


def _sequenced_text_clipboard(text):
    mime_data = QMimeData()
    mime_data.setText(text)
    return SequencedClipboard(mime_data)


def _pump(ms=30):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


@pytest.fixture
def captures(monkeypatch):
    """Count ClipboardSnapshot.capture calls, i.e. real clipboard copies."""
    calls = []
    original = ClipboardSnapshot.capture.__func__

    def counting_capture(cls, mime_data):
        calls.append(threading.current_thread())
        return original(cls, mime_data)

    monkeypatch.setattr(ClipboardSnapshot, "capture", classmethod(counting_capture))
    return calls


def _prefetched(clipboard, sequence_source=None):
    temporary = TemporaryClipboard(
        clipboard, sequence_source=sequence_source or clipboard.sequence
    )
    temporary.request_prefetch(0)
    _pump()
    return temporary


def test_prefetched_snapshot_is_reused_when_clipboard_is_unchanged(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = _prefetched(clipboard)
    assert len(captures) == 1

    stage = temporary.stage_text("dictated text")

    assert len(captures) == 1  # no second copy on the paste path
    assert stage.written and stage.lease is not None
    assert clipboard.text() == "dictated text"
    assert temporary.restore_now(stage.lease) is ClipboardRestoreOutcome.RESTORED
    assert clipboard.text() == "previous text"


def test_prefetch_is_recaptured_when_the_clipboard_changed(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = _prefetched(clipboard)

    clipboard.setText("copied while dictating")
    stage = temporary.stage_text("dictated text")

    assert len(captures) == 2
    temporary.restore_now(stage.lease)
    assert clipboard.text() == "copied while dictating"


def test_prefetch_is_recaptured_when_the_sequence_is_unknown(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    readings = iter([7, None])
    temporary = _prefetched(clipboard, sequence_source=lambda: next(readings))

    clipboard.setText("copied while dictating")
    stage = temporary.stage_text("dictated text")

    assert len(captures) == 2
    temporary.restore_now(stage.lease)
    assert clipboard.text() == "copied while dictating"


def test_prefetch_restores_rich_content(captures):
    mime_data = QMimeData()
    mime_data.setText("previous text")
    mime_data.setHtml("<b>previous text</b>")
    mime_data.setData("application/x-openwhisper-test", QByteArray(b"payload"))
    image = QImage(4, 4, QImage.Format.Format_ARGB32)
    image.fill(QColor("blue"))
    mime_data.setImageData(image)
    clipboard = SequencedClipboard(mime_data)
    temporary = _prefetched(clipboard)

    stage = temporary.stage_text("dictated text")
    outcome = temporary.restore_now(stage.lease)
    restored = clipboard.mimeData()

    assert len(captures) == 1
    assert outcome is ClipboardRestoreOutcome.RESTORED
    assert restored.text() == "previous text"
    assert restored.html() == "<b>previous text</b>"
    assert bytes(restored.data("application/x-openwhisper-test")) == b"payload"
    assert restored.imageData().pixelColor(0, 0) == QColor("blue")
    assert not restored.hasFormat(AUTO_PASTE_MARKER_FORMAT)


def test_prefetch_of_a_blank_clipboard_leaves_the_transcript(captures):
    clipboard = _sequenced_text_clipboard("  ")
    temporary = _prefetched(clipboard)

    stage = temporary.stage_text("dictated text")

    assert len(captures) == 1
    assert stage.written and stage.lease is None
    assert clipboard.text() == "dictated text"


def test_discarded_prefetch_is_not_used(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = _prefetched(clipboard)

    temporary.discard_prefetch()
    _pump()
    temporary.stage_text("dictated text")

    assert len(captures) == 2


def test_discard_cancels_a_prefetch_not_yet_taken(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard, sequence_source=clipboard.sequence)

    temporary.request_prefetch(20)
    temporary.discard_prefetch()
    _pump(60)

    assert captures == []


def test_prefetch_is_consumed_by_one_stage(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = _prefetched(clipboard)
    first = temporary.stage_text("first transcript")
    temporary.restore_now(first.lease)

    temporary.stage_text("second transcript")

    assert len(captures) == 2


def test_new_request_drops_the_previous_recordings_prefetch(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = _prefetched(clipboard)

    # The next recording's capture is still waiting on its delay.
    temporary.request_prefetch(10_000)
    _pump()
    temporary.stage_text("dictated text")

    assert len(captures) == 2


def test_prefetch_waits_for_the_event_loop(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard, sequence_source=clipboard.sequence)

    temporary.request_prefetch(0)

    assert captures == []
    _pump()
    assert len(captures) == 1


def test_prefetch_from_another_thread_reads_on_the_qt_thread(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard, sequence_source=clipboard.sequence)

    worker = threading.Thread(target=temporary.request_prefetch, args=(0,))
    worker.start()
    worker.join()
    _pump()

    assert captures == [threading.main_thread()]
    assert clipboard.read_threads == {threading.main_thread()}


def test_prefetch_is_skipped_while_a_restore_is_pending(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard, sequence_source=clipboard.sequence)
    first = temporary.stage_text("first transcript")
    assert temporary.schedule_restore(first.lease, 10_000)

    temporary.request_prefetch(0)
    _pump()
    assert len(captures) == 1  # only the first stage's own capture

    second = temporary.stage_text("second transcript")
    temporary.restore_now(second.lease)

    assert clipboard.text() == "previous text"


def test_restore_after_prefetch_still_respects_a_newer_user_copy(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = _prefetched(clipboard)
    stage = temporary.stage_text("dictated text")

    clipboard.setText("new user copy")

    assert temporary.restore_now(stage.lease) is ClipboardRestoreOutcome.SKIPPED
    assert clipboard.text() == "new user copy"


def test_prefetch_is_disabled_without_a_change_counter(captures, monkeypatch):
    monkeypatch.setattr(clipboard_module, "system_clipboard_sequence", lambda: None)
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard)

    temporary.request_prefetch(0)
    _pump()

    assert captures == []
    temporary.stage_text("dictated text")
    assert len(captures) == 1


def test_prefetch_failure_falls_back_to_capturing_at_stage(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    clipboard.raise_on_read = True
    temporary = _prefetched(clipboard)
    clipboard.raise_on_read = False

    stage = temporary.stage_text("dictated text")

    assert len(captures) == 1  # the failed read never reached a capture
    assert not stage.restore_unavailable
    temporary.restore_now(stage.lease)
    assert clipboard.text() == "previous text"


def test_cleanup_drops_the_prefetch(captures):
    clipboard = _sequenced_text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard, sequence_source=clipboard.sequence)
    temporary.request_prefetch(20)
    _pump(5)

    temporary.cleanup()
    _pump(60)

    assert captures == []


def test_offscreen_platform_has_no_system_change_counter():
    # The offscreen clipboard is in-process; the Windows counter never sees
    # it change, so trusting that counter would reuse stale snapshots.
    assert QApplication.platformName() == "offscreen"
    assert system_clipboard_sequence() is None
    assert system_text_renderer() is None


# --- Staged text handed to Windows before the paste -------------------------


@pytest.fixture
def renders(monkeypatch):
    """Record each pre-render with the clipboard text at that moment."""
    calls = []
    holder = {}

    def render():
        calls.append(holder["clipboard"].text())

    def install(clipboard):
        holder["clipboard"] = clipboard
        return clipboard

    monkeypatch.setattr(clipboard_module, "system_text_renderer", lambda: render)
    return SimpleNamespace(calls=calls, install=install)


def test_staged_text_is_rendered_after_it_is_written(renders):
    clipboard = renders.install(_text_clipboard("previous text"))
    temporary = TemporaryClipboard(clipboard)

    stage = temporary.stage_text("dictated text")

    assert stage.lease is not None
    assert renders.calls == ["dictated text"]


@pytest.mark.parametrize("initial_text", [None, " "])
def test_blank_clipboard_stage_is_rendered(renders, initial_text):
    clipboard = renders.install(FakeClipboard())
    if initial_text is not None:
        clipboard.setText(initial_text)
    temporary = TemporaryClipboard(clipboard)

    temporary.stage_text("dictated text")

    assert renders.calls == ["dictated text"]


def test_unsnapshotted_stage_is_rendered(renders):
    clipboard = renders.install(_text_clipboard("previous text"))
    clipboard.raise_on_read = True
    temporary = TemporaryClipboard(clipboard)

    stage = temporary.stage_text("dictated text")

    assert stage.restore_unavailable
    assert renders.calls == ["dictated text"]


def test_failed_stage_is_not_rendered(renders):
    clipboard = renders.install(_text_clipboard("previous text"))
    clipboard.fail_set_mime_call = 1
    temporary = TemporaryClipboard(clipboard)

    stage = temporary.stage_text("dictated text")

    assert not stage.written
    assert renders.calls == []


def test_render_failure_does_not_fail_the_stage(monkeypatch):
    def broken_render():
        raise OSError("clipboard busy")

    monkeypatch.setattr(
        clipboard_module, "system_text_renderer", lambda: broken_render
    )
    clipboard = _text_clipboard("previous text")
    temporary = TemporaryClipboard(clipboard)

    stage = temporary.stage_text("dictated text")

    assert stage.written and stage.lease is not None
    assert temporary.restore_now(stage.lease) is ClipboardRestoreOutcome.RESTORED
    assert clipboard.text() == "previous text"


def test_restore_and_plain_copy_are_not_rendered(renders):
    clipboard = renders.install(_text_clipboard("previous text"))
    temporary = TemporaryClipboard(clipboard)
    stage = temporary.stage_text("dictated text")
    renders.calls.clear()

    temporary.restore_now(stage.lease)
    temporary.write_text("history copy")

    assert renders.calls == []
