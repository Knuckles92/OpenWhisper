"""Dictation starts on the first ranked microphone that opens and moves on if it is lost.

A fake sounddevice stands in for PortAudio: its streams are keyed by the
device they were opened on and deliver blocks only when a test feeds them,
so no microphone is opened. The recorder's own watcher thread still detects
the loss and switches, exactly as in the app.
"""
import threading
import time
import wave

import numpy as np
import pytest

from config import config
from services import audio_devices
from services import recorder as recorder_module
from services.recorder import AudioRecorder
from services.settings import SettingsKey, settings_manager
from tests.test_personalize_s7_audio_devices import MME, WASAPI, FakeSd, windows_sd

USB = {"name": "Microphone (2- USB Audio Device", "hostapi": MME}
SNOWBALL = {"name": "Microphone (Blue Snowball )", "hostapi": MME}
GAP_FRAMES = int(config.SAMPLE_RATE * recorder_module.SWITCH_GAP_MS / 1000)


class FakeStream:
    def __init__(self, device, callback, finished_callback, **kwargs):
        self.device = device
        self.callback = callback
        self.finished_callback = finished_callback
        self.kwargs = kwargs
        self.active = False
        self.aborted = self.closed = False
        self.on_start = None

    def start(self):
        self.active = True
        if self.on_start is not None:
            self.on_start(self)

    def stop(self):
        self.active = False

    def abort(self):
        self.aborted = True
        self.active = False

    def close(self):
        self.closed = True

    def feed(self, samples, status=None):
        self.callback(samples.reshape(-1, 1), len(samples), None, status)


class StreamFactory:
    def __init__(self, refuse=(), before_open=None):
        self.opened = []
        self.refuse = set(refuse)
        self.before_open = before_open
        self.on_start = None

    def __call__(self, **kwargs):
        if self.before_open is not None:
            self.before_open(kwargs["device"])
        if kwargs["device"] in self.refuse:
            raise Exception("Error opening InputStream: Device unavailable [PaErrorCode -9985]")
        stream = FakeStream(**kwargs)
        stream.on_start = self.on_start
        self.opened.append(stream)
        return stream


@pytest.fixture
def fake_sd(monkeypatch):
    sd = windows_sd()
    sd.InputStream = StreamFactory()
    monkeypatch.setattr(recorder_module, "sd", sd)
    monkeypatch.setattr(audio_devices.sys, "platform", "win32")
    return sd


@pytest.fixture
def make_recorder(tmp_path):
    made = []

    def build(priority=(USB, SNOWBALL)):
        recorder = AudioRecorder(output_file=str(tmp_path / "dictation.wav"), device_priority=list(priority))
        made.append(recorder)
        return recorder

    yield build
    for recorder in made:
        recorder.cleanup()
    assert audio_devices.open_stream_count() == 0


def _tone(value, frames=config.CHUNK_SIZE):
    return np.full(frames, value, dtype=np.int16)


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def _saved_samples(recorder):
    recorder._audio_spool._queue.join()
    assert recorder.save_recording()
    with wave.open(recorder.output_file) as saved:
        return np.frombuffer(saved.readframes(saved.getnframes()), dtype=np.int16)


def _watch(recorder):
    switches, errors = [], []
    switched = threading.Event()
    failed = threading.Event()
    recorder.device_switch_callback = lambda old, new: (switches.append((old, new)), switched.set())
    recorder.error_callback = lambda message: (errors.append(message), failed.set())
    return switches, errors, switched, failed


def test_start_moves_past_a_microphone_that_will_not_open(fake_sd, make_recorder):
    fake_sd.InputStream.refuse = {2}
    recorder = make_recorder()

    assert recorder.start_recording()

    assert [stream.device for stream in fake_sd.InputStream.opened] == [1]
    assert recorder.active_device.index == 1
    assert recorder._failed_keys == [USB]
    assert audio_devices.open_stream_count() == 1


def test_wasapi_microphones_open_with_conversion(fake_sd, make_recorder):
    recorder = make_recorder([{"name": "Microphone (2- USB Audio Device)", "hostapi": WASAPI}])

    assert recorder.start_recording()

    stream = fake_sd.InputStream.opened[0]
    assert stream.device == 5 and stream.kwargs["extra_settings"].auto_convert is True
    assert stream.kwargs["samplerate"] == config.SAMPLE_RATE


