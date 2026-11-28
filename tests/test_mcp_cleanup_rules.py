"""Tests for learned cleanup rule MCP tools, permissions, and end-to-end flow."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from config import config
from services.agent_mcp.app import create_app
from services.automation import app_control_ext
from services.automation.control import set_app_control
from services.backup import _sanitize_settings
from services.database import DatabaseManager
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings

TOKEN = "mcp-cleanup-rules-" + "r" * 32
MODERN = "2026-07-28"


class CleanupRulesAppControl:
    """Thin AppControl adapter backed directly by app_control_ext and a fake controller."""

    def __init__(self) -> None:
        self.refresh_count = 0
        self._controller = SimpleNamespace(
            ui_controller=SimpleNamespace(
                _settings_dialog=SimpleNamespace(refresh=self._on_refresh)
            )
        )

    def _on_refresh(self) -> None:
        self.refresh_count += 1

    def list_cleanup_rules(self) -> dict:
        return app_control_ext.list_cleanup_rules()

    def add_cleanup_rule(self, rule: str, *, dry_run: bool = False) -> dict:
        return app_control_ext.add_cleanup_rule(self._controller, rule, dry_run=dry_run)

    def update_cleanup_rule(self, index: int, rule: str) -> dict:
        return app_control_ext.update_cleanup_rule(self._controller, index, rule)

    def remove_cleanup_rule(self, *, index: int | None = None, rule: str = "") -> dict:
        return app_control_ext.remove_cleanup_rule(self._controller, index=index, rule=rule)

    def get_personalization_summary(self) -> dict:
        return app_control_ext.get_personalization_summary()


def _rpc(client: TestClient, method: str, params: dict | None = None) -> dict:
    payload = dict(params or {})
    payload["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": method,
    }
    if method == "tools/call":
        headers["Mcp-Name"] = payload["name"]
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": payload},
        headers=headers,
    ).json()


def _call(client: TestClient, name: str, arguments: dict | None = None) -> dict:
    res = _rpc(client, "tools/call", {"name": name, "arguments": arguments or {}})
    assert "result" in res, res
    return res["result"]


def _data(result: dict) -> dict:
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = InMemorySettings(
        {
            SettingsKey.MCP_ENABLED: True,
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True,
            SettingsKey.TRANSCRIPT_CLEANUP_LEVEL: "light",
            SettingsKey.TRANSCRIPT_CLEANUP_RULES: [],
        }
    )
    monkeypatch.setattr(app_control_ext, "settings_manager", settings)
    ctrl = CleanupRulesAppControl()
    set_app_control(ctrl)

    db_path = tmp_path / "history.db"
    db = DatabaseManager(str(db_path))
    db.add_history_entry(
        entry_id="h_last_1",
        text="We should deploy to Kubernetes and check OpenWhisper logs.",
        timestamp="2026-11-27T18:00:00Z",
        model="parakeet",
        raw_text="we should deploy to cube er net ease and check open whisper logs",
    )
    db.close()

    app = create_app(db_path, TOKEN, enabled=lambda: True, settings=settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, settings, ctrl
    set_app_control(None)


def test_cleanup_rule_mutations_default_deny_and_separate_from_personalization(env):
    client, settings, _ctrl = env

    # Read works with default permissions.
    listed = _data(_call(client, "list_cleanup_rules"))
    assert listed == {
        "cleanup_enabled": True,
        "cleanup_level": "light",
        "rules": [],
        "count": 0,
        "max_rules": config.MAX_TRANSCRIPT_CLEANUP_RULES,
    }

    # Enabling manage_personalization alone does NOT allow managing cleanup rules.
    settings.save_setting(SettingsKey.API_MANAGE_PERSONALIZATION, True)
    caps = _data(_call(client, "get_capabilities"))
    assert caps["manage_personalization"] is True
    assert caps["manage_cleanup_rules"] is False

    for tool, args in (
        ("add_cleanup_rule", {"rule": "Always write Kubernetes with a capital K."}),
        ("update_cleanup_rule", {"index": 0, "rule": "Updated rule."}),
        ("remove_cleanup_rule", {"index": 0}),
    ):
        res = _call(client, tool, args)
        assert res.get("isError") is True, f"{tool} should be denied without manage_cleanup_rules"
        assert "permission_denied" in res["content"][0]["text"]


def test_end_to_end_last_transcription_context_to_cleanup_rule(env):
    client, settings, ctrl = env
    settings.save_setting(SettingsKey.API_MANAGE_CLEANUP_RULES, True)

    # 1. Agent fetches the user's last transcription as context.
    recent = _data(_call(client, "list_transcriptions", {"limit": 1}))
    assert len(recent["items"]) == 1
    last_id = recent["items"][0]["id"]
    transcript = _data(_call(client, "get_transcription", {"record_id": last_id}))
    assert "Kubernetes" in transcript["text"]
    assert "open whisper" in transcript["raw_text"]

    # 2. Agent checks existing rules and previews with dry_run=True.
    rule_text = "Always write 'OpenWhisper' as one word in PascalCase."
    preview = _data(_call(client, "add_cleanup_rule", {"rule": rule_text, "dry_run": True}))
    assert preview["added"] is True
    assert preview["dry_run"] is True
    assert preview["count"] == 1
    assert ctrl.refresh_count == 0
    assert _data(_call(client, "list_cleanup_rules"))["count"] == 0

    # 3. Agent saves the rule.
    saved = _data(_call(client, "add_cleanup_rule", {"rule": f"  {rule_text}\n"}))
    assert saved["added"] is True
    assert saved["duplicate"] is False
    assert saved["dry_run"] is False
    assert saved["index"] == 0
    assert saved["rule"] == rule_text
    assert ctrl.refresh_count == 1

    # 4. Duplicate (case-insensitive) is detected without duplicating or refreshing.
    dup = _data(_call(client, "add_cleanup_rule", {"rule": rule_text.lower()}))
    assert dup["added"] is False
    assert dup["duplicate"] is True
    assert dup["index"] == 0
    assert dup["count"] == 1
    assert ctrl.refresh_count == 1

    # 5. get_personalization_summary includes cleanup_rules.
    summary = _data(_call(client, "get_personalization_summary"))
    assert summary["cleanup_rules"]["count"] == 1
    assert summary["cleanup_rules"]["rules"] == [{"index": 0, "rule": rule_text}]


def test_update_remove_validation_limits_and_live_revocation(env):
    client, settings, ctrl = env
    settings.save_setting(SettingsKey.API_MANAGE_CLEANUP_RULES, True)

    _data(_call(client, "add_cleanup_rule", {"rule": "Rule one."}))
    _data(_call(client, "add_cleanup_rule", {"rule": "Rule two."}))

    # Update by index.
    updated = _data(_call(client, "update_cleanup_rule", {"index": 0, "rule": "Rule one (refined)."}))
    assert updated == {
        "updated": True,
        "index": 0,
        "rule": "Rule one (refined).",
        "previous": "Rule one.",
        "count": 2,
    }

    # Updating to match another existing rule is refused.
    dup_update = _call(client, "update_cleanup_rule", {"index": 0, "rule": "rule two."})
    assert dup_update.get("isError") is True
    assert "already exists" in dup_update["content"][0]["text"]

    # Out-of-range index is not_found.
    missing_update = _call(client, "update_cleanup_rule", {"index": 5, "rule": "Nope."})
    assert missing_update.get("isError") is True
    assert "not_found" in missing_update["content"][0]["text"]

    # Control chars and whitespace-only rules are refused.
    for bad in ("   \n\t  ", "bad\x00rule"):
        res = _call(client, "add_cleanup_rule", {"rule": bad})
        assert res.get("isError") is True

    # Remove by rule text (case-insensitive) and by index.
    rem_by_text = _data(_call(client, "remove_cleanup_rule", {"rule": "RULE TWO."}))
    assert rem_by_text == {"removed": True, "index": 1, "rule": "Rule two.", "count": 1}

    rem_by_idx = _data(_call(client, "remove_cleanup_rule", {"index": 0}))
    assert rem_by_idx == {"removed": True, "index": 0, "rule": "Rule one (refined).", "count": 0}

    # Missing both index and rule is refused.
    missing_args = _call(client, "remove_cleanup_rule", {})
    assert missing_args.get("isError") is True

    # Max rules limit enforced.
    settings.save_setting(
        SettingsKey.TRANSCRIPT_CLEANUP_RULES,
        [f"Rule #{i}" for i in range(config.MAX_TRANSCRIPT_CLEANUP_RULES)],
    )
    over_limit = _call(client, "add_cleanup_rule", {"rule": "One too many."})
    assert over_limit.get("isError") is True
    assert "Rule limit reached" in over_limit["content"][0]["text"]

    # Revoking permission takes effect on the very next call.
    settings.save_setting(SettingsKey.API_MANAGE_CLEANUP_RULES, False)
    revoked = _call(client, "remove_cleanup_rule", {"index": 0})
    assert revoked.get("isError") is True
    assert "permission_denied" in revoked["content"][0]["text"]
    assert ctrl.refresh_count >= 4


def test_backup_sanitization_resets_manage_cleanup_rules():
    cleaned = _sanitize_settings({"api_manage_cleanup_rules": True})
    assert cleaned["api_manage_cleanup_rules"] is False
