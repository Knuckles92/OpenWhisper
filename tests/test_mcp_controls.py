"""Permission transactions, title preservation, and safe database migration."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from services.agent_mcp.controls import AgentControls, ControlError
from services.database import SCHEMA_VERSION, DatabaseManager
from services.models import MeetingSession, TranscriptionHistory
from services.settings import SettingsKey, SettingsManager


@pytest.fixture
def controls(tmp_path):
    path = tmp_path / "history.db"
    db = DatabaseManager(str(path))
    settings = SettingsManager(str(tmp_path / "settings.json"))
    settings.update_settings(
        {
            SettingsKey.MCP_RETITLE_TRANSCRIPTIONS: True,
            SettingsKey.MCP_RETITLE_MEETINGS: True,
            SettingsKey.MCP_SETTINGS_ACCESS: True,
            SettingsKey.MCP_WRITABLE_SETTINGS: {SettingsKey.AUTO_PASTE: True},
        }
    )
    with db.get_session() as session:
        session.add(
            TranscriptionHistory(
                id="h",
                text="Original",
                raw_text="Raw",
                source_name="original.wav",
                model="test",
                timestamp="today",
            )
        )
        session.add_all(
            MeetingSession(
                id=f"m_{status}",
                title="Original",
                status=status,
                started_at="today",
                host_token="secret",
                guest_token="secret",
                spool_dir="test",
                state_seq=5,
                state_json=json.dumps(
                    {"title": "Original", "seq": 5, "rolling_summary": "Summary"}
                ),
            )
            for status in ("ended", "active", "paused", "needs_recovery")
        )
    yield AgentControls(path, settings), db, settings
    db.close()


def test_retitles_preserve_source_text_and_saved_insights(controls):
    agent, db, _ = controls
    agent.retitle("transcription", "h", "New title")
    agent.retitle("meeting", "m_ended", "New meeting title")
    with db.get_session() as session:
        row = session.get(TranscriptionHistory, "h")
        assert (row.title, row.source_name, row.text, row.raw_text) == (
            "New title",
            "original.wav",
            "Original",
            "Raw",
        )
        meeting = session.get(MeetingSession, "m_ended")
        assert meeting.title == "New meeting title"
        state = json.loads(meeting.state_json)
        assert state == {
            "title": "New meeting title",
            "seq": 5,
            "rolling_summary": "Summary",
        }


@pytest.mark.parametrize("status", ["active", "paused", "needs_recovery"])
def test_unfinished_meetings_cannot_be_retitled(controls, status):
    agent, _, _ = controls
    with pytest.raises(ControlError, match="record_busy"):
        agent.retitle("meeting", f"m_{status}", "Blocked")


@pytest.mark.parametrize("desktop_bound", [False, True])
def test_remote_meetings_are_not_writable_and_bad_snapshots_are_preserved(
    controls, desktop_bound
):
    agent, db, _ = controls
    calls = []
    if desktop_bound:
        agent.meeting_renamer = lambda *args: calls.append(args)
    with db.get_session() as session:
        session.get(MeetingSession, "m_ended").origin_device_id = "paired"
    with pytest.raises(ControlError, match="not_found"):
        agent.retitle("meeting", "m_ended", "Blocked")
    with db.get_session() as session:
        row = session.get(MeetingSession, "m_ended")
        row.origin_device_id = None
        row.state_json = "broken"
    with pytest.raises(ControlError, match="snapshot_unavailable"):
        agent.retitle("meeting", "m_ended", "Blocked")
    with db.get_session() as session:
        row = session.get(MeetingSession, "m_ended")
        assert row.title == "Original" and row.state_json == "broken"
    assert calls == []


def test_permission_check_is_inside_settings_transaction(controls, monkeypatch):
    agent, _, settings = controls
    original = settings.mutate_settings

    # Use original directly to avoid recursively calling the patched method.
    def revoke_then_commit(mutator):
        original(lambda saved: saved.update({SettingsKey.MCP_SETTINGS_ACCESS: False}))
        return original(mutator)

    monkeypatch.setattr(settings, "mutate_settings", revoke_then_commit)
    with pytest.raises(ControlError, match="permission_denied"):
        agent.update_settings({SettingsKey.AUTO_PASTE: False})
    assert settings.get(SettingsKey.AUTO_PASTE) is None


def test_concurrent_updates_preserve_other_preferences(controls):
    agent, _, settings = controls
    settings.save_setting(
        SettingsKey.MCP_WRITABLE_SETTINGS,
        {
            SettingsKey.AUTO_PASTE: True,
            SettingsKey.COPY_CLIPBOARD: True,
        },
    )
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(
                agent.update_settings,
                [
                    {SettingsKey.AUTO_PASTE: False},
                    {SettingsKey.COPY_CLIPBOARD: False},
                ],
            )
        )
    assert len(results) == 2
    assert settings.get(SettingsKey.AUTO_PASTE) is False
    assert settings.get(SettingsKey.COPY_CLIPBOARD) is False


def test_missing_database_is_not_created(controls, tmp_path):
    _, _, settings = controls
    missing = tmp_path / "missing.db"
    agent = AgentControls(missing, settings)
    with pytest.raises(OperationalError):
        agent.retitle("transcription", "h", "Title")
    assert not missing.exists()


def test_schema_14_migration_preserves_history(tmp_path):
    path = tmp_path / "old.db"
    db = DatabaseManager(str(path))
    with db.get_session() as session:
        session.add(
            TranscriptionHistory(
                id="old", text="Keep this", model="test", timestamp="today"
            )
        )
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE transcription_history DROP COLUMN title"))
        conn.execute(text("UPDATE schema_version SET version=14"))
    db.close()
    db = DatabaseManager(str(path))
    try:
        with db.get_session() as session:
            row = session.get(TranscriptionHistory, "old")
            assert row.text == "Keep this" and row.title is None
        with db.engine.connect() as conn:
            assert (
                conn.execute(text("SELECT version FROM schema_version")).scalar()
                == SCHEMA_VERSION
            )
    finally:
        db.close()


def test_dashboard_retitling_updates_cached_state_and_survives_later_edits(
    controls, tmp_path
):
    from meeting.persist.repository import SqlMeetingRepository
    from meeting.web.archive import ArchivedMeetingDashboard
    from meeting.web.server import MeetingWebServer

    agent, db, _ = controls
    repo = SqlMeetingRepository(db=db)
    archive = ArchivedMeetingDashboard(
        repo, repo.get_meeting("m_ended"), spool_root=str(tmp_path)
    )
    server = MeetingWebServer(archive, repo, port=0)
    server.start()
    archive.attach_server(server)
    try:
        agent.meeting_renamer = lambda record_id, title: server.retitle_saved_meeting(
            record_id, title
        )
        agent.retitle("meeting", "m_ended", "Updated through MCP")
        assert archive.store.snapshot()["title"] == "Updated through MCP"
        archive.store.apply("host", None, [{"op": "set_title", "text": "User edit"}])
        assert repo.get_meeting("m_ended")["title"] == "User edit"
    finally:
        server.stop()


def test_settings_save_failure_preserves_existing_values(controls, monkeypatch):
    agent, _, settings = controls
    settings.save_setting(SettingsKey.AUTO_PASTE, True)

    def fail(saved):
        raise OSError("cannot save")

    monkeypatch.setattr(settings, "_save_all_settings_unlocked", fail)
    with pytest.raises(OSError):
        agent.update_settings({SettingsKey.AUTO_PASTE: False})
    assert settings.get(SettingsKey.AUTO_PASTE) is True


def test_malformed_grants_do_not_authorize_writes(controls):
    agent, _, settings = controls
    settings.update_settings(
        {
            SettingsKey.MCP_RETITLE_TRANSCRIPTIONS: "true",
            SettingsKey.MCP_WRITABLE_SETTINGS: {SettingsKey.AUTO_PASTE: "true"},
        }
    )
    with pytest.raises(ControlError, match="permission_denied"):
        agent.retitle("transcription", "h", "Denied")
    with pytest.raises(ControlError, match="permission_denied"):
        agent.update_settings({SettingsKey.AUTO_PASTE: False})
