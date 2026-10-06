"""A host-kept recording is downloaded once, and nobody gets a half-written file."""
import base64
import importlib
import json
import os
import threading
import time

import pytest
from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

import tests.test_personalize_s9_audio_player as player_tests
from services.remote_records.sync import RecordSync

CHUNK = 64 * 1024
MANIFEST = json.dumps({"kind": "dictation", "audio": "audio/take.wav"}).encode()


class SlowHost:
    """Sends record.json first, then the audio, stalling after its first chunk."""

    def __init__(self, audio: bytes):
        self.audio = audio
        self.gate = threading.Event()
        self.mid_file = threading.Event()
        self.opens = 0
        self.drop_mid_file = False

    def connect(self):
        return _Connection(self)


class _Connection:
    ready = {"capabilities": {"records": True}, "records": {}}

    def __init__(self, host: SlowHost):
        self.host = host

    def request(self, op, **kwargs):
        host = self.host
        if op == "records_open":
            host.opens += 1
            return {"files": [{"name": "record.json", "size": len(MANIFEST)},
                              {"name": "audio/take.wav", "size": len(host.audio)}]}
        assert op == "records_fetch"
        name, offset = kwargs["name"], kwargs["offset"]
        data = host.audio if name == "audio/take.wav" else MANIFEST
        if name == "audio/take.wav" and offset >= CHUNK:
            host.mid_file.set()
            if host.drop_mid_file:
                raise ConnectionError("link dropped")
            assert host.gate.wait(5), "test download never released"
        piece = data[offset:offset + CHUNK]
        return {"data": base64.b64encode(piece).decode(), "eof": offset + len(piece) >= len(data)}

    def close(self):
        pass


def _audio(tmp_path) -> bytes:
    path = player_tests._wav(tmp_path / "take.wav", seconds=4)
    with open(path, "rb") as handle:
        audio = handle.read()
    assert len(audio) > 3 * CHUNK
    return audio


def _size(path: str) -> int:
    return os.path.getsize(path) if os.path.exists(path) else -1


@pytest.fixture
def host(tmp_path):
    host = SlowHost(_audio(tmp_path))
    yield host
    host.gate.set()


@pytest.fixture
def sync(host, tmp_path):
    return RecordSync(kinds={}, connect=host.connect, cache_dir=str(tmp_path / "cache"))


def test_a_second_caller_waits_for_the_running_download(host, sync):
    results = {}

    def call(name):
        results[name] = sync.audio_for("h1")

    first = threading.Thread(target=call, args=("first",), daemon=True)
    first.start()
    assert host.mid_file.wait(3)
    second = threading.Thread(target=call, args=("second",), daemon=True)
    second.start()
    second.join(0.3)
    assert "second" not in results

    host.gate.set()
    first.join(3)
    second.join(3)
    assert results["first"] == results["second"]
    assert _size(results["second"]) == len(host.audio)
    assert host.opens == 1


def test_an_interrupted_download_is_not_kept_as_the_cache(host, sync, tmp_path):
    host.drop_mid_file = True
    with pytest.raises(ConnectionError):
        sync.audio_for("h1")

    host.drop_mid_file = False
    host.gate.set()
    path = sync.audio_for("h1")
    assert _size(path) == len(host.audio)
    assert host.opens == 2
    assert os.listdir(tmp_path / "cache") == ["h1"]


def _pump(ms=200):
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        time.sleep(0.005)


@pytest.fixture
def device(monkeypatch):
    device = player_tests.FakeSoundDevice()
    module = importlib.import_module("ui_qt.widgets.history_playback").audio_player
    player = module.AudioPlayer(sounddevice=device)
    monkeypatch.setattr(module, "_player", player)
    yield device
    player.stop()


@pytest.fixture
def card(host, sync, device, monkeypatch):
    playback = importlib.import_module("ui_qt.widgets.history_playback")
    monkeypatch.setattr(playback, "_fetched", {})
    monkeypatch.setattr(playback, "_in_flight", {})
    sync_module = importlib.import_module("services.remote_records.sync")
    monkeypatch.setattr(sync_module.record_sync, "_instance", sync)
    from ui_qt.widgets.history_sidebar import HistoryItemWidget, remote_history_entry

    card = HistoryItemWidget(remote_history_entry({
        "id": "h1", "text": "On the host.", "timestamp": "2026-10-06T10:00:00+00:00",
        "model": "base", "stored_on": "devbox", "has_audio": True, "file_size": len(host.audio),
    }))
    card.got = []
    card.retranscribe_requested.connect(lambda path: card.got.append(_size(path)))
    yield card
    host.gate.set()
    card.deleteLater()
    _pump(50)


def test_transcribe_again_during_play_waits_for_the_whole_recording(host, card, device):
    card.playback.button.click()
    assert host.mid_file.wait(3)
    card.retranscribe_btn.click()
    _pump(300)
    assert card.got == []

    host.gate.set()
    assert player_tests._wait(lambda: (_pump(10) or True) and bool(card.got and device.streams))
    assert card.got == [len(host.audio)]
    assert host.opens == 1


def test_play_during_transcribe_again_waits_for_the_whole_recording(host, card, device):
    card.retranscribe_btn.click()
    assert host.mid_file.wait(3)
    card.playback.button.click()
    _pump(300)
    assert device.streams == []
    assert card.playback.button.text() == "Getting it…"

    host.gate.set()
    assert player_tests._wait(lambda: (_pump(10) or True) and bool(card.got and device.streams))
    assert card.got == [len(host.audio)]
    assert card.playback.is_playing
    assert _size(importlib.import_module("ui_qt.widgets.history_playback")._fetched["h1"]) == len(host.audio)
    assert host.opens == 1
