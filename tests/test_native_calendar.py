"""The native calendar uses the real local-day index and saved-record services."""
from concurrent.futures import Future
from datetime import date, timedelta, timezone
from types import SimpleNamespace

import pytest

from meeting.persist.repository import SqlMeetingRepository
from services.native_desktop.commands import dispatch
from tests.test_native_commands import context as context


class Records:
    def __init__(self, items=None):
        self.items = items or {}
        self.deleted = []
        self.queries = []

    def listing_wanted(self, kind):
        return kind in self.items

    def list_remote(self, kind, query="", limit=500):
        self.queries.append((kind, query, limit))
        return [dict(item, stored_on="Office host") for item in self.items.get(kind, [])[:limit]]

    def copies_on_host(self, kind, ids):
        return set()

    def remote_item(self, kind, item_id):
        return next((dict(item, stored_on="Office host") for item in self.items.get(kind, []) if item["id"] == item_id), None)

    def status(self):
        return SimpleNamespace(host_name="Office host")

    def delete_remote(self, kind, item_id):
        self.deleted.append((kind, item_id))
        return True


@pytest.fixture
def calendar_context(context):
    context.meetings = SqlMeetingRepository(context.database)
    context.record_sync = Records()
    context.calendar_timezone = timezone(timedelta(hours=-7))
    return context


def add(context, record_id, timestamp, text="Words to remember", **fields):
    context.database.add_history_entry(record_id, text, timestamp, "local", **fields)


def test_calendar_places_utc_records_on_local_days_and_merges_meetings(calendar_context):
    c = calendar_context
    add(c, "late-october", "2026-11-01T01:30:00Z", source_name="Quick Record", entry_kind="dictation")
    add(c, "file", "2026-11-02T11:00:00", source_name="interview.wav", entry_kind="file", audio_duration=60)
    c.meetings.create_meeting(id="meeting", title="Planning", status="ended", started_at="2026-11-02T12:00:00",
        ended_at="2026-11-02T12:10:00", spool_dir="", host_token="PRIVATE_HOST_CAPABILITY", guest_token="PRIVATE_GUEST_CAPABILITY")
    october = dispatch(c, "history.calendar.get", {"year": 2026, "month": 10, "day": "2026-10-31"})
    assert [item["id"] for item in october["items"]] == ["late-october"]
    assert october["stats"]["count"] == 1
    november = dispatch(c, "history.calendar.get", {"year": 2026, "month": 11, "day": "2026-11-02"})
    assert [item["kind"] for item in november["items"]] == ["file", "meeting"]
    assert november["stats"]["count"] == 2 and november["stats"]["seconds"] == 660
    assert november["stats"]["busiest"] == {"day": "2026-11-02", "count": 2}
    assert "PRIVATE_HOST_CAPABILITY" not in str(november)
    assert len(november["weeks"][0]) == 7
    assert november["months"] == [{"year": 2026, "month": 10, "count": 1}, {"year": 2026, "month": 11, "count": 2}]
    filtered = dispatch(c, "history.calendar.get", {"year": 2026, "month": 11, "day": "2026-11-02", "kinds": ["meeting"]})
    assert filtered["stats"]["count"] == 1 and filtered["items"][0]["id"] == "meeting"
    disconnected = dispatch(c, "history.calendar.get", {"year": 2026, "month": 11, "place": ["on", "Disconnected host"]})
    assert disconnected["place"] is None and disconnected["stats"]["count"] == 2
    today = dispatch(c, "history.calendar.get", {"today": True, "year": 2025, "month": 1, "day": "2025-01-01"})
    assert today["selected"] == date.today().isoformat()


def test_calendar_bounds_filters_and_dense_day_pagination(calendar_context):
    c = calendar_context
    for number in range(7):
        add(c, f"entry-{number}", "2026-11-02T10:00:00", entry_kind="dictation", source_name="Quick Record")
    first = dispatch(c, "history.calendar.get", {"year": 2026, "month": 11, "day": "2026-11-02", "limit": 3})
    second = dispatch(c, "history.calendar.get", {"year": 2026, "month": 11, "day": "2026-11-02", "limit": 3, "offset": 3})
    assert first["total"] == 7 and first["has_more"] and len(second["items"]) == 3
    assert {item["id"] for item in first["items"]}.isdisjoint(item["id"] for item in second["items"])
    for params in ({"year": 0}, {"month": 13}, {"kinds": []}, {"place": ["unknown", "host"]},
                   {"year": 2026, "month": 11, "day": "2026-10-31"}):
        with pytest.raises(ValueError):
            dispatch(c, "history.calendar.get", params)


