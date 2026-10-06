"""Undo AI edit: choosing between an entry's original and AI versions."""

import importlib

import pytest

from services.history_manager import (
    AI_VERSION,
    ORIGINAL_VERSION,
    ai_text,
    entry_version,
    history_manager,
    kind_of,
)
from services.models import TranscriptionHistory


class _Sync:
    def __init__(self):
        self.edited = []
        self.saved = []

    def record_saved(self, kind, record_id):
        self.saved.append((kind, record_id))

    def record_edited(self, kind, record_id):
        self.edited.append((kind, record_id))

    def record_deleted(self, kind, record_id):
        pass


@pytest.fixture
def db():
    """The app's own database, which history_manager writes to."""
    return importlib.import_module("services.database").db


@pytest.fixture
def sync(monkeypatch):
    sync_module = importlib.import_module("services.remote_records.sync")
    fake = _Sync()
    monkeypatch.setattr(sync_module.record_sync, "_instance", fake)
    return fake


def _cleaned(text="Hello, world.", raw="hello world um", **fields):
    return history_manager.add_entry(text=text, model="base", raw_text=raw, **fields)


def test_saving_an_ai_edit_keeps_both_versions(sync):
    entry = _cleaned(entry_kind="dictation")

    stored = history_manager.get_entry_by_id(entry.id)
    assert (stored.text, stored.raw_text, stored.cleaned_text) == (
        "Hello, world.", "hello world um", "Hello, world.",
    )
    assert entry_version(stored) == AI_VERSION
    plain = history_manager.add_entry(text="as heard", model="base")
    assert history_manager.get_entry_by_id(plain.id).cleaned_text is None
    assert entry_version(plain) == ""


def test_choosing_the_original_and_back_never_swaps_fields(sync):
    entry = _cleaned()

    original = history_manager.use_version(entry.id, ORIGINAL_VERSION)
    assert (original.text, original.raw_text, original.cleaned_text) == (
        "hello world um", "hello world um", "Hello, world.",
    )
    stored = history_manager.get_entry_by_id(entry.id)
    assert entry_version(stored) == ORIGINAL_VERSION
    assert ai_text(stored) == "Hello, world."

    ai = history_manager.use_version(entry.id, AI_VERSION)
    assert (ai.text, ai.raw_text, ai.cleaned_text) == (
        "Hello, world.", "hello world um", "Hello, world.",
    )
    assert sync.edited == [("dictation", entry.id), ("dictation", entry.id)]


def test_a_legacy_entry_fills_in_its_ai_version_on_the_first_undo(db, sync):
    db.add_history_entry(
        entry_id="legacy", text="Cleaned long ago.", raw_text="cleaned long ago",
        timestamp="2026-01-01T09:00:00", model="base", source_name="Quick Record",
    )
    legacy = history_manager.get_entry_by_id("legacy")
    assert legacy.cleaned_text is None
    assert entry_version(legacy) == AI_VERSION
    assert ai_text(legacy) == "Cleaned long ago."

    history_manager.use_version("legacy", ORIGINAL_VERSION)
    stored = history_manager.get_entry_by_id("legacy")
    assert (stored.text, stored.cleaned_text) == ("cleaned long ago", "Cleaned long ago.")

    history_manager.use_version("legacy", AI_VERSION)
    assert history_manager.get_entry_by_id("legacy").text == "Cleaned long ago."


def test_choosing_the_version_already_shown_changes_nothing(sync):
    entry = _cleaned()
    assert history_manager.use_version(entry.id, AI_VERSION).text == "Hello, world."
    assert sync.edited == []


def test_only_this_computers_entries_with_an_ai_edit_can_change(db, sync):
    db.add_history_entry(
        entry_id="laptop", text="Theirs.", raw_text="theirs", timestamp="2026-01-01T09:00:00+00:00",
        model="base",
    )
    from services.models import TranscriptionHistory as Model

    with db.get_session() as session:
        session.get(Model, "laptop").origin_device_id = "device-1"
    plain = history_manager.add_entry(text="no cleanup", model="base")

    assert history_manager.use_version("laptop", ORIGINAL_VERSION) is None
    assert history_manager.get_entry_by_id("laptop").text == "Theirs."
    assert history_manager.use_version(plain.id, ORIGINAL_VERSION) is None
    assert history_manager.use_version("missing", ORIGINAL_VERSION) is None
    with pytest.raises(ValueError):
        history_manager.use_version(plain.id, "raw")
    assert sync.edited == []


def test_a_host_kept_entry_is_never_edited(sync):
    from ui_qt.widgets.history_sidebar import remote_history_entry

    remote = remote_history_entry({
        "id": "on-host", "text": "Cleaned.", "raw_text": "cleaned", "timestamp": "2026-01-01T09:00:00+00:00",
        "model": "base", "stored_on": "devbox",
    })
    assert entry_version(remote) == AI_VERSION
    assert history_manager.use_version("on-host", ORIGINAL_VERSION) is None


def test_the_database_only_edits_the_text_fields(db):
    db.add_history_entry(entry_id="e1", text="a", timestamp="2026-01-01T09:00:00+00:00", model="base")
    with pytest.raises(TypeError):
        db.update_history_entry("e1", model="other")
    with pytest.raises(TypeError):
        db.update_history_entry("e1", text=None)
    assert db.update_history_entry("missing", text="b") is None
    assert db.update_history_entry("e1", text="b", cleaned_text="c").text == "b"
    assert db.get_history_entry_by_id("e1").cleaned_text == "c"


def test_export_and_agents_read_the_chosen_text(db, sync, tmp_path):
    from services.agent_api.store import HistoryStore
    from services.history_export import serialize_history_entry

    entry = _cleaned()
    history_manager.use_version(entry.id, ORIGINAL_VERSION)
    stored = history_manager.get_entry_by_id(entry.id)

    payload = serialize_history_entry(stored)
    assert payload["text"] == "hello world um"
    assert payload["raw_text"] == "hello world um"
    assert payload["cleaned_text"] == "Hello, world."

    store = HistoryStore(db.db_path)
    detail = store.transcription(entry.id)
    data = detail.model_dump() if hasattr(detail, "model_dump") else dict(detail)
    assert data["text"] == "hello world um"
    assert "cleaned_text" not in data and "app_name" not in data


def test_search_finds_entries_by_app(sync):
    history_manager.add_entry(text="see you at noon", model="base", app_name="Slack")
    history_manager.add_entry(text="quarterly numbers", model="base", app_name="Outlook")

    assert [e.text for e in history_manager.search_history("slack")] == ["see you at noon"]


def test_last_dictation_skips_files_rewrites_and_other_computers(db, sync):
    db.add_history_entry(entry_id="old", text="old", timestamp="2026-01-01T09:00:00", model="base",
                         source_name="Quick Record · Email")
    db.add_history_entry(entry_id="file", text="file", timestamp="2026-01-02T09:00:00+00:00",
                         model="base", source_name="talk.mp3")
    db.add_history_entry(entry_id="cmd", text="cmd", timestamp="2026-01-03T09:00:00+00:00",
                         model="base", source_name="Quick Record", entry_kind="command")
    db.add_history_entry(entry_id="theirs", text="theirs", timestamp="2026-01-04T09:00:00+00:00",
                         model="base", source_name="Quick Record")
    with db.get_session() as session:
        session.get(TranscriptionHistory, "theirs").origin_device_id = "device-1"

    assert history_manager.last_dictation().id == "old"
    assert kind_of(history_manager.get_entry_by_id("file")) == "file"
    assert kind_of(history_manager.get_entry_by_id("cmd")) == "command"
