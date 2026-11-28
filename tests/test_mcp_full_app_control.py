"""Tests for full MCP app control, expanded settings, and #001 walkthrough references."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services.agent_mcp.app import create_app
from services.agent_mcp.controls import CONTROLS_BY_KEY
from services.automation import app_control_ext, catalog, events
from services.automation.control import LiveState, set_app_control
from services.database import DatabaseManager
from services.settings import SettingsKey
from services.walkthrough import document, library
from services.walkthrough.session import WalkthroughResult
from tests.fakes.settings import InMemorySettings

TOKEN = "mcp-full-control-" + "c" * 32
MODERN = "2026-07-28"


class FullFakeAppControl:
    def __init__(self, walkthrough_root: Path):
        self.calls: list[tuple] = []
        self.walkthrough_root = walkthrough_root

    def _rec(self, *args):
        self.calls.append(args)

    def state(self):
        self._rec("state")
        return LiveState(
            False,
            False,
            True,
            "",
            "",
            "",
            "2.0.0",
            meeting_state={"active": True, "paused": False, "meeting_id": "m_live", "status": "active", "elapsed_s": 12.5},
            walkthrough_state={"active": False, "paused": False, "shot_count": 0, "snap_mode": "region", "auto_mode": False, "writing_up": False, "countdown_left": 0},
        )

    def list_actions(self):
        return []

    def run_action(self, action_id, text, *, source):
        self._rec("run_action", action_id, text, source)
        return "job_act"

    def start_recording(self, *, action_id="", profile_id="", route=False, source="mcp"):
        self._rec("start_recording", action_id, profile_id, route, source)
        return "job_rec"

    def stop_recording(self):
        self._rec("stop_recording")

    def cancel(self):
        self._rec("cancel")

    def notify(self, text, *, title=""):
        self._rec("notify", text, title)

    def list_profiles(self):
        return [{"id": "email", "name": "Email", "hotkey": "", "use_learned_rules": True, "active_for_quick_record": False}]

    def list_transforms(self):
        return [{"id": "polish", "name": "Polish", "hotkey": ""}]

    def run_transform(self, transform_id, text, *, source="mcp"):
        self._rec("run_transform", transform_id, text, source)
        return f"polished:{text}"

    def transcribe_audio_file(self, path, *, language=None, source="mcp"):
        self._rec("transcribe_audio_file", path, language, source)
        return {"queued": True, "filename": Path(path).name}

    def meeting_status(self):
        return {"active": True, "paused": False, "meeting_id": "m_live", "status": "active", "elapsed_s": 12.5}

    def start_meeting(self, *, title="", cloud=None, source="mcp"):
        self._rec("start_meeting", title, cloud, source)
        return {"started": True, "cloud": bool(cloud), "status": "starting"}

    def pause_meeting(self):
        self._rec("pause_meeting")
        return {"paused": True}

    def resume_meeting(self):
        self._rec("resume_meeting")
        return {"resumed": True}

    def end_meeting(self):
        self._rec("end_meeting")
        return {"ending": True}

    def background_meeting(self):
        self._rec("background_meeting")
        return {"backgrounded": True}

    def retry_meeting_finalization(self, *, step="failed", meeting_id=None):
        self._rec("retry_meeting_finalization", step, meeting_id)
        return {"retrying": True, "step": step, "meeting_id": meeting_id}

    def manage_recovered_meeting(self, meeting_id, action):
        self._rec("manage_recovered_meeting", meeting_id, action)
        return {"meeting_id": meeting_id, "action": action}

    def open_meeting_view(self, *, meeting_id="", view="dashboard"):
        self._rec("open_meeting_view", meeting_id, view)
        return {"opened": True, "view": view, "meeting_id": meeting_id or None}

    def get_meeting_dashboard_state(self, *, meeting_id=None):
        self._rec("get_meeting_dashboard_state", meeting_id)
        return {"meeting_id": meeting_id or "m_live", "is_live": True, "elapsed_s": 12.5, "state": {}, "jobs": {}}

    def update_live_meeting(self, *, title=None, intent=None, topic=None, cloud_enabled=None):
        self._rec("update_live_meeting", title, intent, topic, cloud_enabled)
        return {"ok": True, "meeting_id": "m_live", "updated": ["title"]}

    def manage_meeting_card_item(self, *, action, card=None, item_id=None, text=None, data=None, evidence=None):
        self._rec("manage_meeting_card_item", action, card, item_id, text, data, evidence)
        return {"ok": True, "action": action, "seq": 1, "item_id": item_id or "it_1"}

    def manage_meeting_question(self, *, action, text=None, question_id=None, answer_text=None, evidence=None):
        self._rec("manage_meeting_question", action, text, question_id, answer_text, evidence)
        return {"ok": True, "action": action, "seq": 2, "question_id": question_id or "q_1"}

    def update_meeting_transcript_or_speaker(
        self, *, action, participant_id=None, display_name=None, segment_id=None, text=None
    ):
        self._rec("update_meeting_transcript_or_speaker", action, participant_id, display_name, segment_id, text)
        return {"ok": True, "action": action, "seq": 3, "target_id": participant_id or segment_id}

    def request_meeting_note_adjustment(self, *, text, wait=True, timeout_s=60.0):
        self._rec("request_meeting_note_adjustment", text, wait, timeout_s)
        return {"ok": True, "status": "completed" if wait else "queued", "meeting_id": "m_live"}

    def request_meeting_custom_report(self, *, request_text, meeting_id=None):
        self._rec("request_meeting_custom_report", request_text, meeting_id)
        return {"ok": True, "meeting_id": meeting_id or "m_live", "report_id": "rep_1", "status": "running"}

    def undo_live_meeting_action(self, *, event_seq):
        self._rec("undo_live_meeting_action", event_seq)
        return {"ok": True, "meeting_id": "m_live", "undone_seq": event_seq, "applied": 1}

    def walkthrough_status(self):
        return {"active": True, "paused": False, "shot_count": 2}

    def start_walkthrough(self, *, snap_mode="", auto_mode=None, source="mcp"):
        self._rec("start_walkthrough", snap_mode, auto_mode, source)
        return {"started": True, "job_id": "job_wt"}

    def finish_walkthrough(self):
        self._rec("finish_walkthrough")
        return {"finishing": True}

    def cancel_walkthrough(self):
        self._rec("cancel_walkthrough")
        return {"canceled": True}

    def set_walkthrough_paused(self, paused):
        self._rec("set_walkthrough_paused", paused)
        return {"paused": paused}

    def snap_walkthrough(self, *, snap_mode="", source="mcp"):
        self._rec("snap_walkthrough", snap_mode, source)
        return {"snapped": True}

    def choose_walkthrough_snap_mode(self, mode):
        self._rec("choose_walkthrough_snap_mode", mode)
        return {"snap_mode": mode}

    def edit_walkthrough_shots(self, action, *, number=None, step=None):
        self._rec("edit_walkthrough_shots", action, number, step)
        return {"ok": True}

    def take_screenshot(self, *, source="mcp"):
        self._rec("take_screenshot", source)
        return {"started": True}

    def list_walkthroughs(self, *, q="", limit=20):
        return app_control_ext.list_walkthroughs(q=q, limit=limit, root=self.walkthrough_root)

    def get_walkthrough(self, ref, *, spoken=False, include_images=False):
        return app_control_ext.get_walkthrough(
            ref, spoken=spoken, include_images=include_images, root=self.walkthrough_root
        )

    def run_walkthrough_writeup(self, ref, *, style=""):
        self._rec("run_walkthrough_writeup", ref, style)
        return {"started": True, "ref": ref, "style": style or "default"}

    def get_personalization_summary(self):
        return {
            "dictionary": {"count": 1, "max_terms": 500, "terms": [{"id": "t1", "term": "OpenWhisper"}]},
            "snippets": {"count": 0, "max_snippets": 100, "snippets": []},
            "app_styles": {"enabled": True, "tones": {"email": "formal"}, "overrides": []},
            "languages": {"current": "en", "choices": ["en"], "accepted": ["en"]},
            "profiles": [],
            "walkthrough_styles": [{"id": "walkthrough", "name": "Walkthrough", "built_in": True}],
            "meeting_context": {"past_recall_enabled": False, "context_folder_enabled": False},
        }

    def list_dictionary(self):
        return {"terms": [{"id": "t1", "term": "OpenWhisper", "starred": True, "heard": [], "learned": False, "protected": False}]}

    def add_dictionary_term(self, term, *, heard=(), starred=False):
        self._rec("add_dictionary_term", term, heard, starred)
        return {"id": "t2", "term": term, "starred": starred, "heard": list(heard)}

    def bulk_add_dictionary_terms(self, items, *, dry_run=False):
        self._rec("bulk_add_dictionary_terms", items, dry_run)
        return {"dry_run": dry_run, "added": items, "updated": [], "skipped": [], "rejected": [], "total_after": len(items) + 1}

    def remove_dictionary_term(self, term_or_id):
        self._rec("remove_dictionary_term", term_or_id)
        return {"removed": True, "id": term_or_id, "term": term_or_id}

    def list_snippets(self):
        return {"enabled": True, "snippets": []}

    def upsert_snippet(self, trigger, text, *, snippet_id="", formatted=False):
        self._rec("upsert_snippet", trigger, text, snippet_id, formatted)
        return {"id": snippet_id or "s1", "trigger": trigger, "text": text, "formatted": formatted}

    def bulk_upsert_snippets(self, items, *, dry_run=False, skip_existing=True):
        self._rec("bulk_upsert_snippets", items, dry_run, skip_existing)
        return {"dry_run": dry_run, "added": items, "updated": [], "skipped": [], "rejected": [], "total_after": len(items)}

    def delete_snippet(self, snippet_id):
        self._rec("delete_snippet", snippet_id)
        return {"deleted": True, "id": snippet_id}

    def get_app_styles(self):
        return {"enabled": True, "tones": {"email": "formal", "work": "casual", "personal": "casual", "other": "formal"}, "overrides": []}

    def set_app_style_tone(self, category, tone):
        self._rec("set_app_style_tone", category, tone)
        return {"category": category, "tone": tone}

    def set_app_style_override(self, match, category):
        self._rec("set_app_style_override", match, category)
        return {"match": match, "category": category, "overrides": [{"match": match, "category": category}]}

    def remove_app_style_override(self, match):
        self._rec("remove_app_style_override", match)
        return {"removed": True, "match": match, "overrides": []}

    def list_walkthrough_styles(self):
        return {"styles": [{"id": "walkthrough", "name": "Walkthrough", "instructions": "...", "built_in": True}]}

    def upsert_walkthrough_style(self, name, instructions):
        self._rec("upsert_walkthrough_style", name, instructions)
        return {"id": "custom1", "name": name, "instructions": instructions}

    def delete_walkthrough_style(self, style_id):
        self._rec("delete_walkthrough_style", style_id)
        return {"deleted": True, "id": style_id}

    def list_languages(self):
        return {"current": "en", "choices": ["en", "es"], "accepted": ["en", "es"]}

    def set_active_language(self, code):
        self._rec("set_active_language", code)
        return {"current": code or "auto"}

    def set_quick_record_profile(self, profile_id):
        self._rec("set_quick_record_profile", profile_id)
        return {"quick_record_profile": profile_id or None}

    def engine_status(self):
        return {"engine": "local_whisper", "whisper_model": "base", "ready": True, "transcribing": False}

    def list_models(self):
        return {"selected_engine": "local_whisper", "engines": [], "whisper_models": [], "local_asr_models": []}

    def select_model(self, engine, *, model=""):
        self._rec("select_model", engine, model)
        return {"engine": engine, "whisper_model": model}

    def download_model(self, model_name):
        self._rec("download_model", model_name)
        return {"started": True, "model": model_name}

    def list_audio_devices(self):
        return {"priority": [], "devices": [{"name": "USB Mic", "hostapi": "MME"}]}

    def select_audio_device(self, name, *, hostapi=""):
        self._rec("select_audio_device", name, hostapi)
        return {"selected": {"name": name, "hostapi": hostapi}}

    def remote_status(self):
        return {"host_mode_enabled": False, "paired_to_host": False, "connected": False}

    def reconnect_remote(self):
        self._rec("reconnect_remote")
        return {"reconnecting": True}

    def rename_paired_computer(self, name, *, device_id="", device_name=""):
        self._rec("rename_paired_computer", name, device_id, device_name)
        return {"device_id": device_id or "d1", "name": name or "DESKTOP-LAPTOP",
                "previous_name": device_name or "DESKTOP-LAPTOP", "paired_name": None}

    def control_window(self, action, *, text="", tab=""):
        self._rec("control_window", action, text, tab)
        return {"ok": True, "action": action, "tab": tab or None}


def _make_walkthrough(root: Path, folder_name: str, text: str, *, number: int | None = None) -> Path:
    folder = root / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    shot = folder / "shot-1.png"
    # Minimal 1x1 PNG bytes
    shot.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff"
        b"\xff?\x00\x05\xfe\x02\xfeA\x89\x18\xb5\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    wt = document.Walkthrough(
        started_at=datetime(2026, 11, 20, 10, 0, 0),
        duration_s=15.0,
        sections=(document.Section(text=text, shot=shot, kept_at_s=5.0),),
        number=number,
    )
    document.write_manifest(wt, folder)
    (folder / "walkthrough.md").write_text(f"# Walkthrough\n\n{text}\n\n![Shot 1](shot-1.png)\n", encoding="utf-8")
    return folder


def _rpc(client, method, params=None):
    params = dict(params or {})
    params["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": method,
    }
    if method in ("tools/call", "prompts/get"):
        headers["Mcp-Name"] = params["name"]
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers=headers,
    ).json()


def _call(client, name, arguments=None):
    res = _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "result" in res, res
    return res["result"]


def _data(result):
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def env(tmp_path):
    wt_root = tmp_path / "captures"
    _make_walkthrough(wt_root, "2026-11-20_10-00-00", "First walkthrough explanation", number=1)
    _make_walkthrough(wt_root, "2026-11-21_11-00-00", "Second walkthrough about billing", number=2)
    fake = FullFakeAppControl(wt_root)
    set_app_control(fake)
    replacement = events.EventBus(buffer_size=16)
    prev_bus = events.set_bus(replacement)
    settings = InMemorySettings({SettingsKey.MCP_ENABLED: True})
    db_path = tmp_path / "history.db"
    DatabaseManager(str(db_path)).close()
    app = create_app(db_path, TOKEN, enabled=lambda: True, settings=settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, settings, fake, wt_root
    set_app_control(None)
    events.set_bus(prev_bus)
    replacement.close()


def test_walkthrough_numbering_monotonic_and_resolution(tmp_path):
    root = tmp_path / "captures"
    f1 = _make_walkthrough(root, "2026-11-20_10-00-00", "Legacy one", number=None)
    f2 = _make_walkthrough(root, "2026-11-20_10-05-00", "Legacy two", number=None)
    assert library.folder_numbers(root) == {f1: 1, f2: 2}
    assert library.format_number(1) == "#001"
    assert library.parse_ref_number("#001") == 1
    assert library.parse_ref_number("wt-002") == 2

    n3 = library.allocate_number(root)
    assert n3 == 3
    f3 = _make_walkthrough(root, "2026-11-20_10-10-00", "Numbered three", number=n3)
    assert library.resolve_folder(root, "#003") == f3
    assert library.resolve_folder(root, "latest") == f3

    # Deleting #003 still allocates #004 next (never collides/reuses)
    library.delete_entry(f3)
    assert library.allocate_number(root) == 4


def test_walkthrough_ready_card_shows_and_copies_ref_badge(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    from ui_qt.widgets import walkthrough_send
    from ui_qt.widgets.walkthrough_ready import WalkthroughReadyCard

    monkeypatch.setattr(walkthrough_send, "refresh_threads", lambda: None)

    folder = _make_walkthrough(tmp_path / "captures", "2026-11-20_10-00-00", "Check badge", number=7)
    pdf = folder / "walkthrough.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    result = WalkthroughResult(
        folder=folder,
        pdf=pdf,
        markdown=folder / "walkthrough.md",
        clipboard_text="",
        history_text="Check badge",
        history_raw=None,
        shots=1,
        duration_s=15.0,
        first_shot=folder / "shot-1.png",
        number=7,
    )
    assert result.ref == "#007"
    card = WalkthroughReadyCard()
    try:
        card.present(result, copied=True)
        assert not card.ref_badge.isHidden()
        assert card.ref_badge.text() == "#007"
        card.ref_badge.click()
        assert app.clipboard().text() == "#007"
        assert "#007" in card.subtitle.text()
    finally:
        card.close()
        card.deleteLater()


def test_default_deny_for_all_new_permission_domains(env):
    client, _settings, _fake, _wt_root = env
    for tool, args in (
        ("start_meeting", {}),
        ("pause_meeting", {}),
        ("list_walkthroughs", {}),
        ("get_walkthrough", {"ref": "#001"}),
        ("start_walkthrough", {}),
        ("snap_walkthrough", {}),
        ("take_screenshot", {}),
        ("run_transform", {"transform_id": "polish", "text": "hello"}),
        ("add_dictionary_term", {"term": "Kube"}),
        ("upsert_snippet", {"trigger": "sig", "text": "Regards"}),
        ("set_app_style_tone", {"category": "email", "tone": "casual"}),
        ("set_active_language", {"code": "es"}),
        ("select_model", {"engine": "local_whisper"}),
        ("select_audio_device", {"name": "USB Mic"}),
        ("rename_paired_computer", {"name": "Kitchen laptop", "device_name": "DESKTOP-LAPTOP"}),
        ("transcribe_audio_file", {"path": "sample.wav"}),
        ("control_window", {"action": "show"}),
    ):
        res = _call(client, tool, args)
        assert res["isError"] is True, f"{tool} should be denied by default"
        assert "permission_denied" in res["content"][0]["text"]


def test_full_app_control_tools_when_permitted(env):
    client, settings, fake, _wt_root = env
    for key in (
        SettingsKey.API_RUN_ACTIONS,
        SettingsKey.API_CONTROL_RECORDING,
        SettingsKey.API_CONTROL_MEETINGS,
        SettingsKey.API_READ_WALKTHROUGHS,
        SettingsKey.API_CONTROL_WALKTHROUGHS,
        SettingsKey.API_MANAGE_PERSONALIZATION,
        SettingsKey.API_CONTROL_APP,
    ):
        settings.values[key] = True

    # Enriched live state
    live = _data(_call(client, "get_live_state"))
    assert live["meeting_state"]["meeting_id"] == "m_live"
    assert live["walkthrough_state"]["active"] is False
    assert live["permissions"]["control_meetings"] is True
    assert live["permissions"]["control_walkthroughs"] is True

    # Dictation with profile_id / route + transforms
    assert _data(_call(client, "start_dictation", {"profile_id": "email"}))["job_id"] == "job_rec"
    assert _data(_call(client, "run_transform", {"transform_id": "polish", "text": "hi"}))["text"] == "polished:hi"

    # Meetings
    assert _data(_call(client, "start_meeting", {"title": "Standup", "cloud": False}))["started"] is True
    assert _data(_call(client, "pause_meeting"))["paused"] is True
    assert _data(_call(client, "resume_meeting"))["resumed"] is True
    assert _data(_call(client, "end_meeting"))["ending"] is True
    assert _data(_call(client, "background_meeting"))["backgrounded"] is True
    assert _data(_call(client, "retry_meeting_finalization", {"step": "insights"}))["retrying"] is True
    assert _data(_call(client, "manage_recovered_meeting", {"meeting_id": "m1", "action": "finalize"}))["action"] == "finalize"
    assert _data(_call(client, "open_meeting_view", {"view": "report"}))["opened"] is True

    # Walkthroughs: read by #001, #002, latest, and include_images
    listed = _data(_call(client, "list_walkthroughs", {"q": "#002"}))
    assert len(listed["walkthroughs"]) == 1
    assert listed["walkthroughs"][0]["ref"] == "#002"
    wt = _data(_call(client, "get_walkthrough", {"ref": "#001", "include_images": True}))
    assert wt["ref"] == "#001"
    assert wt["shots"] == 1
    assert len(wt["images"]) == 1
    assert wt["images"][0]["mime_type"] == "image/png"
    assert _data(_call(client, "get_walkthrough", {"ref": "latest"}))["ref"] == "#002"

    # Walkthrough control
    assert _data(_call(client, "start_walkthrough", {"snap_mode": "screen", "auto_mode": True}))["started"] is True
    assert _data(_call(client, "snap_walkthrough"))["snapped"] is True
    assert _data(_call(client, "set_walkthrough_paused", {"paused": True}))["paused"] is True
    assert _data(_call(client, "edit_walkthrough_shots", {"action": "undo"}))["ok"] is True
    assert _data(_call(client, "finish_walkthrough"))["finishing"] is True
    assert _data(_call(client, "run_walkthrough_writeup", {"ref": "#001", "style": "Bug report"}))["started"] is True
    assert _data(_call(client, "take_screenshot"))["started"] is True

    # Personalization (single + bulk + summary + styles + walkthrough styles)
    summary = _data(_call(client, "get_personalization_summary"))
    assert summary["dictionary"]["count"] == 1
    assert _data(_call(client, "add_dictionary_term", {"term": "TypeSafe", "starred": True}))["term"] == "TypeSafe"
    bulk_dict = _data(_call(client, "bulk_add_dictionary_terms", {
        "terms": [{"term": "PyQt6", "heard": ["pie cute six"], "starred": True}, {"term": "Tailscale"}],
        "dry_run": True,
    }))
    assert bulk_dict["dry_run"] is True
    assert len(bulk_dict["added"]) == 2
    assert _data(_call(client, "upsert_snippet", {"trigger": "my email", "text": "a@b.com"}))["trigger"] == "my email"
    bulk_snip = _data(_call(client, "bulk_upsert_snippets", {
        "snippets": [{"trigger": "standup update", "text": "Yesterday:\nToday:\nBlockers:"}],
        "dry_run": False,
    }))
    assert len(bulk_snip["added"]) == 1
    assert _data(_call(client, "set_app_style_tone", {"category": "work", "tone": "very_casual"}))["tone"] == "very_casual"
    assert _data(_call(client, "set_app_style_override", {"match": "Linear", "category": "work"}))["category"] == "work"
    assert _data(_call(client, "remove_app_style_override", {"match": "Linear"}))["removed"] is True
    assert len(_data(_call(client, "list_walkthrough_styles"))["styles"]) == 1
    assert _data(_call(client, "upsert_walkthrough_style", {"name": "PR Brief", "instructions": "Write a concise PR summary."}))["name"] == "PR Brief"
    assert _data(_call(client, "delete_walkthrough_style", {"style_id": "custom1"}))["deleted"] is True
    assert _data(_call(client, "set_active_language", {"code": "es"}))["current"] == "es"
    assert _data(_call(client, "set_quick_record_profile", {"profile_id": "email"}))["quick_record_profile"] == "email"

    # Built-in MCP prompts for chat/history personalization
    prompts_resp = _rpc(client, "prompts/list")
    prompt_names = {p["name"] for p in prompts_resp["result"]["prompts"]}
    assert {"personalize_from_context", "personalize_from_history"} <= prompt_names
    prompt_msg = _rpc(client, "prompts/get", {"name": "personalize_from_context", "arguments": {"focus": "engineering"}})
    assert "get_personalization_summary" in prompt_msg["result"]["messages"][0]["content"]["text"]
    assert "engineering" in prompt_msg["result"]["messages"][0]["content"]["text"]

    # Models, devices, remote, window
    assert _data(_call(client, "select_model", {"engine": "local_whisper", "model": "small"}))["whisper_model"] == "small"
    assert _data(_call(client, "download_model", {"model_name": "small"}))["started"] is True
    assert _data(_call(client, "select_audio_device", {"name": "USB Mic"}))["selected"]["name"] == "USB Mic"
    assert _data(_call(client, "reconnect_remote"))["reconnecting"] is True
    renamed = _data(_call(client, "rename_paired_computer",
                          {"name": "Kitchen laptop", "device_name": "DESKTOP-LAPTOP"}))
    assert renamed["name"] == "Kitchen laptop" and renamed["previous_name"] == "DESKTOP-LAPTOP"
    assert ("rename_paired_computer", "Kitchen laptop", "", "DESKTOP-LAPTOP") in fake.calls
    # An empty name is how a computer goes back to the one it paired with.
    assert _data(_call(client, "rename_paired_computer", {"name": "", "device_id": "d1"}))["device_id"] == "d1"
    assert ("rename_paired_computer", "", "d1", "") in fake.calls
    assert _data(_call(client, "control_window", {"action": "switch_tab", "tab": "walkthrough"}))["tab"] == "walkthrough"
    assert _data(_call(client, "transcribe_audio_file", {"path": "clip.wav"}))["queued"] is True


def test_expanded_settings_catalog_and_backup_scrub():
    from services.backup import _sanitize_settings

    for key in (
        SettingsKey.WALKTHROUGH_AUTO_MODE,
        SettingsKey.WALKTHROUGH_WRITE_UP,
        SettingsKey.MEETING_END_REDECODE,
        SettingsKey.TYPESAFE_CITATIONS_ENABLED,
        SettingsKey.SNIPPETS_ENABLED,
        SettingsKey.COMPACT_MODE,
    ):
        assert key in CONTROLS_BY_KEY

    sanitized = _sanitize_settings({
        "api_control_meetings": True,
        "api_read_walkthroughs": True,
        "api_control_walkthroughs": True,
        "api_manage_personalization": True,
        "api_control_app": True,
    })
    for key in (
        "api_control_meetings",
        "api_read_walkthroughs",
        "api_control_walkthroughs",
        "api_manage_personalization",
        "api_control_app",
    ):
        assert sanitized[key] is False


def test_bulk_personalization_dry_run_dedupe_and_commit(monkeypatch):
    from services import settings as settings_mod

    store = InMemorySettings({})
    monkeypatch.setattr(app_control_ext, "settings_manager", store)
    monkeypatch.setattr(settings_mod, "settings_manager", store)

    # Dry run validates and reports without mutating saved settings
    preview = app_control_ext.bulk_add_dictionary_terms(
        None,
        [
            {"term": "PyQt6", "heard": ["pie cute six"], "starred": True},
            {"term": "   "},
        ],
        dry_run=True,
    )
    assert preview["dry_run"] is True
    assert len(preview["added"]) == 1
    assert len(preview["rejected"]) == 1
    assert app_control_ext.list_dictionary()["terms"] == []

    # Real commit adds, then a second batch updates/merges variants and skips unchanged
    first = app_control_ext.bulk_add_dictionary_terms(
        None,
        [{"term": "PyQt6", "heard": ["pie cute six"], "starred": False}],
        dry_run=False,
    )
    assert len(first["added"]) == 1
    second = app_control_ext.bulk_add_dictionary_terms(
        None,
        [
            {"term": "PyQt6", "heard": ["pie cute six"], "starred": False},  # unchanged -> skipped
            {"term": "PyQt6", "heard": ["pi qt six"], "starred": True},      # new variant + star -> updated
            {"term": "Tailscale", "starred": True},                           # new -> added
        ],
        dry_run=False,
    )
    assert len(second["skipped"]) == 1
    assert len(second["updated"]) == 1
    assert len(second["added"]) == 1
    assert second["total_after"] == 2

    # Bulk snippets: dry run -> commit -> skip_existing
    snip_preview = app_control_ext.bulk_upsert_snippets(
        None,
        [{"trigger": "my signoff", "text": "Best,\nAlex"}],
        dry_run=True,
    )
    assert snip_preview["dry_run"] is True
    assert len(snip_preview["added"]) == 1
    assert app_control_ext.list_snippets()["snippets"] == []

    snip_commit = app_control_ext.bulk_upsert_snippets(
        None,
        [{"trigger": "my signoff", "text": "Best,\nAlex"}],
        dry_run=False,
    )
    assert len(snip_commit["added"]) == 1
    snip_dup = app_control_ext.bulk_upsert_snippets(
        None,
        [{"trigger": "my signoff", "text": "Different"}],
        dry_run=False,
        skip_existing=True,
    )
    assert len(snip_dup["skipped"]) == 1

    # App style overrides & walkthrough styles
    app_control_ext.set_app_style_override(None, "Linear", "work")
    assert any(o["match"] == "Linear" and o["category"] == "work" for o in app_control_ext.get_app_styles()["overrides"])
    app_control_ext.upsert_walkthrough_style(None, "PR Brief", "Write a concise PR summary.")
    summary = app_control_ext.get_personalization_summary()
    assert summary["dictionary"]["count"] == 2
    assert summary["snippets"]["count"] == 1
    assert any(s["name"] == "PR Brief" for s in summary["walkthrough_styles"])


def test_new_events_present_in_catalog_and_mcp_events_descriptors():
    from services.agent_mcp import events_ext

    names = {entry["name"] for entry in events_ext.descriptors()}
    for event_type in (
        catalog.MEETING_PAUSED,
        catalog.MEETING_RESUMED,
        catalog.MEETING_FINALIZED,
        catalog.WALKTHROUGH_STARTED,
        catalog.WALKTHROUGH_SHOT_ADDED,
        catalog.WALKTHROUGH_FINISHED,
        catalog.WALKTHROUGH_WRITEUP_COMPLETED,
    ):
        assert event_type in catalog.PUBLIC_TYPES
        assert event_type in names
