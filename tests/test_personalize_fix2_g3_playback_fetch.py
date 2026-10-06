"""A host recording download that outlives the card or dialog that asked for it."""
import importlib
import threading
import time

import pytest
from PyQt6.QtCore import QEvent
from PyQt6.QtWidgets import QApplication

import tests.test_personalize_s9_audio_player as player_tests


def _playback():
    return importlib.import_module("ui_qt.widgets.history_playback")


def _remote():
    from ui_qt.widgets.history_sidebar import remote_history_entry

    return remote_history_entry({
        "id": "h1", "text": "On the host.", "timestamp": "2026-10-06T10:00:00+00:00",
        "model": "base", "stored_on": "devbox", "has_audio": True, "file_size": 22050,
    })


def _pump(ms=200):
    deadline = time.monotonic() + ms / 1000
    while time.monotonic() < deadline:
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        time.sleep(0.005)


@pytest.fixture
def device(monkeypatch):
    device = player_tests.FakeSoundDevice()
    module = _playback().audio_player
    player = module.AudioPlayer(sounddevice=device)
    monkeypatch.setattr(module, "_player", player)
    yield device
    player.stop()


@pytest.fixture
def thread_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(repr(args.exc_value)))
    return errors


@pytest.fixture
def slow_host(monkeypatch, tmp_path):
    """A host whose download waits for ``release``, like one in progress."""
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
    _pump(50)


def _show(sidebar, entries):
    sidebar._history_load_generation += 1
    sidebar._apply_history_results(sidebar._history_load_generation, "", entries, "")


def test_a_refresh_mid_download_hands_the_click_to_the_rebuilt_card(device, slow_host, thread_errors):
    from ui_qt.widgets.history_sidebar import HistorySidebar

    sidebar = HistorySidebar()
    sidebar.expand()
    _show(sidebar, {"entries": [_remote()], "notice": ""})
    card = sidebar._history_cards["h1"][1]
    card.playback.button.click()
    assert card.playback.button.text() == "Getting it…"

    # A host listing slower than REMOTE_MERGE_WAIT_S shows the local entries
    # first, which deletes the host-kept card; the merged list rebuilds it.
    _show(sidebar, [])
    _pump(50)
    _show(sidebar, {"entries": [_remote()], "notice": ""})
    rebuilt = sidebar._history_cards["h1"][1]
    assert rebuilt is not card
    assert rebuilt.playback.button.text() == "Getting it…"
    assert not rebuilt.playback.button.isEnabled()

    slow_host.release.set()
    assert player_tests._wait(lambda: (_pump(10) or True) and bool(device.streams))
    assert player_tests._wait(lambda: device.streams[0].started)
    assert thread_errors == []
    assert rebuilt.playback.is_playing and rebuilt.playback.button.text() == "Stop"
    assert _playback()._fetched == {"h1": slow_host.audio}
    assert slow_host.calls == 1
    sidebar.deleteLater()


def test_a_card_deleted_mid_download_plays_nothing_but_keeps_the_file(device, slow_host, thread_errors):
    from ui_qt.widgets.history_sidebar import HistoryItemWidget

    card = HistoryItemWidget(_remote())
    card.playback.button.click()
    card.deleteLater()
    _pump(20)

    slow_host.release.set()
    _pump(300)
    assert thread_errors == []
    assert device.streams == []
    assert _playback()._fetched == {"h1": slow_host.audio}

    later = HistoryItemWidget(_remote())
    assert later.playback.button.text() == "Play" and later.playback.button.isEnabled()
    later.playback.button.click()
    assert player_tests._wait(lambda: device.streams and device.streams[0].started)
    assert slow_host.calls == 1


def test_closing_the_dialog_mid_download_cancels_the_play(device, slow_host, thread_errors):
    from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog

    dialog = HistoryEntryDialog(_remote())
    dialog.play_button.click()
    assert dialog.play_button.text() == "Getting it…"
    dialog.reject()

    slow_host.release.set()
    _pump(300)
    assert thread_errors == []
    assert device.streams == []
    assert dialog.play_button.text() == "Play" and dialog.play_button.isEnabled()


def test_collapsing_history_mid_download_cancels_the_play(device, slow_host, thread_errors):
    from ui_qt.widgets.history_sidebar import HistorySidebar

    sidebar = HistorySidebar()
    sidebar.expand()
    _show(sidebar, {"entries": [_remote()], "notice": ""})
    sidebar._history_cards["h1"][1].playback.button.click()
    sidebar.collapse()

    slow_host.release.set()
    _pump(300)
    assert thread_errors == []
    assert device.streams == []
    sidebar.deleteLater()


def test_a_second_click_joins_the_download_and_plays_where_it_was_made(device, slow_host, thread_errors):
    from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog
    from ui_qt.widgets.history_sidebar import HistoryItemWidget

    dialog = HistoryEntryDialog(_remote())
    card = HistoryItemWidget(_remote())
    card.playback.button.click()
    dialog.play_button.click()
    assert dialog.play_button.text() == "Getting it…"

    slow_host.release.set()
    assert player_tests._wait(lambda: (_pump(10) or True) and bool(device.streams))
    assert thread_errors == []
    assert slow_host.calls == 1
    assert dialog.playback.is_playing and not card.playback.is_playing
    assert card.playback.button.text() == "Play"
    dialog.reject()


def test_a_failed_download_is_reported_on_the_card_that_asked(device, slow_host, thread_errors, monkeypatch):
    from ui_qt.widgets.history_sidebar import HistoryItemWidget

    def unreachable(_record_id):
        slow_host.release.wait(5)
        raise ConnectionError("host offline")

    monkeypatch.setattr(slow_host, "audio_for", unreachable)
    asked, other = HistoryItemWidget(_remote()), None
    asked.playback.button.click()
    other = HistoryItemWidget(_remote())
    assert other.playback.button.text() == "Getting it…"

    slow_host.release.set()
    _pump(300)
    assert thread_errors == []
    assert asked.playback.note == "Couldn't get the recording: host offline"
    assert other.playback.note == ""
    for card in (asked, other):
        assert card.playback.button.text() == "Play" and card.playback.button.isEnabled()
    assert _playback()._fetched == {}
