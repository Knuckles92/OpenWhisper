"""Domain-level native commands against isolated settings and real SQLite."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.database import DatabaseManager
from services.history_manager import HistoryManager
from services.native_desktop.commands import dispatch
from services.settings import SettingsManager


@pytest.fixture
def context(tmp_path, monkeypatch):
    database = DatabaseManager(str(tmp_path / "history.db"))
    import services.history_manager as history_module
    monkeypatch.setattr(history_module, "db", database)
    manager = HistoryManager(str(tmp_path / "recordings"), max_recordings=None)
    controller = Mock()
    controller.recorder.is_recording = False
    controller.transcription_runtime.has_active_job = False
    controller.transcription_runtime.recording_walkthrough = None
    controller.is_meeting_active.return_value = False
    controller.transcription_readiness_message.return_value = None
    controller.meeting_runtime.meeting_busy.return_value = False
    ui = SimpleNamespace(state={"status": "Ready"}, answer_prompt=Mock())
    result = SimpleNamespace(database=database, history=manager,
        settings=SettingsManager(str(tmp_path / "settings.json")),
        controller=controller, ui=ui, emit=Mock(), captures_root=tmp_path / "captures")
    yield result
    database.engine.dispose()


def add_history(context, record_id, text, **extra):
    context.database.add_history_entry(record_id, text, "2026-11-01T10:00:00", "test", **extra)


def test_dispatch_is_allowlisted(context):
    for method in ("controller.cleanup", "__dict__", "window.classic"):
        with pytest.raises(ValueError, match="Unknown"):
            dispatch(context, method)
    with pytest.raises(ValueError, match="parameters"):
        dispatch(context, "history.list", [])


def test_hotkey_capture_suspends_global_hooks_until_explicit_end(context):
    assert dispatch(context, "hotkeys.capture", {"active": True}) == {"ok": True, "active": True}
    context.controller.hotkey_runtime.set_capture_suspended.assert_called_once_with(True)
    assert context.ui.state["hotkey_capture_active"] is True
    dispatch(context, "hotkeys.capture", {"active": False})
    assert context.controller.hotkey_runtime.set_capture_suspended.call_args.args == (False,)
    assert context.ui.state["hotkey_capture_active"] is False
    with pytest.raises(ValueError, match="true or false"):
        dispatch(context, "hotkeys.capture", {"active": "false"})


def test_native_key_events_preserve_source_and_real_release(context):
    manager = context.controller.hotkey_manager
    manager.handle_hotkey_press.return_value = True
    manager.handle_hotkey_release.return_value = True
    assert dispatch(context, "hotkeys.event", {"key": "r", "modifiers": ["ctrl", "alt"], "pressed": True})["handled"]
    manager.handle_hotkey_press.assert_called_once_with(frozenset({"ctrl", "alt"}), "r", source="native")
    manager.handle_hotkey_release.assert_not_called()
    assert dispatch(context, "hotkeys.event", {"key": "r", "pressed": False})["handled"]
    manager.handle_hotkey_release.assert_called_once_with(frozenset(), "r", source="native")
    assert not dispatch(context, "hotkeys.event", {"key": "r", "pressed": True, "repeat": True})["handled"]
    assert manager.handle_hotkey_press.call_count == 1
    with pytest.raises(ValueError, match="modifiers"):
        dispatch(context, "hotkeys.event", {"key": "r", "modifiers": ["unexpected"], "pressed": True})
    context.controller.hotkey_manager = SimpleNamespace()
    assert not dispatch(context, "hotkeys.event", {"key": "r", "pressed": True})["handled"]


def test_schema_never_echoes_unknown_settings_or_secret_values(context, monkeypatch):
    context.settings.update_settings({"OPENAI_API_KEY": "private-value", "extension_password": "private-value",
                                     "api_transcription_servers": {"custom": {"base_url": "https://user:private-value@example.test/v1?token=private-value"}}})
    monkeypatch.setattr("services.credentials.credential_source", lambda name: "stored")
    result = dispatch(context, "settings.schema")
    payload = json.dumps(result)
    assert "private-value" not in payload
    assert "extension_password" not in payload
    assert len(result["categories"]) >= 10
    assert sum(len(category["fields"]) for category in result["categories"]) >= 120
    assert all(item["value"] is None and item["configured"] for item in result["credentials"])


def test_every_persisted_setting_has_a_native_surface_or_internal_reason(context):
    coverage = dispatch(context, "settings.coverage")["items"]
    assert len(coverage) >= 173
    assert all(item["covered"] for item in coverage)
    assert all(item["surface"] or item["reason"] for item in coverage)


def test_credentials_are_write_only_and_signing_secrets_go_to_clipboard(context, monkeypatch):
    from services import credentials
    memory = credentials.MemoryCredentialBackend()
    credential_store = credentials.CredentialStore(lambda: (memory, "memory"))
    monkeypatch.setattr(credentials, "store", lambda: credential_store)
    result = dispatch(context, "credentials.set", {"name": "OPENAI_API_KEY", "value": "synthetic-test-credential"})
    assert result["configured"] and result["name"] == "OPENAI_API_KEY"
    assert "synthetic-test-credential" not in json.dumps(result)
    assert "synthetic-test-credential" not in json.dumps(context.settings.load_all_settings())
    schema = dispatch(context, "settings.schema")
    assert "synthetic-test-credential" not in json.dumps(schema)
    context.ui.clipboard = Mock()
    context.ui.clipboard.copy_to_clipboard.return_value = True
    saved = dispatch(context, "collections.save", {"collection": "actions", "item": {
        "name": "Signed delivery", "kind": "webhook", "params": {"url": "https://example.test/hook"}}})
    result = dispatch(context, "collections.secret.copy", {"collection": "actions", "id": saved["id"]})
    secret = context.ui.clipboard.copy_to_clipboard.call_args.args[0]
    assert secret.startswith("whsec_") and secret not in json.dumps(result)
    assert secret not in json.dumps(context.ui.state)
    dispatch(context, "credentials.set", {"name": "OPENAI_API_KEY", "value": ""})
    assert credential_store.get("OPENAI_API_KEY") is None


def test_settings_update_validates_all_changes_before_writing(context):
    context.settings.update_settings({"auto_paste": True})
    with pytest.raises(ValueError):
        dispatch(context, "settings.update", {"changes": {"auto_paste": False, "ui_font_scale": 999}})
    assert context.settings.get("auto_paste") is True
    with pytest.raises(ValueError):
        dispatch(context, "settings.update", {"key": "OPENAI_API_KEY", "value": "private-value"})
    assert dispatch(context, "settings.update", {"key": "auto_paste", "value": False})["ok"]
    assert context.settings.get("auto_paste") is False


def test_engine_preference_refuses_while_recording_without_persisting(context):
    context.controller.recorder.is_recording = True
    with pytest.raises(ValueError, match="Finish"):
        dispatch(context, "settings.update", {"key": "whisper_device", "value": "cpu"})
    assert "whisper_device" not in context.settings.load_all_settings()


def test_retention_previews_removal_and_cancel_preserves_settings_and_audio(context):
    for number in range(1, 4):
        (Path(context.history.recordings_folder) / f"recording_20261101_10000{number}.wav").write_bytes(b"audio")
    context.ui.request_prompt = Mock(return_value=False)
    change = {"changes": {"recording_retention_mode": "custom", "max_saved_recordings": 1}}
    assert dispatch(context, "settings.update", change)["canceled"]
    assert "max_saved_recordings" not in context.settings.load_all_settings()
    assert len(context.history.get_recordings()) == 3
    context.ui.request_prompt.return_value = True
    assert dispatch(context, "settings.update", change)["ok"]
    assert len(context.history.get_recordings()) == 1


def test_dictionary_and_snippets_use_domain_validation(context):
    saved = dispatch(context, "collections.save", {"collection": "dictionary", "item": {
        "term": "OpenWhisper", "heard": ["open whisper"], "starred": True}})
    rows = dispatch(context, "collections.list", {"collection": "dictionary"})["items"]
    assert rows[0]["id"] == saved["id"]
    assert rows[0]["heard"] == ["open whisper"]
    dispatch(context, "collections.save", {"collection": "snippets", "item": {"trigger": "my signoff", "text": "Kind regards"}})
    before = context.settings.load_all_settings()
    with pytest.raises(ValueError):
        dispatch(context, "collections.save", {"collection": "snippets", "item": {"trigger": "my signoff", "text": "Different"}})
    assert context.settings.load_all_settings() == before


def test_custom_endpoints_never_accept_embedded_credentials(context):
    with pytest.raises(ValueError, match="without credentials"):
        dispatch(context, "collections.save", {"collection": "text_models", "item": {
            "name": "Private server", "base_url": "https://user:secret@example.test/v1"}})
    context.settings.update_settings({"api_transcription_servers": {"custom": {
        "base_url": "https://user:secret@example.test/v1?token=hidden", "model": "test"}}})
    payload = json.dumps(dispatch(context, "collections.list", {"collection": "speech_servers"}))
    assert "secret" not in payload.replace('"secret": false', '')
    assert "hidden" not in payload


def test_structured_engine_preferences_and_custom_speech_key_slots(context, monkeypatch):
    from services import credentials
    memory = credentials.MemoryCredentialBackend()
    credential_store = credentials.CredentialStore(lambda: (memory, "memory"))
    monkeypatch.setattr(credentials, "store", lambda: credential_store)
    dispatch(context, "collections.save", {"collection": "speech_servers", "item": {
        "id": "custom", "base_url": "https://speech.example.test/v1",
        "api_key_env": "MY_SPEECH_API_KEY", "model": "whisper-large"}})
    schema = dispatch(context, "settings.schema")
    assert "MY_SPEECH_API_KEY" in [item["name"] for item in schema["credentials"]]
    dispatch(context, "credentials.set", {"name": "MY_SPEECH_API_KEY", "value": "test-speech-key"})
    assert credential_store.get("MY_SPEECH_API_KEY") == "test-speech-key"
    assert "test-speech-key" not in json.dumps(dispatch(context, "settings.schema"))
    with pytest.raises(ValueError, match="Unsupported credential"):
        dispatch(context, "credentials.set", {"name": "UNRELATED_PROCESS_VARIABLE", "value": "value"})
    dispatch(context, "collections.save", {"collection": "engine_devices", "item": {"id": "parakeet", "device": "cpu"}})
    assert context.settings.get("local_asr_devices")["parakeet"] == "cpu"
    before = context.settings.load_all_settings()
    context.controller.recorder.is_recording = True
    with pytest.raises(ValueError, match="Finish"):
        dispatch(context, "collections.save", {"collection": "engine_devices", "item": {"id": "parakeet", "device": "cuda"}})
    assert context.settings.load_all_settings() == before


def test_history_pagination_uses_stable_order_and_literal_search(context):
    add_history(context, "a", "100% certain", entry_kind="dictation")
    add_history(context, "b", "Ordinary words", entry_kind="file")
    add_history(context, "c", "100 percent", entry_kind="command")
    first = dispatch(context, "history.list", {"limit": 2})
    second = dispatch(context, "history.list", {"limit": 2, "offset": 2})
    assert [row["id"] for row in first["items"]] == ["c", "b"]
    assert [row["id"] for row in second["items"]] == ["a"]
    assert first["total"] == 3 and first["has_more"] and not second["has_more"]
    literal = dispatch(context, "history.list", {"query": "%"})
    assert [row["id"] for row in literal["items"]] == ["a"]
    assert dispatch(context, "history.list", {"kind": "file"})["total"] == 1


def test_history_detail_edit_export_and_delete(context, tmp_path):
    add_history(context, "a", "Words to revise", raw_text="Original words", entry_kind="dictation")
    edited = dispatch(context, "history.update", {"id": "a", "title": "Revised title", "text": "Revised words"})
    assert edited["item"]["text"] == "Revised words"
    assert dispatch(context, "history.detail", {"id": "a"})["raw"] == "Original words"
    exported = dispatch(context, "history.export", {"id": "a", "format": "json"})
    assert json.loads(exported["text"])["entries"][0]["text"] == "Revised words"
    target = tmp_path / "transcript.txt"
    dispatch(context, "history.export", {"id": "a", "format": "txt", "path": str(target)})
    assert "Revised words" in target.read_text()
    with pytest.raises(FileExistsError):
        dispatch(context, "history.export", {"id": "a", "format": "txt", "path": str(target)})
    dispatch(context, "history.delete", {"id": "a"})
    assert dispatch(context, "history.list")["total"] == 0


def test_history_playback_only_resolves_retained_regular_audio(context, tmp_path):
    audio = Path(context.history.recordings_folder) / "safe.wav"
    audio.write_bytes(b"RIFF")
    add_history(context, "safe", "Safe audio", audio_file=audio.name)
    assert dispatch(context, "history.detail", {"id": "safe"})["playback_path"] == str(audio)
    assert dispatch(context, "history.playback", {"id": "safe"})["path"] == str(audio)
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"RIFF")
    add_history(context, "unsafe", "Unsafe audio", audio_file="../outside.wav")
    with pytest.raises(ValueError, match="no saved audio"):
        dispatch(context, "history.playback", {"id": "unsafe"})


def test_walkthrough_library_checks_paths_and_shows_saved_steps(context):
    folder = context.captures_root / "2026-11-01_10-00-00"
    folder.mkdir(parents=True)
    (folder / "shot-1.png").write_bytes(b"image")
    manifest = {"version": 1, "started_at": "2026-11-01T10:00:00", "duration_s": 10,
                "sections": [{"text": "Open the application", "shot": "shot-1.png", "kept_at_s": 3}]}
    (folder / "walkthrough.json").write_text(json.dumps(manifest))
    (folder / "walkthrough.md").write_text("# Walkthrough\n\n![Open](shot-1.png)")
    result = dispatch(context, "walkthroughs.detail", {"id": folder.name})
    assert result["steps"][0]["image_path"] == str(folder / "shot-1.png")
    assert dispatch(context, "walkthroughs.list")["items"][0]["step_count"] == 1
    with pytest.raises(ValueError, match="not found"):
        dispatch(context, "walkthroughs.detail", {"id": "../outside"})
    manifest["sections"][0]["shot"] = "../outside.png"
    (folder / "walkthrough.json").write_text(json.dumps(manifest))
    assert dispatch(context, "walkthroughs.list")["warnings"]


def test_batch_queue_uses_real_request_types_and_preserves_order(context, tmp_path):
    paths = [tmp_path / "first.wav", tmp_path / "second.wav"]
    for path in paths:
        path.write_bytes(b"RIFF")
    dispatch(context, "file.enqueue", {"paths": [str(path) for path in paths], "relation": "sequential"})
    request = context.controller.upload_audio_files.call_args.args[0]
    assert request.combine
    assert request.source_names == ("first.wav", "second.wav")
    queue = dispatch(context, "file.queue")
    assert queue["active"] and [item["name"] for item in queue["items"]] == ["first.wav", "second.wav"]
    with pytest.raises(ValueError, match="Cancel"):
        dispatch(context, "file.clear")
    dispatch(context, "file.cancel")
    assert all(item["status"] == "canceled" for item in dispatch(context, "file.queue")["items"])
    with pytest.raises(ValueError, match="No file batch"):
        dispatch(context, "file.cancel")


def test_batch_queue_rejects_unavailable_engine_before_adding_files(context, tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFF")
    context.controller.transcription_readiness_message.return_value = "Download the selected model first"
    with pytest.raises(ValueError, match="Download"):
        dispatch(context, "file.enqueue", {"paths": [str(audio)]})
    assert "file_queue" not in context.ui.state


def test_meeting_capture_policy_and_prompt_responses_are_explicit(context):
    dispatch(context, "meeting.start", {"capture_mode": "mic", "cloud_enabled": False})
    assert context.controller.meeting_runtime.start_meeting.call_args.kwargs == {
        "cloud_enabled": False, "system_audio_policy": "disabled", "intent": ""}
    dispatch(context, "prompt.resolve", {"id": "42", "value": False})
    context.ui.answer_prompt.assert_called_once_with("42", False)


def test_meeting_reports_redact_capabilities_and_support_domain_edits(context):
    from meeting.persist.repository import SqlMeetingRepository
    from meeting.interfaces import TranscriptSegment
    context.meetings = SqlMeetingRepository(context.database)
    context.meetings.create_meeting(id="meeting-a", title="Planning", status="ended",
        started_at="2026-11-01T10:00:00", ended_at="2026-11-01T10:01:00", spool_dir="",
        host_token="private-host-capability", guest_token="private-guest-capability",
        state_json=json.dumps({"meeting_id": "meeting-a", "status": "ended", "title": "Planning"}))
    context.meetings.add_segments([TranscriptSegment("segment-a", "meeting-a", None,
                                                   "mic", 0, 10, "We agreed to ship the update.")])
    result = dispatch(context, "meetings.list")
    assert result["total"] == 1 and result["items"][0]["duration"] == 60
    assert "agreed" in result["items"][0]["preview"]
    result = dispatch(context, "meetings.detail", {"id": "meeting-a"})
    assert "agreed" in result["transcript"]
    assert "private-host-capability" not in json.dumps(result)
    assert "private-guest-capability" not in json.dumps(result)
    patched = dispatch(context, "meetings.patch", {"id": "meeting-a", "ops": [
        {"op": "add_item", "card": "user_notes", "text": "Remember the release notes"}]})
    assert patched["ok"]
    assert patched["item"]["cards"]["user_notes"][0]["text"] == "Remember the release notes"
    assert context.meetings.get_meeting("meeting-a")["state_seq"] > 0
    with pytest.raises(ValueError, match="supported"):
        dispatch(context, "meetings.patch", {"id": "meeting-a", "ops": [{"op": "finish_custom_report"}]})
    from meeting.stored import open_store
    external = open_store(context.meetings, "meeting-a", context.meetings.get_meeting("meeting-a"), historical=True)
    assert external.apply("host", None, [{"op": "add_item", "card": "user_notes", "text": "Saved from another client"}])[0].ok
    refreshed = dispatch(context, "meetings.detail", {"id": "meeting-a"})
    assert len(refreshed["cards"]["user_notes"]) == 2
    store = context._meeting_review_stores["meeting-a"]
    assert store.apply("host", None, [{"op": "request_custom_report", "request": "A brief", "run_id": "test-run"}])[0].ok
    with pytest.raises(ValueError, match="Finish"):
        dispatch(context, "meetings.delete", {"id": "meeting-a"})
    with pytest.raises(ValueError, match="busy"):
        dispatch(context, "meetings.retry", {"id": "meeting-a"})
    assert dispatch(context, "meetings.detail", {"id": "meeting-a"})["custom_reports"][0]["status"] == "running"


def test_walkthrough_markdown_export_copies_portable_assets(context, tmp_path):
    folder = context.captures_root / "2026-11-01_10-00-00"
    folder.mkdir(parents=True)
    (folder / "shot-1.png").write_bytes(b"image")
    (folder / "walkthrough.json").write_text(json.dumps({"version": 1,
        "started_at": "2026-11-01T10:00:00", "duration_s": 10,
        "sections": [{"text": "Open app", "shot": "shot-1.png"}]}))
    (folder / "walkthrough.md").write_text("# Walkthrough\n\n![Open](shot-1.png)")
    destination = tmp_path / "export.md"
    result = dispatch(context, "walkthroughs.export", {"id": folder.name, "format": "md", "path": str(destination)})
    assert "export-assets/shot-1.png" in destination.read_text()
    assert (tmp_path / "export-assets" / "shot-1.png").read_bytes() == b"image"
    assert result["assets"][0]["filename"] == "shot-1.png"
    archive = dispatch(context, "walkthroughs.export", {"id": folder.name, "format": "zip"})
    import zipfile
    with zipfile.ZipFile(archive["source_path"]) as zipped:
        assert {"walkthrough.md", "walkthrough.json", "shot-1.png"} <= set(zipped.namelist())
