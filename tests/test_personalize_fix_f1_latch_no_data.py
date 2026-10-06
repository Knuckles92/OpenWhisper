"""Push-and-hold taps that end before the first audio block arrives.

The stream reports recording as soon as it opens, but the first block can
take a while (MME latency, a Bluetooth headset switching profiles), so a
quick tap's release often finds no audio yet. That is not a cancel.
"""

import time

import pytest

from services.runtime import hotkeys as runtime_module
from services.runtime.hotkeys import HotkeyRuntime
from tests.test_personalize_s6_runtime import WINDOW, _Controller, _drain, _tap_then_press, _wait_until


class _SlowFirstBlockRecorder:
    def __init__(self):
        self.is_recording = False
        self.has_data = False
        self.capture_canceled = False

    def has_recording_data(self):
        return self.has_data


@pytest.fixture
def runtime():
    controller = _Controller()
    controller.recorder = _SlowFirstBlockRecorder()
    instance = HotkeyRuntime(controller)
    controller.runtime = instance
    yield instance
    instance.cleanup()


@pytest.fixture
def latch(monkeypatch):
    setting = {"on": True}
    monkeypatch.setattr(runtime_module, "resolve_recording_hands_free_latch", lambda: setting["on"])
    return setting


def test_a_double_tap_before_any_audio_latches_and_the_next_press_stops(runtime, latch):
    controller = runtime.controller
    pressed_at = _tap_then_press(runtime, 0.15)
    runtime._queue_record_release(pressed_at + 0.08)
    _drain(runtime)

    assert controller.hands_free_changed.emitted == [True]
    assert controller.calls == ["start"]

    runtime._queue_record_press(pressed_at + 2)
    _drain(runtime)
    assert controller.calls == ["start", "stop"]
    assert controller.hands_free_changed.emitted == [True, False]


def test_a_lone_tap_before_any_audio_still_cancels_after_the_window(runtime, latch):
    controller = runtime.controller
    start = time.monotonic()
    runtime._queue_record_press(start)
    runtime._queue_record_release(start + 0.05)

    assert _wait_until(lambda: "cancel" in controller.calls, timeout=WINDOW + 2)
    assert controller.calls == ["start", "cancel"]


def test_without_the_latch_a_tap_before_any_audio_cancels_at_once(runtime, latch):
    latch["on"] = False
    controller = runtime.controller
    start = time.monotonic()
    runtime._queue_record_press(start)
    runtime._queue_record_release(start + 0.05)
    _drain(runtime)

    assert controller.calls == ["start", "cancel"]


def test_a_long_hold_without_audio_stops_instead_of_leaving_the_mic_open(runtime, latch):
    controller = runtime.controller
    start = time.monotonic()
    runtime._queue_record_press(start)
    runtime._queue_record_release(start + 1.0)
    _drain(runtime)

    assert controller.calls == ["start", "stop"]


def test_a_hold_the_cancel_shortcut_claimed_is_left_alone(runtime, latch):
    controller = runtime.controller
    start = time.monotonic()
    runtime._queue_record_press(start)
    _drain(runtime)
    controller.recorder.capture_canceled = True
    runtime._queue_record_release(start + 1.0)
    _drain(runtime)

    assert controller.calls == ["start"]
