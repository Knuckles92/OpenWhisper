"""History playback through a dedicated output stream, with a fake device."""

import importlib
import threading
import time
import wave

import numpy as np
import pytest


def _module():
    return importlib.import_module("services.audio_player")


class FakeStream:
    def __init__(self, device, **kwargs):
        self.device = device
        self.kwargs = kwargs
        self.started = self.aborted = self.closed = False
        self.finished = 0

    def start(self):
        if self.device.fail_start:
            raise RuntimeError("device busy")
        self.started = True

    def _end(self):
        self.finished += 1
        self.kwargs["finished_callback"]()

    def abort(self):
        self.aborted = True
        self._end()

    def close(self):
        self.closed = True

    def pull(self, frames=4096):
        """One device callback; True while the stream keeps going."""
        out = np.full((frames, 1), 99, dtype=np.int16)
        try:
            self.kwargs["callback"](out, frames, None, None)
        except self.device.CallbackStop:
            self.last = out
            self._end()
            return False
        self.last = out
        return True


class FakeSoundDevice:
    class CallbackStop(Exception):
        pass

    def __init__(self):
        self.streams = []
        self.fail_open = False
        self.fail_start = False

    def OutputStream(self, **kwargs):
        if self.fail_open:
            raise RuntimeError("no output device")
        stream = FakeStream(self, **kwargs)
        self.streams.append(stream)
        return stream


def _wait(until, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if until():
            return True
        time.sleep(0.005)
    return until()


def _wav(path, *, rate=44100, channels=1, width=2, seconds=0.25, value=1200):
    frames = int(rate * seconds)
    samples = np.arange(frames * channels, dtype=np.int64) % 2000 + value
    data = samples.astype("<i2").tobytes() if width == 2 else bytes(frames * channels)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(data)
    return str(path)


@pytest.fixture
def device():
    return FakeSoundDevice()


@pytest.fixture
def player(device):
    player = _module().AudioPlayer(sounddevice=device)
    yield player
    player.stop()


def test_only_the_recorders_own_wavs_play(player, tmp_path, device):
    text = tmp_path / "notes.wav"
    text.write_text("not audio", encoding="utf-8")
    refused = [
        _wav(tmp_path / "16k.wav", rate=16000),
        _wav(tmp_path / "stereo.wav", channels=2),
        _wav(tmp_path / "8bit.wav", width=1),
        _wav(tmp_path / "empty.wav", seconds=0),
        str(text),
        str(tmp_path / "missing.wav"),
    ]
    for path in refused:
        assert player.play(path) is False, path
        assert player.last_error
        assert not player.is_playing
    assert device.streams == []


def test_play_streams_the_samples_and_stop_ends_it_once(player, tmp_path, device):
    path = _wav(tmp_path / "take.wav")
    finished = []

    assert player.play(path, on_finished=lambda: finished.append(threading.get_ident()))
    assert player.is_playing and player.path == path and player.session == 1
    assert player.duration == pytest.approx(0.25)
    assert _wait(lambda: device.streams and device.streams[0].started)
    stream = device.streams[0]
    assert stream.kwargs["samplerate"] == 44100
    assert stream.kwargs["channels"] == 1 and stream.kwargs["dtype"] == "int16"

    assert stream.pull(1000)
    with wave.open(path, "rb") as handle:
        expected = np.frombuffer(handle.readframes(1000), dtype="<i2")
    assert np.array_equal(stream.last[:, 0], expected)
    assert player.position == pytest.approx(1000 / 44100)

    player.stop()
    assert stream.aborted and stream.closed
    assert not player.is_playing and player.session == 0 and player.position == 0
    assert len(finished) == 1
    player.stop()
    assert len(finished) == 1


def test_a_recording_played_to_the_end_finishes_and_closes(player, tmp_path, device):
    path = _wav(tmp_path / "take.wav", seconds=0.05)
    finished = []
    assert player.play(path, on_finished=lambda: finished.append(True))
    assert _wait(lambda: device.streams and device.streams[0].started)
    stream = device.streams[0]

    while stream.pull(1024):
        pass

    total = int(44100 * 0.05)
    assert not stream.last[total % 1024:].any()
    assert finished == [True]
    assert not player.is_playing
    assert _wait(lambda: stream.closed)
    assert not stream.aborted


def test_playing_another_recording_replaces_the_first(player, tmp_path, device):
    first, second = [], []
    assert player.play(_wav(tmp_path / "a.wav"), on_finished=lambda: first.append(True))
    assert _wait(lambda: device.streams and device.streams[0].started)
    assert player.play(_wav(tmp_path / "b.wav"), on_finished=lambda: second.append(True))

    assert first == [True] and second == []
    assert device.streams[0].closed
    assert player.session == 2
    assert _wait(lambda: len(device.streams) == 2 and device.streams[1].started)


def test_a_device_that_wont_open_ends_the_playback(player, tmp_path, device):
    device.fail_open = True
    finished = []
    assert player.play(_wav(tmp_path / "take.wav"), on_finished=lambda: finished.append(True))
    assert _wait(lambda: finished == [True])
    assert not player.is_playing
    assert "no output device" in player.last_error


def test_a_device_that_wont_start_is_closed(player, tmp_path, device):
    device.fail_start = True
    finished = []
    assert player.play(_wav(tmp_path / "take.wav"), on_finished=lambda: finished.append(True))
    assert _wait(lambda: finished == [True])
    assert device.streams[0].closed and not player.is_playing


def test_stopping_while_the_file_loads_opens_no_device(player, tmp_path, device, monkeypatch):
    module = _module()
    release = threading.Event()
    real_read = module._read_samples

    def slow_read(path, frames):
        release.wait(3)
        return real_read(path, frames)

    monkeypatch.setattr(module, "_read_samples", slow_read)
    finished = []
    assert player.play(_wav(tmp_path / "take.wav"), on_finished=lambda: finished.append(True))
    player.stop()
    release.set()
    time.sleep(0.1)
    assert finished == [True]
    assert device.streams == []


def test_a_raising_finished_callback_is_contained(player, tmp_path, device):
    assert player.play(_wav(tmp_path / "take.wav"), on_finished=lambda: 1 / 0)
    assert _wait(lambda: device.streams and device.streams[0].started)
    player.stop()
    assert not player.is_playing


def test_stop_playback_never_creates_a_player(monkeypatch, device, tmp_path):
    module = _module()
    monkeypatch.setattr(module, "_player", None)
    module.stop_playback()
    assert module._player is None

    shared = module.AudioPlayer(sounddevice=device)
    monkeypatch.setattr(module, "_player", shared)
    assert module.player() is shared
    assert shared.play(_wav(tmp_path / "take.wav"))
    stopper = threading.Thread(target=module.stop_playback)
    stopper.start()
    stopper.join()
    assert not shared.is_playing


def test_a_meeting_start_stops_playback(monkeypatch):
    module = _module()
    stopped = []
    monkeypatch.setattr(module, "stop_playback", lambda: stopped.append(True))
    meeting = importlib.import_module("services.runtime.meeting")

    class Controller:
        meeting_active = False

        def __getattr__(self, name):
            return type("Signal", (), {"emit": lambda *_a: None})()

    runtime = meeting.MeetingRuntime.__new__(meeting.MeetingRuntime)
    runtime.controller = Controller()
    runtime._lock = threading.Lock()
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    runtime._launch(False)

    assert stopped == [True]
