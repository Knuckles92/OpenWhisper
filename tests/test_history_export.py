"""Tests for bulk transcription-history export assembly."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from services.history_export import (
    FORMAT_JSON,
    FORMAT_MARKDOWN,
    FORMAT_TXT,
    FORMAT_VERSION,
    entry_file_stem,
    filter_export_entries,
    list_export_entries,
    render_entry_document,
    render_export_document,
    serialize_history_entry,
    write_per_entry_files,
)


def _entry(
    entry_id="h_1",
    *,
    timestamp="2026-03-15T14:30:00+00:00",
    model="local_whisper (turbo | cuda (float16))",
    text="Hello from history",
    raw_text=None,
    cleanup_provider=None,
    cleanup_model=None,
    audio_file=None,
    transcription_time=1.2,
    audio_duration=3.4,
    file_size=2048,
    source_name=None,
):
    return SimpleNamespace(
        id=entry_id,
        timestamp=timestamp,
        model=model,
        text=text,
        raw_text=raw_text,
        cleanup_provider=cleanup_provider,
        cleanup_model=cleanup_model,
        audio_file=audio_file,
        transcription_time=transcription_time,
        audio_duration=audio_duration,
        file_size=file_size,
        source_name=source_name,
    )


def test_serialize_history_entry_adds_display_fields():
    payload = serialize_history_entry(
        _entry(audio_file="recording_20260315_143000.wav", text="")
    )
    assert payload["id"] == "h_1"
    assert payload["has_audio"] is True
    assert payload["preview_text"] == "Empty transcript"
    assert payload["formatted_timestamp"]


def test_list_export_entries_uses_manager_order():
    class Manager:
        def get_history(self):
            return [_entry("h_new"), _entry("h_old")]

    rows = list_export_entries(Manager())
    assert [row["id"] for row in rows] == ["h_new", "h_old"]


def test_filter_export_entries_applies_date_and_audio():
    rows = [
        serialize_history_entry(_entry("h_old", timestamp="2026-01-01T10:00:00+00:00")),
        serialize_history_entry(_entry("h_mid", timestamp="2026-03-15T14:30:00+00:00")),
        serialize_history_entry(_entry("h_new", timestamp="2026-06-01T09:00:00+00:00")),
        serialize_history_entry(
            _entry(
                "h_audio",
                timestamp="2026-03-20T09:00:00+00:00",
                audio_file="recording.wav",
            )
        ),
        serialize_history_entry(_entry("h_undated", timestamp="")),
    ]
    filtered = filter_export_entries(
        rows,
        from_dt=datetime(2026, 3, 1),
        to_dt=datetime(2026, 3, 31, 23, 59, 59),
    )
    assert [row["id"] for row in filtered] == ["h_mid", "h_audio"]

    audio_only = filter_export_entries(rows, only_with_audio=True)
    assert [row["id"] for row in audio_only] == ["h_audio"]

    undated_dropped = filter_export_entries(
        rows, from_dt=datetime(2020, 1, 1)
    )
    assert "h_undated" not in [row["id"] for row in undated_dropped]


def test_markdown_toggles_cleaned_and_raw():
    entry = serialize_history_entry(
        _entry(
            text="Fixed hello",
            raw_text="um hello",
            cleanup_provider="openai",
            cleanup_model="gpt-4o",
        )
    )
    full = render_entry_document(entry, FORMAT_MARKDOWN)
    assert "Fixed hello" in full
    assert "### Raw" in full
    assert "um hello" in full
    assert "Cleanup: openai · gpt-4o" in full

    cleaned_only = render_entry_document(
        entry, FORMAT_MARKDOWN, include_cleaned=True, include_raw=False
    )
    assert "Fixed hello" in cleaned_only
    assert "### Raw" not in cleaned_only

    raw_only = render_entry_document(
        entry, FORMAT_MARKDOWN, include_cleaned=False, include_raw=True
    )
    assert "Fixed hello" not in raw_only
    assert "um hello" in raw_only


def test_txt_and_json_ignore_markdown_toggles():
    entry = serialize_history_entry(
        _entry(text="Fixed", raw_text="Raw", cleanup_model="gpt-4o")
    )
    txt = render_entry_document(
        entry, FORMAT_TXT, include_cleaned=False, include_raw=False
    )
    assert "Fixed" in txt
    assert "Raw:" in txt
    payload = json.loads(
        render_entry_document(
            entry, FORMAT_JSON, include_cleaned=False, include_raw=False
        )
    )
    assert payload["text"] == "Fixed"
    assert payload["raw_text"] == "Raw"
    assert "preview_text" not in payload


def test_combined_documents_wrap_entries():
    entries = [
        serialize_history_entry(_entry("h_a", text="Alpha")),
        serialize_history_entry(_entry("h_b", text="Beta")),
    ]
    markdown = render_export_document(entries, FORMAT_MARKDOWN)
    assert markdown.startswith("# Transcription History Export")
    assert "Alpha" in markdown
    assert "Beta" in markdown

    txt = render_export_document(entries, FORMAT_TXT)
    assert "Alpha" in txt
    assert "-" * 60 in txt

    envelope = json.loads(render_export_document(entries, FORMAT_JSON))
    assert envelope["format_version"] == FORMAT_VERSION
    assert [row["id"] for row in envelope["entries"]] == ["h_a", "h_b"]


def test_write_per_entry_files_disambiguates_stems(tmp_path):
    stamp = datetime(2026, 3, 15, 14, 30, tzinfo=timezone.utc).isoformat()
    entries = [
        serialize_history_entry(_entry("h_a", timestamp=stamp, text="Same title")),
        serialize_history_entry(_entry("h_b", timestamp=stamp, text="Same title")),
    ]
    written = write_per_entry_files(entries, FORMAT_TXT, str(tmp_path))
    names = [path.split(tmp_path.as_posix())[-1].lstrip("/\\") for path in written]
    assert len(set(names)) == 2
    assert all(name.endswith(".txt") for name in names)
    assert (tmp_path / names[0]).read_text(encoding="utf-8")


def test_write_per_entry_files_never_overwrites_prior_export(tmp_path):
    entry = serialize_history_entry(_entry(text="Existing title"))
    original_name = f"{entry_file_stem(entry)}.txt"
    original = tmp_path / original_name
    original.write_text("keep me", encoding="utf-8")

    written = write_per_entry_files([entry], FORMAT_TXT, str(tmp_path))

    assert original.read_text(encoding="utf-8") == "keep me"
    assert written == [str(tmp_path / original_name.replace(".txt", "-2.txt"))]


def test_entry_file_stem_uses_local_stamp_and_preview():
    entry = serialize_history_entry(
        _entry(timestamp="2026-03-15T14:30:00+00:00", text="Hello from history")
    )
    stem = entry_file_stem(entry)
    assert "Hello-from-history" in stem
    assert stem.endswith("Hello-from-history")
