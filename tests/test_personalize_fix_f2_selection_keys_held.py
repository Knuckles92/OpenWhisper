"""No copy is sent while the shortcut's modifiers are still held.

A Ctrl+C sent with Alt still down is AltGr+C on Polish or Czech layouts and
types a character over the selection; with Shift it opens DevTools in a
browser. So when the keys stay down past the wait, the read gives up and
says so instead of copying.
"""

import sys
import threading
import types
from concurrent.futures import Future

import pytest

from services import focus_context, synthetic_keys
from services.dictation_pipeline import DictationJob, JobMode
from services.focus_context import FocusSnapshot
from services.runtime.command import CommandRefused, UnreadSelection
from tests.test_personalize_s3_command_runtime import (
    NOTEPAD,
    FakeService,
    _settle,
    bare_commands,
)

KEYS_HELD = "Release the shortcut keys, then try again"


@pytest.fixture
def h(monkeypatch, _session_qt_application):
    h = bare_commands(monkeypatch, keys_released=False)
    focus_context.set_service(FakeService(FocusSnapshot(NOTEPAD, None)))
    h.ui.clipboard_selection = "would be read"
    yield h
    h.commands.cleanup()


def test_command_mode_refuses_instead_of_copying_with_keys_held(h):
    h.commands.key_pressed(1.0)
    job = h.runtime._job
    h.commands.key_released(2.0)
    _settle(job.selection.done)

    assert h.ui.captures == []
    assert job.selection_text() == ""
    with pytest.raises(CommandRefused, match=f"^{KEYS_HELD}$"):
        h.commands.complete_recording("translate this into Spanish", job)
    assert h.runtime._transcript_cleanup.calls == []


def test_a_transform_abandons_instead_of_copying_with_keys_held(h):
    h.commands.run_transform("polish")

    _settle(lambda: h.runtime.events[-1][0] == "abandon")
    assert h.runtime.events[-1] == ("abandon", KEYS_HELD)
    assert h.ui.captures == []


def test_a_command_with_a_known_selection_needs_no_keys_released(h):
    future: Future = Future()
    future.set_result("old words")
    job = DictationJob(mode=JobMode.COMMAND, selection=future)

    text, raw, _info = h.commands.complete_recording("make it formal", job)

    assert raw == "old words" and h.ui.captures == []


def _fake_keyboard(monkeypatch, pressed):
    keyboard = types.SimpleNamespace(is_pressed=lambda name: name in pressed)
    monkeypatch.setitem(sys.modules, "keyboard", keyboard)


def test_a_modifier_the_hook_thinks_is_stuck_but_is_up_counts_as_released(monkeypatch):
    # The hook can miss a key-up that went to an elevated window; refusing on
    # that alone would block Command Mode until the key is pressed again.
    _fake_keyboard(monkeypatch, {"alt"})
    monkeypatch.setattr(synthetic_keys, "_physical_key_reader", lambda: lambda vk: False)

    assert synthetic_keys._windows_modifiers_held() is False


def test_a_modifier_the_os_also_holds_counts_as_held(monkeypatch):
    _fake_keyboard(monkeypatch, {"windows"})
    down = []
    monkeypatch.setattr(synthetic_keys, "_physical_key_reader",
                        lambda: lambda vk: down.append(vk) or vk == 0x5C)

    assert synthetic_keys._windows_modifiers_held() is True
    assert 0x5C in down


def test_without_the_os_key_state_the_hook_decides(monkeypatch):
    _fake_keyboard(monkeypatch, {"ctrl"})
    monkeypatch.setattr(synthetic_keys, "_physical_key_reader", lambda: None)

    assert synthetic_keys._windows_modifiers_held() is True


def test_the_reader_thread_never_copies_with_keys_held(h):
    results = []
    done = threading.Event()

    def collect(text):
        results.append(text)
        done.set()

    h.commands._read_selection(h.commands._request_focus(), collect)

    assert done.wait(1.0)
    assert results == [UnreadSelection(KEYS_HELD)] and h.ui.captures == []
