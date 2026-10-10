"""Paste original picks this computer's newest dictation when records move to the host.

A move to the paired host deletes the local row about a second after the
save, so the newest row left here can be a days-old one from before the
switch. That one must never be pasted in place of the dictation just made.
"""

import importlib
import os
import threading
from types import SimpleNamespace

import pytest

from services.remote_asr import settings as remote_settings
from services.remote_asr import tailscale
from services.remote_asr.client import RemoteConnection
from services.remote_asr.settings import ClientPairing
from services.remote_records import sync as sync_module
from services.remote_records.kinds import DictationRecords, MeetingRecords
from services.remote_records.sync import RecordSync
from tests import test_remote_records as remote_tests
from ui_qt import history_actions


@pytest.fixture(autouse=True)
def no_real_tailscale(monkeypatch):
    monkeypatch.setattr(tailscale, "status", lambda timeout=4.0: tailscale.TailscaleStatus("not_installed"))
    monkeypatch.setattr(tailscale, "whois", lambda address, timeout=4.0: None)


@pytest.fixture
def host(tmp_path):
    host = remote_tests.Host(tmp_path / "host")
    yield host
    host.close()


@pytest.fixture
def client(host, tmp_path, monkeypatch):
    result = host.pair()
    pairing = ClientPairing("127.0.0.1", host.server.port, result.fingerprint, "devbox",
                            device_id=result.device_id)
    monkeypatch.setattr(remote_settings, "load_client_pairing", lambda settings=None: pairing)
    monkeypatch.setattr(remote_settings, "load_client_token", lambda: result.token)
    sync = RecordSync(
        {"dictation": DictationRecords(), "meeting": MeetingRecords(str(tmp_path / "meetings"))},
        connect=lambda: RemoteConnection("127.0.0.1", host.server.port, result.token,
                                         result.fingerprint),
        cache_dir=str(tmp_path / "cache"),
    )
    monkeypatch.setattr(sync_module.record_sync, "_instance", sync)
    sync.device_id = result.device_id
    yield sync
    sync.stop()


class FakeUI:
    def __init__(self):
        self.pasted = []
        self.copied = []
        self.statuses = []

    def on_paste_text_now(self, text):
        self.pasted.append(text)
        return True

    def copy_to_clipboard(self, text, html=""):
        self.copied.append(text)
        return True

    def set_status(self, text):
        self.statuses.append(text)

    def refresh_history(self):
        pass


def _history_module():
    return importlib.import_module("services.history_manager")


def _hm():
    return _history_module().history_manager


def _dictate(tmp_path, name, text, raw):
    source = remote_tests._wav(tmp_path / f"{name}.wav")
    return _hm().add_entry(
        text=text, raw_text=raw, model="parakeet (cuda)", source_audio_path=source,
        audio_duration=0.5, file_size=os.path.getsize(source),
        source_name="Quick Record", entry_kind="dictation",
    )


def _restart(monkeypatch):
    monkeypatch.setattr(_history_module().history_manager, "_instance", None)


def test_the_moved_dictation_is_pasted_not_an_old_local_one(host, client, tmp_path):
    old = _dictate(tmp_path, "old", "Old meeting notes.", "old meeting notes um")
    client.set_location("host")
    new = _dictate(tmp_path, "new", "Send the invoice today.", "send the invoice today um")
    client.run_once()
    assert _hm().get_entry_by_id(new.id) is None
    assert host.db.get_history_entry_by_id(new.id) is not None

    ui = FakeUI()
    history_actions.paste_last_original(ui)

    assert ui.pasted == ["send the invoice today um"]
    assert ui.statuses == [history_actions.PASTED]
    assert _hm().get_entry_by_id(old.id).text == "Old meeting notes."
    assert host.db.get_history_entry_by_id(new.id).text == "Send the invoice today."

    history_actions.copy_last_original(ui)
    assert ui.copied == ["send the invoice today um"]


def test_host_only_storage_still_pastes_the_newest_original(host, client, tmp_path):
    client.set_location("host")
    _dictate(tmp_path, "new", "Send the invoice today.", "send the invoice today um")
    client.run_once()

    ui = FakeUI()
    history_actions.paste_last_original(ui)

    assert ui.pasted == ["send the invoice today um"]


def test_after_a_restart_a_leftover_from_before_the_switch_is_not_offered(
    host, client, tmp_path, monkeypatch
):
    old = _dictate(tmp_path, "old", "Old meeting notes.", "old meeting notes um")
    client.set_location("host")
    _dictate(tmp_path, "new", "Send the invoice today.", "send the invoice today um")
    client.run_once()
    _restart(monkeypatch)

    ui = FakeUI()
    history_actions.paste_last_original(ui)

    assert ui.pasted == [] and ui.statuses == [history_actions.NOTHING_YET]
    assert _hm().get_entry_by_id(old.id).text == "Old meeting notes."


def test_deleting_the_newest_never_falls_back_to_a_leftover(host, client, tmp_path):
    old = _dictate(tmp_path, "old", "Old meeting notes.", "old meeting notes um")
    client.set_location("host")
    new = _dictate(tmp_path, "new", "Send the invoice today.", "send the invoice today um")
    assert _hm().delete_entry(new.id)

    ui = FakeUI()
    history_actions.paste_last_original(ui)

    assert ui.pasted == [] and ui.statuses == [history_actions.NOTHING_YET]
    assert _hm().get_entry_by_id(old.id).text == "Old meeting notes."


def test_deleting_the_moved_dictation_on_the_host_forgets_it(host, client, tmp_path, monkeypatch):
    from ui_qt.widgets import history_sidebar

    client.set_location("host")
    new = _dictate(tmp_path, "new", "Send the invoice today.", "send the invoice today um")
    client.run_once()
    deleted = threading.Event()
    relay = SimpleNamespace(deleted=SimpleNamespace(emit=lambda *_a: deleted.set()))
    monkeypatch.setattr(history_sidebar, "_remote_relay", lambda: relay)
    sidebar = SimpleNamespace(_token=1)

    history_sidebar.HistorySidebar.delete_remote_entry(sidebar, new.id)

    assert deleted.wait(10)
    assert host.db.get_history_entry_by_id(new.id) is None
    ui = FakeUI()
    history_actions.paste_last_original(ui)
    assert ui.pasted == [] and ui.statuses == [history_actions.NOTHING_YET]


def test_clearing_history_forgets_the_moved_dictation(host, client, tmp_path):
    client.set_location("host")
    _dictate(tmp_path, "new", "Send the invoice today.", "send the invoice today um")
    client.run_once()

    _hm().clear_history()

    ui = FakeUI()
    history_actions.paste_last_original(ui)
    assert ui.pasted == [] and ui.statuses == [history_actions.NOTHING_YET]


def test_kept_here_after_a_restart_the_newest_local_dictation_still_counts(tmp_path, monkeypatch):
    sync = SimpleNamespace(record_saved=lambda *_a: None, record_edited=lambda *_a: None,
                           record_deleted=lambda *_a: None, cleared=lambda *_a: None)
    monkeypatch.setattr(sync_module.record_sync, "_instance", sync)
    _hm().add_entry(text="Hello, world.", raw_text="um hello world", model="base",
                    entry_kind="dictation", source_name="Quick Record")
    _restart(monkeypatch)

    ui = FakeUI()
    history_actions.paste_last_original(ui)

    assert ui.pasted == ["um hello world"]