def test_without_a_ranked_microphone_start_queries_no_driver(fake_sd, make_recorder, monkeypatch):
    monkeypatch.setattr(fake_sd, "query_devices", lambda: pytest.fail("start must not enumerate"))
    recorder = make_recorder(())

    assert recorder.start_recording()

    assert fake_sd.InputStream.opened[0].device is None
    assert recorder.active_device is None


def test_when_nothing_opens_start_reports_no_device(fake_sd, make_recorder):
    fake_sd.InputStream.refuse = {None, 0, 1, 2}
    recorder = make_recorder()

    assert not recorder.start_recording()

    assert recorder.last_start_error == "No audio device available"
    assert recorder.stream is None
    assert audio_devices.open_stream_count() == 0


def test_a_lost_microphone_hands_the_same_recording_to_the_next(fake_sd, make_recorder, monkeypatch):
    metrics = []
    monkeypatch.setattr("services.diagnostics.record_metrics", lambda **values: metrics.append(values))
    recorder = make_recorder()
    switches, errors, switched, _failed = _watch(recorder)
    assert recorder.start_recording()
    usb = fake_sd.InputStream.opened[0]
    usb.feed(_tone(100))
    first_gate = recorder._post_roll_gate

    usb.active = False  # unplugged
    assert switched.wait(3)

    snowball = fake_sd.InputStream.opened[1]
    assert snowball.device == 1 and snowball.active
    assert usb.aborted and usb.closed
    assert switches == [("USB Audio Device", "Blue Snowball")]
    assert recorder._post_roll_gate is not first_gate
    usb.feed(_tone(999))  # a late block from the dead stream
    snowball.feed(_tone(200))
    assert recorder._post_roll_gate.frames_before_stop == config.CHUNK_SIZE
    recorder.stop_recording()
    recorder._end_post_roll("test")
    assert recorder.wait_for_stop_completion(3)

    samples = _saved_samples(recorder)
    chunk = config.CHUNK_SIZE
    assert (samples[:chunk] == 100).all()
    assert (samples[chunk:chunk + GAP_FRAMES] == 0).all()
    assert (samples[chunk + GAP_FRAMES:2 * chunk + GAP_FRAMES] == 200).all()
    assert errors == [] and recorder.dropped_frames == 0 and not recorder.last_capture_error
    assert metrics[-1]["device_switches"] == 1
    assert audio_devices.open_stream_count() == 0


def test_blocks_from_the_replacement_never_land_before_the_gap(fake_sd, make_recorder):
    recorder = make_recorder()
    _switches, _errors, switched, _failed = _watch(recorder)
    assert recorder.start_recording()
    usb = fake_sd.InputStream.opened[0]
    usb.feed(_tone(100))
    # PortAudio may deliver a block before the switch has finished.
    fake_sd.InputStream.on_start = lambda stream: stream.feed(_tone(300))

    usb.active = False
    assert switched.wait(3)
    fake_sd.InputStream.opened[1].feed(_tone(200))
    recorder.stop_recording()
    recorder._end_post_roll("test")
    assert recorder.wait_for_stop_completion(3)

    samples = _saved_samples(recorder)
    chunk = config.CHUNK_SIZE
    assert 300 not in samples
    assert (samples[chunk:chunk + GAP_FRAMES] == 0).all()
    assert (samples[chunk + GAP_FRAMES:2 * chunk + GAP_FRAMES] == 200).all()


def test_a_finished_stream_counts_as_lost_but_our_own_close_does_not(fake_sd, make_recorder):
    recorder = make_recorder()
    switches, _errors, switched, _failed = _watch(recorder)
    assert recorder.start_recording()
    usb = fake_sd.InputStream.opened[0]
    usb.feed(_tone(1))

    usb.finished_callback()
    assert switched.wait(3)
    snowball = fake_sd.InputStream.opened[1]
    snowball.feed(_tone(2))
    recorder.stop_recording()
    recorder._end_post_roll("test")
    assert recorder.wait_for_stop_completion(3)

    snowball.finished_callback()  # PortAudio reports our own abort too
    assert recorder._stream_lost.is_set() is False
    assert len(switches) == 1


def test_blocks_stopping_counts_as_lost(fake_sd, make_recorder, monkeypatch):
    monkeypatch.setattr(recorder_module, "STALL_AFTER_BLOCKS_S", 0.3)
    recorder = make_recorder()
    switches, _errors, switched, _failed = _watch(recorder)
    assert recorder.start_recording()
    fake_sd.InputStream.opened[0].feed(_tone(5))

    assert switched.wait(3)  # no block for longer than the stall limit
    assert [stream.device for stream in fake_sd.InputStream.opened] == [2, 1]