def test_paired_host_listing_is_async_and_only_latest_calendar_query_is_published(calendar_context):
    c = calendar_context
    c.record_sync = Records({"dictation": [{"id": "remote", "timestamp": "2026-11-02T10:00:00",
        "text": "Kept on another computer", "entry_kind": "dictation", "source_name": "Quick Record"}]})
    future = Future()
    c.controller.executor.submit.return_value = future
    first = dispatch(c, "history.calendar.get", {"year": 2026, "month": 11, "day": "2026-11-02"})
    second = dispatch(c, "history.calendar.get", {"year": 2026, "month": 11, "day": "2026-11-02", "kinds": ["dictation"]})
    assert first["remote_loading"] and second["remote_loading"]
    c.controller.executor.submit.assert_called_once()
    future.set_result(c.controller.executor.submit.call_args.args[0]())
    published = [call.args[1] for call in c.emit.call_args_list if call.args[0] == "history.calendar.loaded"]
    assert len(published) == 1 and published[0]["request_id"] == second["request_id"]
    assert published[0]["items"][0]["location"] == "On Office host"
    cached = dispatch(c, "history.calendar.get", {"year": 2026, "month": 11, "day": "2026-11-02"})
    assert not cached["remote_loading"] and cached["total"] == 1


def test_host_only_transcripts_can_open_export_and_delete_without_unsafe_local_edits(calendar_context):
    c = calendar_context
    c.record_sync = Records({"dictation": [{"id": "remote", "timestamp": "2026-11-02T10:00:00",
        "text": "Complete host transcript", "raw_text": "Original host words", "has_audio": True,
        "entry_kind": "dictation", "source_name": "Quick Record"}]})
    opened = dispatch(c, "history.calendar.open", {"id": "remote", "kind": "dictation", "request_id": "clicked-1"})
    assert opened["request_id"] == "clicked-1" and opened["item"]["text"] == "Complete host transcript"
    assert not opened["item"]["editable"] and opened["item"]["can_delete"]
    assert opened["item"]["audio_available"] and opened["item"]["playback_path"] is None
    assert "Complete host transcript" in dispatch(c, "history.export", {"id": "remote"})["text"]
    with pytest.raises(ValueError, match="Edit this entry"):
        dispatch(c, "history.update", {"id": "remote", "text": "changed"})
    assert not c.record_sync.deleted
    assert dispatch(c, "history.delete", {"id": "remote"})["ok"]
    assert c.record_sync.deleted == [("dictation", "remote")]


def test_retained_audio_retranscription_uses_validated_recording_and_busy_guard(calendar_context):
    c = calendar_context
    audio = c.history.recordings_folder
    from pathlib import Path
    Path(audio).mkdir(exist_ok=True)
    (Path(audio) / "recording.wav").write_bytes(b"RIFF")
    add(c, "audio", "2026-11-02T10:00:00", audio_file="recording.wav")
    assert dispatch(c, "history.retranscribe", {"id": "audio"})["ok"]
    c.controller.retranscribe_audio.assert_called_once_with(str((Path(audio) / "recording.wav").resolve()))
    c.controller.recorder.is_recording = True
    with pytest.raises(ValueError, match="Finish"):
        dispatch(c, "history.retranscribe", {"id": "audio"})
    c.controller.retranscribe_audio.assert_called_once()


def test_host_recording_fetch_is_async_and_retranscription_uses_its_service_cache(calendar_context, tmp_path):
    c = calendar_context
    c.record_sync = Records({"dictation": [{"id": "remote", "text": "Words", "timestamp": "2026-11-02T10:00:00", "has_audio": True}]})
    cached = tmp_path / "fetched-recording.wav"
    cached.write_bytes(b"RIFF")
    c.record_sync.audio_for = lambda item_id: str(cached)
    future = Future()
    c.controller.executor.submit.return_value = future
    assert dispatch(c, "history.audio.get", {"id": "remote"})["loading"]
    work = c.controller.executor.submit.call_args.args
    future.set_result(work[0](*work[1:]))
    assert c.emit.call_args.args == ("history.audio.ready", {"id": "remote", "path": str(cached.resolve())})
    assert dispatch(c, "history.playback", {"id": "remote"})["path"] == str(cached.resolve())
    assert dispatch(c, "history.detail", {"id": "remote"})["playback_path"] == str(cached.resolve())
    assert dispatch(c, "history.retranscribe", {"id": "remote"})["ok"]
    c.controller.retranscribe_audio.assert_called_once_with(str(cached.resolve()))


