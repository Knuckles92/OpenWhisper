"""A History refresh that rebuilds a card in place keeps the click waiting on its download."""
import importlib
import threading

import pytest

import tests.test_personalize_fix2_g3_playback_fetch as fetch_tests
import tests.test_personalize_s9_audio_player as player_tests


def _playback():
    return importlib.import_module("ui_qt.widgets.history_playback")


@pytest.fixture
def device(monkeypatch):
    device = player_tests.FakeSoundDevice()
    module = _playback().audio_player
    player = module.AudioPlayer(sounddevice=device)
    monkeypatch.setattr(module, "_player", player)
    yield device
    player.stop()


@pytest.fixture
def slow_host(monkeypatch, tmp_path):
    playback = _playback()
    monkeypatch.setattr(playback, "_fetched", {})
    monkeypatch.setattr(playback, "_in_flight", {})
    sync_module = importlib.import_module("services.remote_records.sync")

    class Sync:
        def __init__(self):
            self.release = threading.Event()
            self.calls = 0
            self.audio = player_tests._wav(tmp_path / "cached.wav")

        def record_saved(self, *_args):
            pass

        def record_edited(self, *_args):
            pass

        def record_deleted(self, *_args):
            pass

        def listing_wanted(self, _kind):
            return False

        def audio_for(self, _record_id):
            self.calls += 1
            if not self.release.wait(5):
                raise TimeoutError("test download never released")
            return self.audio

    fake = Sync()
    monkeypatch.setattr(sync_module.record_sync, "_instance", fake)
    yield fake
    fake.release.set()
    fetch_tests._pump(50)


@pytest.fixture
def sidebar():
    from ui_qt.widgets.history_sidebar import HistorySidebar

    sidebar = HistorySidebar()
    sidebar.expand()
    yield sidebar
    sidebar.deleteLater()


def _click_then_rebuild(sidebar):
    fetch_tests._show(sidebar, {"entries": [fetch_tests._remote()], "notice": ""})
    card = sidebar._history_cards["h1"][1]
    card.playback.button.click()
    # Edited on the host while the download runs: one refresh rebuilds the card.
    edited = fetch_tests._remote()
    edited.text = "Edited on the host."
    fetch_tests._show(sidebar, {"entries": [edited], "notice": ""})
    rebuilt = sidebar._history_cards["h1"][1]
    assert rebuilt is not card
    assert rebuilt.playback.button.text() == "Getting it…"
    return rebuilt


def test_an_in_place_rebuild_mid_download_still_plays_the_click(device, slow_host, sidebar):
    rebuilt = _click_then_rebuild(sidebar)

    slow_host.release.set()
    assert player_tests._wait(lambda: (fetch_tests._pump(10) or True) and bool(device.streams))
    assert rebuilt.playback.is_playing and rebuilt.playback.button.text() == "Stop"
    assert slow_host.calls == 1


def test_stopping_before_a_rebuilt_cards_download_finishes_plays_nothing(device, slow_host, sidebar):
    rebuilt = _click_then_rebuild(sidebar)
    _playback().stop_all()

    slow_host.release.set()
    fetch_tests._pump(300)
    assert device.streams == []
    assert rebuilt.playback.button.text() == "Play" and rebuilt.playback.button.isEnabled()