def test_an_overflow_fails_the_recording_without_switching(fake_sd, make_recorder):
    recorder = make_recorder()
    switches, errors, _switched, failed = _watch(recorder)
    assert recorder.start_recording()

    fake_sd.InputStream.opened[0].feed(_tone(5), status="input overflow")

    assert failed.wait(3)
    assert "overflow" in errors[0]
    assert switches == [] and len(fake_sd.InputStream.opened) == 1


def test_no_switch_after_the_stop_press(fake_sd, make_recorder):
    recorder = make_recorder()
    switches, errors, _switched, _failed = _watch(recorder)
    assert recorder.start_recording()
    usb = fake_sd.InputStream.opened[0]
    usb.feed(_tone(5))
    recorder.stop_recording()

    usb.active = False
    time.sleep(0.3)
    recorder._end_post_roll("test")
    assert recorder.wait_for_stop_completion(3)

    assert switches == [] and errors == []
    assert len(fake_sd.InputStream.opened) == 1


def test_a_cancel_while_switching_keeps_the_replacement_closed(fake_sd, make_recorder):
    recorder = make_recorder()
    switches, errors, _switched, _failed = _watch(recorder)
    assert recorder.start_recording()
    usb = fake_sd.InputStream.opened[0]
    usb.feed(_tone(5))
    canceled = threading.Event()

    def cancel_on_reopen(device):
        if device == 1 and not canceled.is_set():
            canceled.set()
            recorder.cancel_recording()

    fake_sd.InputStream.before_open = cancel_on_reopen
    usb.active = False
    assert canceled.wait(3)
    assert recorder.wait_for_stop_completion(3)

    assert switches == [] and errors == []
    replacement = fake_sd.InputStream.opened[-1]
    assert replacement.device == 1 and replacement.closed
    assert audio_devices.open_stream_count() == 0


def test_with_no_other_microphone_the_partial_recording_fails_as_before(fake_sd, make_recorder):
    recorder = make_recorder()
    switches, errors, _switched, failed = _watch(recorder)
    assert recorder.start_recording()
    usb = fake_sd.InputStream.opened[0]
    usb.feed(_tone(7))
    fake_sd.InputStream.refuse = {0, 1}

    usb.active = False

    assert failed.wait(3)
    assert errors == [recorder_module.NO_MICROPHONE_LEFT]
    assert switches == []
    assert recorder.wait_for_stop_completion(3)
    recorder._audio_spool._queue.join()
    assert recorder.save_recording(allow_incomplete=True)


def test_the_default_microphone_falls_back_to_the_sound_mapper(fake_sd, make_recorder):
    recorder = make_recorder(())
    switches, _errors, switched, _failed = _watch(recorder)
    assert recorder.start_recording()
    fake_sd.InputStream.opened[0].feed(_tone(3))

    fake_sd.InputStream.opened[0].active = False

    assert switched.wait(3)
    assert [stream.device for stream in fake_sd.InputStream.opened] == [None, 0]
    assert switches == [("", "")]


def test_from_settings_uses_the_saved_order(fake_sd):
    settings_manager.update_settings({SettingsKey.AUDIO_INPUT_PRIORITY: [SNOWBALL, USB]})

    recorder = AudioRecorder.from_settings()

    assert recorder.device_priority == [SNOWBALL, USB]
    recorder.cleanup()


def test_from_settings_migrates_the_legacy_index(fake_sd):
    settings_manager.update_settings({SettingsKey.AUDIO_INPUT_DEVICE: 2})

    recorder = AudioRecorder.from_settings()

    assert recorder.device_priority == [USB]
    assert settings_manager.get(SettingsKey.AUDIO_INPUT_PRIORITY) == [USB]
    recorder.cleanup()


def test_start_falls_back_to_the_default_when_devices_cannot_be_listed(monkeypatch, make_recorder):
    sd = FakeSd([], [])
    sd.InputStream = StreamFactory()
    monkeypatch.setattr(sd, "query_devices", lambda: (_ for _ in ()).throw(OSError("PortAudio not initialized")))
    monkeypatch.setattr(recorder_module, "sd", sd)
    recorder = make_recorder()

    assert recorder.start_recording()

    assert [stream.device for stream in sd.InputStream.opened] == [None]
