"""Agent annotations (notes, tags, external_refs), batch organize_records, history digest, and UI."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services import annotations as record_annotations
from services.agent_mcp.controls import AgentControls, ControlError
from services.database import SCHEMA_VERSION, DatabaseManager
from services.models import MeetingSegment, MeetingSession, RecordAnnotation, TranscriptionHistory
from services.settings import SettingsKey, SettingsManager

T1 = "2026-11-24T10:00:00+00:00"
T2 = "2026-11-25T15:30:00+00:00"


@pytest.fixture
def env(tmp_path, monkeypatch):
    db_path = tmp_path / "annotations.db"
    db = DatabaseManager(str(db_path))
    settings = SettingsManager(str(tmp_path / "settings.json"))
    settings.save_all_settings({})

    with db.get_session() as session:
        session.add_all(
            [
                TranscriptionHistory(
                    id="tx_1",
                    text="We should ship the OpenWhisper MCP skill next week.",
                    raw_text="we should ship the open whisper mcp skill next week",
                    timestamp=T1,
                    model="local_whisper",
                    source_name="Quick Record",
                ),
                TranscriptionHistory(
                    id="tx_2",
                    text="Buy coffee beans and milk after work.",
                    timestamp=T2,
                    model="local_whisper",
                    source_name="Quick Record",
                ),
                TranscriptionHistory(
                    id="tx_remote",
                    text="From paired laptop",
                    timestamp=T2,
                    model="local_whisper",
                    origin_device_id="laptop-1",
                    origin_device_name="Laptop",
                ),
                MeetingSession(
                    id="m_1",
                    title="Weekly Sync",
                    status="ended",
                    started_at=T2,
                    ended_at="2026-11-25T16:00:00+00:00",
                    host_token="h",
                    guest_token="g",
                    spool_dir="spool",
                    state_seq=2,
                    state_json=json.dumps(
                        {
                            "title": "Weekly Sync",
                            "rolling_summary": "Aligned on shipping SEP-2640 skills and notes.",
                        }
                    ),
                ),
            ]
        )
        session.flush()
        session.add(
            MeetingSegment(
                id="seg_1",
                meeting_id="m_1",
                channel="mic",
                start_s=0.0,
                end_s=4.5,
                text="Let's ship the MCP skill.",
                created_at=T2,
            )
        )

    # Create one saved walkthrough under an isolated captures dir.
    wt_root = tmp_path / "walkthroughs"
    wt_folder = wt_root / "2026-11-25_14-00-00"
    wt_folder.mkdir(parents=True)
    (wt_folder / "walkthrough.json").write_text(
        json.dumps(
            {
                "version": 1,
                "number": 1,
                "started_at": "2026-11-25T14:00:00+00:00",
                "duration_s": 12.0,
                "sections": [{"text": "Open settings and toggle MCP.", "shot": None, "notes": []}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("services.walkthrough.storage.captures_root", lambda: wt_root)

    changes: list[tuple[str, dict]] = []
    controls = AgentControls(
        db_path, settings, on_change=lambda kind, data: changes.append((kind, data))
    )
    yield controls, db, settings, changes, wt_folder
    db.close()


def test_v18_database_migrates_to_v19_with_record_annotations(tmp_path):
    db_path = tmp_path / "v18.db"
    with closing(sqlite3.connect(str(db_path))) as conn, conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version(version) VALUES (18)")
    manager = DatabaseManager(str(db_path))
    try:
        with manager.engine.connect() as conn:
            version = conn.exec_driver_sql("SELECT version FROM schema_version").scalar_one()
            tables = {
                row[0]
                for row in conn.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        assert version == SCHEMA_VERSION == 19
        assert "record_annotations" in tables
    finally:
        manager.close()


def test_annotation_permission_gating_and_crud(env):
    controls, db, settings, changes, wt_folder = env

    # Reads work out of the box (empty when unannotated).
    empty = controls.get_annotation("transcription", "tx_1")
    assert empty["exists"] is False
    assert empty["tags"] == []

    # Writes are blocked until MCP_MANAGE_ANNOTATIONS is enabled.
    with pytest.raises(ControlError, match="permission_denied"):
        controls.set_annotation("transcription", "tx_1", {"summary": "MCP skill plan"})

    settings.save_setting(SettingsKey.MCP_MANAGE_ANNOTATIONS, True)
    saved = controls.set_annotation(
        "transcription",
        "tx_1",
        {
            "summary": "Plan to ship OpenWhisper MCP skill.",
            "notes": "- Implement SEP-2640\n- Add unobtrusive notes button",
            "tags": ["#Project:OpenWhisper", "mcp", "MCP"],
            "external_refs": [
                {
                    "system": "obsidian",
                    "id": "Notes/MCP-Skill.md",
                    "label": "MCP Skill Note",
                    "url": "obsidian://open?vault=Work&file=MCP-Skill",
                }
            ],
            "author": "Claude Code",
        },
    )
    assert saved["exists"] is True
    assert saved["tags"] == ["Project:OpenWhisper", "mcp"]
    assert saved["external_refs"][0]["system"] == "obsidian"
    assert changes[-1] == ("annotation", {"record_kind": "transcription", "id": "tx_1"})

    # Merge preserves omitted fields and updates only supplied ones.
    merged = controls.set_annotation(
        "transcription", "tx_1", {"summary": "Updated summary"}, merge=True
    )
    assert merged["summary"] == "Updated summary"
    assert merged["tags"] == ["Project:OpenWhisper", "mcp"]
    assert "SEP-2640" in merged["notes"]

    # Remote records cannot be annotated.
    with pytest.raises(ControlError, match="not_found"):
        controls.set_annotation("transcription", "tx_remote", {"summary": "Nope"})

    # Unsafe external_refs URL schemes (e.g. javascript:) are rejected.
    with pytest.raises(ControlError, match="invalid_value"):
        controls.set_annotation(
            "transcription",
            "tx_1",
            {"external_refs": [{"system": "web", "id": "1", "url": "javascript:alert(1)"}]},
        )

    # Walkthrough annotations respect READ_WALKTHROUGHS and resolve #001 -> folder name.
    with pytest.raises(ControlError, match="permission_denied"):
        controls.set_annotation("walkthrough", "#001", {"summary": "MCP toggle guide"})
    settings.save_setting(SettingsKey.API_READ_WALKTHROUGHS, True)
    wt_ann = controls.set_annotation(
        "walkthrough", "#001", {"summary": "MCP toggle guide", "tags": ["mcp", "guide"]}
    )
    assert wt_ann["record_id"] == wt_folder.name
    assert controls.get_annotation("walkthrough", "latest")["summary"] == "MCP toggle guide"

    # list_tags and list_annotations filter cleanly.
    tags = controls.list_tags()["tags"]
    assert tags[0] == {"tag": "mcp", "count": 2}
    by_system = controls.list_annotations(external_system="obsidian")
    assert [item["record_id"] for item in by_system["items"]] == ["tx_1"]
    by_tag = controls.list_annotations(tag="guide")
    assert [item["record_id"] for item in by_tag["items"]] == [wt_folder.name]

    # History sidebar search also finds transcriptions by annotation tags/notes.
    found = db.search_history_entries("Project:OpenWhisper")
    assert [row.id for row in found] == ["tx_1"]

    # Clearing annotation leaves the transcription intact.
    deleted = controls.delete_annotation("transcription", "tx_1")
    assert deleted["deleted"] is True
    assert controls.get_annotation("transcription", "tx_1")["exists"] is False
    assert db.get_history_entry_by_id("tx_1").text.startswith("We should ship")


def test_organize_records_and_history_digest(env):
    controls, db, settings, _changes, wt_folder = env
    settings.update_settings(
        {
            SettingsKey.MCP_RETITLE_TRANSCRIPTIONS: True,
            SettingsKey.MCP_RETITLE_MEETINGS: True,
            SettingsKey.MCP_MANAGE_ANNOTATIONS: True,
            SettingsKey.API_READ_WALKTHROUGHS: True,
        }
    )

    # Dry-run validates without mutating anything.
    preview = controls.organize_records(
        [
            {
                "record_kind": "transcription",
                "record_id": "tx_1",
                "title": "MCP Skill Roadmap",
                "summary": "Ship skill + notes",
                "tags": ["work", "mcp"],
            },
            {
                "record_kind": "meeting",
                "record_id": "m_1",
                "title": "MCP Skill Alignment Sync",
                "tags": ["work"],
            },
        ],
        dry_run=True,
    )
    assert preview["dry_run"] is True
    assert preview["count"] == 2
    assert db.get_history_entry_by_id("tx_1").title is None

    # Real run commits both titles and annotations atomically.
    applied = controls.organize_records(
        [
            {
                "record_kind": "transcription",
                "record_id": "tx_1",
                "title": "MCP Skill Roadmap",
                "summary": "Ship skill + notes",
                "tags": ["work", "mcp"],
                "external_refs": [{"system": "agent-db", "id": "item-99"}],
            },
            {
                "record_kind": "meeting",
                "record_id": "m_1",
                "title": "MCP Skill Alignment Sync",
                "tags": ["work"],
            },
        ],
        dry_run=False,
    )
    assert applied["dry_run"] is False
    assert db.get_history_entry_by_id("tx_1").title == "MCP Skill Roadmap"
    with db.get_session() as session:
        assert session.get(MeetingSession, "m_1").title == "MCP Skill Alignment Sync"

    # Full history digest returns transcriptions, meetings, and walkthroughs with annotations.
    digest = controls.history_digest(limit=20)
    assert digest["walkthroughs_included"] is True
    ids = {(item["record_kind"], item["record_id"]) for item in digest["items"]}
    assert ("transcription", "tx_1") in ids
    assert ("transcription", "tx_2") in ids
    assert ("meeting", "m_1") in ids
    assert ("walkthrough", wt_folder.name) in ids
    assert ("transcription", "tx_remote") not in ids

    tx1_item = next(i for i in digest["items"] if i["record_id"] == "tx_1")
    assert tx1_item["annotation"]["tags"] == ["work", "mcp"]
    m1_item = next(i for i in digest["items"] if i["record_id"] == "m_1")
    assert m1_item["segment_count"] == 1
    assert "SEP-2640" in m1_item["rolling_summary"]

    # only_unannotated=True skips already-annotated records for fast incremental sync.
    unannotated = controls.history_digest(only_unannotated=True, limit=20)
    unannotated_ids = {(item["record_kind"], item["record_id"]) for item in unannotated["items"]}
    assert ("transcription", "tx_1") not in unannotated_ids
    assert ("meeting", "m_1") not in unannotated_ids
    assert ("transcription", "tx_2") in unannotated_ids
    assert ("walkthrough", wt_folder.name) in unannotated_ids


def test_record_notes_dialog_saves_and_history_entry_dialog_shows_button(tmp_path, monkeypatch):
    from PyQt6.QtWidgets import QApplication

    from ui_qt.dialogs.history_entry_dialog import HistoryEntryDialog
    from ui_qt.dialogs.record_notes_dialog import RecordNotesDialog

    app = QApplication.instance() or QApplication([])
    db = DatabaseManager(str(tmp_path / "ui.db"))
    monkeypatch.setattr("services.database.db", db)
    try:
        db.add_history_entry(
            entry_id="ui_tx_1",
            text="Transcript body stays untouched.",
            timestamp=T2,
            model="local_whisper",
        )
        entry = db.get_history_entry_by_id("ui_tx_1")

        # Save notes via RecordNotesDialog.
        notes_dialog = RecordNotesDialog("transcription", "ui_tx_1", record_title="Test Entry")
        notes_dialog.summary_edit.setText("Concise summary")
        notes_dialog.tags_edit.setText("mcp, notes")
        notes_dialog.notes_edit.setPlainText("Bullet one\nBullet two")
        notes_dialog._on_save()
        assert notes_dialog.changed is True
        notes_dialog.deleteLater()

        saved = record_annotations.get_annotation("transcription", "ui_tx_1")
        assert saved["exists"] is True
        assert saved["tags"] == ["mcp", "notes"]

        # HistoryEntryDialog shows the Agent Notes button reflecting the saved tag count.
        entry_dialog = HistoryEntryDialog(entry)
        try:
            assert "Agent Notes" in entry_dialog.notes_button.text()
            assert "(2)" in entry_dialog.notes_button.text()
            assert not entry_dialog.notes_button.isHidden()
        finally:
            entry_dialog.close()
            entry_dialog.deleteLater()
            app.processEvents()
    finally:
        db.close()
