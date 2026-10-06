"""A lost microphone whose driver hangs on close never wedges the recorder.

The lost stream's abort() blocks like a driver that hangs after the device
is removed. Whether or not another microphone takes over, the watcher must
finish, so the next dictation can start.
"""
import threading

import pytest

from services import audio_devices
from services import recorder as recorder_module
from tests.test_personalize_s7_recorder import (  # noqa: F401  (fixtures)
    _tone,
    _wait,
    _watch,
    fake_sd,
    make_recorder,
)


@pytest.fixture
def hang(monkeypatch):
    """Make a stream's abort() block until teardown.

    Requested after ``make_recorder`` so it tears down first: the recorder's
    teardown expects every stream closed.
    """
    monkeypatch.setattr(recorder_module, "LOST_STREAM_CLOSE_TIMEOUT_S", 0.2)
    monkeypatch.setattr("services.diagnostics.record_metrics", lambda **values: None)
    release = threading.Event()
    hung = []

    def install(stream):
        entered = threading.Event()

        def abort():
            entered.set()
            release.wait(10)
            stream.aborted = True
            stream.active = False

        stream.abort = abort
        hung.append(stream)
        return entered

    yield install
    release.set()
    assert _wait(lambda: not any(id(stream) in audio_devices._open_streams for stream in hung))


def _assert_ready_for_the_next_dictation(recorder, sd, lost):
    assert recorder.wait_for_stop_completion(3)
    assert not recorder.is_recording
    assert recorder.stream is None
    sd.InputStream.refuse = set()
    sd.InputStream.before_open = None
    assert recorder.start_recording()
    assert recorder.stream is not lost


def test_with_no_other_microphone_a_hung_close_still_ends_the_recording(fake_sd, make_recorder, hang):  # noqa: F811
    recorder = make_recorder()
    _switches, errors, _switched, failed = _watch(recorder)
    assert recorder.start_recording()
    usb = fake_sd.InputStream.opened[0]
    closing = hang(usb)
    usb.feed(_tone(7))
    fake_sd.InputStream.refuse = {0, 1}

    usb.active = False

    assert failed.wait(3)
    assert errors == [recorder_module.NO_MICROPHONE_LEFT]
    assert closing.wait(3)
    _assert_ready_for_the_next_dictation(recorder, fake_sd, usb)


@pytest.mark.parametrize("end", ["cancel_recording", "stop_recording"])
def test_ending_during_the_switch_with_a_hung_close_still_ends_the_recording(fake_sd, make_recorder, hang, end):  # noqa: F811
    recorder = make_recorder()
    switches, errors, _switched, _failed = _watch(recorder)
    assert recorder.start_recording()
    usb = fake_sd.InputStream.opened[0]
    closing = hang(usb)
    usb.feed(_tone(5))
    ended = threading.Event()

    def end_on_reopen(device):
        if device == 1 and not ended.is_set():
            ended.set()
            getattr(recorder, end)()

    fake_sd.InputStream.before_open = end_on_reopen
    usb.active = False

    assert ended.wait(3)
    assert closing.wait(3)
    assert switches == [] and errors == []
    assert fake_sd.InputStream.opened[-1].closed
    _assert_ready_for_the_next_dictation(recorder, fake_sd, usb)
