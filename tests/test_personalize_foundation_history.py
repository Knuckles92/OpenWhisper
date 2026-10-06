"""History schema v17: per-entry context columns and the dictation_stats table."""

import sqlite3
from contextlib import closing

import pytest
from sqlalchemy import inspect

from services.models import HISTORY_CONTEXT_COLUMNS, DictationStat, TranscriptionHistory

CONTEXT = {
    "app_id": "outlook.exe",
    "app_name": "Outlook",
    "app_category": "email",
    "cleanup_level": "medium",
    "entry_kind": "dictation",
    "cleaned_text": "Hello, world.",
    "language": "en",
}


def _v16_database(path):
    """A database as schema v16 left it, holding one entry."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version(version) VALUES (16)")
        conn.execute(
            """
            CREATE TABLE transcription_history (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                raw_text TEXT,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
                audio_file TEXT,
                transcription_time REAL,
                audio_duration REAL,
                file_size INTEGER,
                cleanup_provider TEXT,
                cleanup_model TEXT,
                source_name TEXT,
                title TEXT,
                origin_device_id TEXT,
                origin_device_name TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO transcription_history (id, text, timestamp, model, source_name) "
            "VALUES ('old', 'kept text', '2026-10-01T09:00:00+00:00', 'base', 'Quick Record')"
        )
        conn.commit()
    finally:
        conn.close()


def test_v16_database_gains_the_context_columns_and_stats_table(tmp_path):
    db_path = str(tmp_path / "legacy_v16.db")
    _v16_database(db_path)

    from services.database import SCHEMA_VERSION, DatabaseManager

    assert SCHEMA_VERSION == 17
    manager = DatabaseManager(db_path=db_path)
    try:
        with manager.engine.connect() as connection:
            columns = {
                row[1]
                for row in connection.exec_driver_sql(
                    "PRAGMA table_info(transcription_history)"
                )
            }
            version = connection.exec_driver_sql(
                "SELECT version FROM schema_version"
            ).scalar_one()
        assert set(HISTORY_CONTEXT_COLUMNS) <= columns
        assert version == SCHEMA_VERSION
        assert "dictation_stats" in inspect(manager.engine).get_table_names()

        legacy = manager.get_history_entry_by_id("old")
        assert legacy.text == "kept text"
        # Legacy rows derive their kind from source_name.
        assert all(getattr(legacy, column) is None for column in HISTORY_CONTEXT_COLUMNS)
    finally:
        manager.close()

    # Running the migration again finds every column already there.
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE schema_version SET version = 16")
    manager = DatabaseManager(db_path=db_path)
    try:
        assert manager.get_history_entry_by_id("old").text == "kept text"
    finally:
        manager.close()


def test_add_history_entry_stores_context_and_leaves_unset_ones_null(db):
    db.add_history_entry(
        entry_id="e1", text="Hello, world.", timestamp="2026-10-06T10:00:00+00:00",
        model="base", **CONTEXT,
    )
    db.add_history_entry(
        entry_id="e2", text="plain", timestamp="2026-10-06T10:01:00+00:00",
        model="base", app_name=None,
    )

    stored = db.get_history_entry_by_id("e1")
    assert {column: getattr(stored, column) for column in HISTORY_CONTEXT_COLUMNS} == CONTEXT
    plain = db.get_history_entry_by_id("e2")
    assert all(getattr(plain, column) is None for column in HISTORY_CONTEXT_COLUMNS)


def test_only_known_context_columns_reach_the_model():
    entry = TranscriptionHistory.create(text="hi", model="base", entry_kind="command")
    assert entry.entry_kind == "command"
    with pytest.raises(TypeError, match="window_title"):
        TranscriptionHistory.create(text="hi", model="base", window_title="Inbox")


def test_history_manager_passes_context_through_and_drops_the_rest():
    from services.history_manager import history_manager

    entry = history_manager.add_entry(
        text="Hello, world.",
        model="base",
        window_title="Inbox — never stored",
        app_category=None,
        **{key: value for key, value in CONTEXT.items() if key != "app_category"},
    )

    stored = history_manager.get_entry_by_id(entry.id)
    assert stored.app_name == "Outlook"
    assert stored.entry_kind == "dictation"
    assert stored.app_category is None
    assert not hasattr(stored, "window_title")


def test_dictation_stats_rows_round_trip(db):
    with db.get_session() as session:
        session.add(DictationStat(
            entry_id="e1", timestamp="2026-10-06T10:00:00+00:00", entry_kind="dictation",
            app_name="Outlook", app_category="email", cleanup_level="medium",
            spoken_words=12, final_words=11, words_edited=2, audio_seconds=4.5,
            language="en",
        ))
    with db.get_session() as session:
        row = session.query(DictationStat).one()
        assert (row.id, row.spoken_words, row.final_words, row.words_edited) == (1, 12, 11, 2)
        assert row.audio_seconds == 4.5


def test_history_export_carries_the_context_columns():
    from services.history_export import serialize_history_entry

    entry = TranscriptionHistory.create(text="Hello, world.", model="base", **CONTEXT)
    payload = serialize_history_entry(entry)

    assert {column: payload[column] for column in HISTORY_CONTEXT_COLUMNS} == CONTEXT