def test_host_meeting_open_fetches_once_and_returns_safe_real_details(calendar_context):
    c = calendar_context
    c.record_sync = Records({"meeting": [{"id": "remote-meeting", "started_at": "2026-11-02T10:00:00", "title": "Host planning"}]})
    def check_out(kind, item_id):
        c.meetings.create_meeting(id=item_id, title="Host planning", status="ended", started_at="2026-11-02T10:00:00",
            spool_dir="", host_token="PRIVATE_HOST_CAPABILITY", guest_token="PRIVATE_GUEST_CAPABILITY")
    c.record_sync.check_out = check_out
    future = Future()
    c.controller.executor.submit.return_value = future
    assert dispatch(c, "history.calendar.open", {"id": "remote-meeting", "kind": "meeting", "request_id": "one"})["loading"]
    assert dispatch(c, "history.calendar.open", {"id": "remote-meeting", "kind": "meeting", "request_id": "two"})["loading"]
    c.controller.executor.submit.assert_called_once()
    future.set_result(c.controller.executor.submit.call_args.args[0]())
    event, payload = c.emit.call_args.args
    assert event == "history.calendar.opened" and payload["request_id"] == "two"
    assert payload["item"]["title"] == "Host planning"
    assert "PRIVATE_HOST_CAPABILITY" not in str(payload)


def test_original_and_ai_versions_are_real_persisted_history_choices(calendar_context):
    c = calendar_context
    add(c, "cleaned", "2026-11-02T10:00:00", text="AI cleaned words", raw_text="Original spoken words")
    original = dispatch(c, "history.update", {"id": "cleaned", "version": "original"})["item"]
    assert original["text"] == "Original spoken words" and original["version"] == "original"
    cleaned = dispatch(c, "history.update", {"id": "cleaned", "version": "ai"})["item"]
    assert cleaned["text"] == "AI cleaned words" and cleaned["version"] == "ai"
    assert dispatch(c, "history.list")["total"] == 1


def test_host_full_text_search_is_async_paginated_deduplicated_and_gates_stale_results(calendar_context):
    c = calendar_context
    rows = [{"id": f"host-{number}", "text": "A long introduction. " * 30 + "needle beyond preview",
             "timestamp": "2026-11-02T10:00:00", "entry_kind": "dictation", "source_name": "Quick Record"}
            for number in range(9)]
    c.record_sync = Records({"dictation": rows})
    add(c, "host-0", "2026-11-02T10:00:00")
    older, newer = Future(), Future()
    c.controller.executor.submit.side_effect = [older, newer]
    first = dispatch(c, "history.remote.get", {"query": "old words"})
    second = dispatch(c, "history.remote.get", {"query": "needle beyond preview", "offset": 3, "limit": 3})
    assert first["loading"] and second["loading"]
    work = c.controller.executor.submit.call_args_list
    newer.set_result(work[1].args[0]())
    older.set_result(work[0].args[0]())
    events = [call.args[1] for call in c.emit.call_args_list if call.args[0] == "history.remote.loaded"]
    assert len(events) == 1 and events[0]["request_id"] == second["request_id"]
    assert events[0]["query"] == "needle beyond preview"
    assert [item["id"] for item in events[0]["items"]] == ["host-4", "host-5", "host-6"]
    assert events[0]["total"] == 8 and events[0]["has_more"] and events[0]["host"] == "Office host"
    assert all(len(item["text"]) <= 220 for item in events[0]["items"])
    assert ("dictation", "needle beyond preview", 500) in c.record_sync.queries


def test_unreachable_host_search_reports_its_error_without_changing_local_history(calendar_context):
    from services.remote_records.sync import RecordsUnavailable
    c = calendar_context
    add(c, "local", "2026-11-02T10:00:00")
    c.record_sync = Records({"dictation": []})
    c.record_sync.list_remote = lambda *_args: (_ for _ in ()).throw(RecordsUnavailable("Office host isn't reachable right now."))
    future = Future()
    c.controller.executor.submit.return_value = future
    assert dispatch(c, "history.remote.get", {"query": "find"})["loading"]
    try:
        c.controller.executor.submit.call_args.args[0]()
    except Exception as exc:
        future.set_exception(exc)
    event, payload = c.emit.call_args.args
    assert event == "history.remote.loaded" and not payload["loading"]
    assert "isn't reachable" in payload["notice"]
    assert dispatch(c, "history.list")["total"] == 1


def test_history_delete_can_remove_retained_audio_only_when_explicitly_chosen(calendar_context):
    from pathlib import Path
    c = calendar_context
    folder = Path(c.history.recordings_folder)
    folder.mkdir(exist_ok=True)
    for item_id in ("keep-audio", "delete-audio"):
        (folder / f"{item_id}.wav").write_bytes(b"RIFF")
        add(c, item_id, "2026-11-02T10:00:00", audio_file=f"{item_id}.wav")
    dispatch(c, "history.delete", {"id": "keep-audio"})
    assert (folder / "keep-audio.wav").exists()
    dispatch(c, "history.delete", {"id": "delete-audio", "delete_audio": True})
    assert not (folder / "delete-audio.wav").exists()
    assert dispatch(c, "history.list")["total"] == 0
