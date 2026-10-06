"""An Undo AI edit made while its dictation is being sent still reaches the host.

Uses the real SpeechHost and HostRecordStore fixtures of
tests/test_personalize_s9_remote_records.py.
"""
from __future__ import annotations

import importlib

import pytest

from services.remote_records.sync import RecordSync
from tests.test_personalize_s9_remote_records import (  # noqa: F401 (pytest fixtures)
    _dictate,
    client,
    host,
    no_real_tailscale,
)

AI = "Hello from the laptop."
ORIGINAL = "hello from the laptop um"


def _history():
    return importlib.import_module("services.history_manager")


@pytest.fixture
def undo_while_sending(monkeypatch):
    """The first upload's files go out with the AI text; the user picks the original meanwhile."""
    history = _history()
    real_send = RecordSync._send
    entry_ids = []

    def send(self, *args, **kwargs):
        result = real_send(self, *args, **kwargs)
        if entry_ids:
            history.history_manager.use_version(entry_ids.pop(), history.ORIGINAL_VERSION)
        return result

    monkeypatch.setattr(RecordSync, "_send", send)
    return entry_ids


def test_a_copy_kept_on_both_is_sent_again(host, client, tmp_path, undo_while_sending):  # noqa: F811
    client.set_location("both")
    entry = _dictate(tmp_path)
    undo_while_sending.append(entry.id)

    client.run_once()

    assert host.db.get_history_entry_by_id(entry.id).text == AI
    row = client._row("dictation", entry.id)
    assert (row.action, row.state) == ("copy", "pending")
    assert client._wake.is_set()

    client.run_once()

    assert host.db.get_history_entry_by_id(entry.id).text == ORIGINAL
    row = client._row("dictation", entry.id)
    assert (row.action, row.state) == ("copy", "done")
    assert row.content_digest == client.kinds["dictation"].digest(entry.id)


def test_a_moved_entry_stays_here_until_the_edit_is_sent(host, client, tmp_path, undo_while_sending):  # noqa: F811
    history = _history()
    client.set_location("host")
    entry = _dictate(tmp_path)
    undo_while_sending.append(entry.id)

    client.run_once()

    assert history.history_manager.get_entry_by_id(entry.id).text == ORIGINAL
    assert (client._row("dictation", entry.id).state) == "pending"

    client.run_once()

    assert host.db.get_history_entry_by_id(entry.id).text == ORIGINAL
    assert history.history_manager.get_entry_by_id(entry.id) is None
    assert client._row("dictation", entry.id) is None


def test_an_unedited_upload_is_done_once(host, client, tmp_path, monkeypatch):  # noqa: F811
    client.set_location("both")
    entry = _dictate(tmp_path)
    sends = []
    real_send = RecordSync._send
    monkeypatch.setattr(RecordSync, "_send",
                        lambda self, *args: sends.append(1) or real_send(self, *args))

    client.run_once()
    client.run_once()

    assert sends == [1]
    row = client._row("dictation", entry.id)
    assert (row.action, row.state) == ("copy", "done")
    assert row.content_digest == client.kinds["dictation"].digest(entry.id)
