"""Effective theme reporting and round-trip preference restoration."""

import pytest
from fastapi.testclient import TestClient

from services.agent_mcp.app import create_app
from services.agent_mcp.controls import AgentControls, ControlError, SETTING_CONTROLS
from services.settings import SettingsKey, SettingsManager, UiTheme, resolve_ui_theme


@pytest.fixture
def controls(tmp_path):
    settings = SettingsManager(str(tmp_path / "preferences.json"))
    settings.update_settings(
        {
            SettingsKey.MCP_SETTINGS_ACCESS: True,
            SettingsKey.MCP_WRITABLE_SETTINGS: {
                SettingsKey.UI_THEME: True,
                SettingsKey.AUTO_PASTE: True,
            },
        }
    )
    return AgentControls(tmp_path / "unused.db", settings)


def theme_setting(controls):
    return next(
        item
        for item in controls.get_settings()["settings"]
        if item["key"] == SettingsKey.UI_THEME
    )


@pytest.mark.parametrize(
    "mode, expected", [("classic", "dark"), ("omarchy", "omarchy")]
)
def test_inherited_theme_round_trip_preserves_desktop_default(
    controls, monkeypatch, mode, expected
):
    monkeypatch.setenv("OPENWHISPER_UI", mode)
    before = controls.settings.load_all_settings()
    theme = theme_setting(controls)
    assert theme["value"] == expected
    assert theme["inherited"] is True
    assert theme["resettable"] is True
    assert theme["writable"] is True
    assert theme["choices"] == list(UiTheme.ALL)
    assert controls.settings.load_all_settings() == before

    notifications = []
    controls.on_change = lambda kind, values: notifications.append((kind, values))
    changed = controls.update_settings({SettingsKey.UI_THEME: UiTheme.LIGHT})
    assert changed == {
        "updated": {SettingsKey.UI_THEME: UiTheme.LIGHT},
        "previous": {SettingsKey.UI_THEME: expected},
        "restore": {SettingsKey.UI_THEME: None},
    }
    assert theme_setting(controls)["inherited"] is False
    restored = controls.update_settings(changed["restore"])
    assert restored["updated"] == {SettingsKey.UI_THEME: expected}
    assert restored["previous"] == {SettingsKey.UI_THEME: UiTheme.LIGHT}
    assert restored["restore"] == {SettingsKey.UI_THEME: UiTheme.LIGHT}
    assert controls.settings.load_all_settings() == before
    assert theme_setting(controls)["inherited"] is True
    assert notifications == [
        ("settings", {SettingsKey.UI_THEME: UiTheme.LIGHT}),
        ("settings", {SettingsKey.UI_THEME: expected}),
    ]

    monkeypatch.setenv("OPENWHISPER_UI", "classic" if mode == "omarchy" else "omarchy")
    assert theme_setting(controls)["value"] == (
        "dark" if mode == "omarchy" else "omarchy"
    )


@pytest.mark.parametrize("mode", ["classic", "omarchy"])
@pytest.mark.parametrize("preference", UiTheme.ALL)
def test_saved_theme_overrides_environment_and_restores_explicitly(
    controls, monkeypatch, mode, preference
):
    monkeypatch.setenv("OPENWHISPER_UI", mode)
    controls.settings.save_setting(SettingsKey.UI_THEME, preference)
    theme = theme_setting(controls)
    assert theme["value"] == preference
    assert theme["inherited"] is False
    changed = controls.update_settings({SettingsKey.UI_THEME: UiTheme.LIGHT})
    assert changed["previous"] == {SettingsKey.UI_THEME: preference}
    assert changed["restore"] == {SettingsKey.UI_THEME: preference}
    restored = controls.update_settings(changed["restore"])
    assert restored["updated"] == {SettingsKey.UI_THEME: preference}
    assert controls.settings.get(SettingsKey.UI_THEME) == preference
    assert theme_setting(controls)["inherited"] is False


@pytest.mark.parametrize("mode", ["classic", "omarchy"])
@pytest.mark.parametrize("invalid", ["sepia", None, True, [], {}])
def test_invalid_saved_theme_reports_resolver_fallback(
    controls, monkeypatch, mode, invalid
):
    monkeypatch.setenv("OPENWHISPER_UI", mode)
    controls.settings.save_setting(SettingsKey.UI_THEME, invalid)
    theme = theme_setting(controls)
    assert theme["value"] == UiTheme.DARK
    assert theme["inherited"] is False
    changed = controls.update_settings({SettingsKey.UI_THEME: UiTheme.LIGHT})
    assert changed["previous"] == {SettingsKey.UI_THEME: UiTheme.DARK}
    assert changed["restore"] == {SettingsKey.UI_THEME: UiTheme.DARK}


def test_environment_detected_omarchy_matches_desktop_resolver(controls, monkeypatch):
    from services import desktop_session

    monkeypatch.setattr(desktop_session.sys, "platform", "linux")
    monkeypatch.setenv("OPENWHISPER_UI", "auto")
    monkeypatch.setenv("OMARCHY_PATH", "/synthetic/omarchy")
    assert theme_setting(controls)["value"] == UiTheme.OMARCHY
    assert resolve_ui_theme(controls.settings.load_all_settings()) == UiTheme.OMARCHY
    monkeypatch.setenv("OPENWHISPER_UI", "classic")
    assert theme_setting(controls)["value"] == UiTheme.DARK


def test_scalar_previous_remains_usable_for_restoring_appearance(controls, monkeypatch):
    monkeypatch.setenv("OPENWHISPER_UI", "omarchy")
    changed = controls.update_settings({SettingsKey.UI_THEME: UiTheme.LIGHT})
    restored = controls.update_settings(changed["previous"])
    assert restored["updated"] == {SettingsKey.UI_THEME: UiTheme.OMARCHY}
    assert theme_setting(controls)["value"] == UiTheme.OMARCHY


def test_resetting_an_already_inherited_theme_is_idempotent(controls, monkeypatch):
    monkeypatch.setenv("OPENWHISPER_UI", "omarchy")
    before = controls.settings.load_all_settings()
    for _ in range(2):
        assert controls.update_settings({SettingsKey.UI_THEME: None}) == {
            "updated": {SettingsKey.UI_THEME: UiTheme.OMARCHY},
            "previous": {SettingsKey.UI_THEME: UiTheme.OMARCHY},
            "restore": {SettingsKey.UI_THEME: None},
        }
        assert controls.settings.load_all_settings() == before


@pytest.mark.parametrize(
    "permission", [SettingsKey.MCP_SETTINGS_ACCESS, SettingsKey.MCP_WRITABLE_SETTINGS]
)
def test_theme_reset_keeps_existing_permission_gate_and_batch_atomicity(
    controls, permission
):
    controls.settings.update_settings(
        {
            SettingsKey.UI_THEME: UiTheme.LIGHT,
            permission: False
            if permission == SettingsKey.MCP_SETTINGS_ACCESS
            else {
                SettingsKey.AUTO_PASTE: True,
            },
        }
    )
    before = controls.settings.load_all_settings()
    with pytest.raises(ControlError, match="permission_denied"):
        controls.update_settings(
            {SettingsKey.AUTO_PASTE: False, SettingsKey.UI_THEME: None}
        )
    assert controls.settings.load_all_settings() == before


def test_null_is_not_a_general_settings_reset(controls):
    before = controls.settings.load_all_settings()
    with pytest.raises(ControlError, match="invalid_value"):
        controls.update_settings(
            {SettingsKey.UI_THEME: None, SettingsKey.AUTO_PASTE: None}
        )
    assert controls.settings.load_all_settings() == before


@pytest.mark.parametrize(
    "control",
    [control for control in SETTING_CONTROLS if control.key != SettingsKey.UI_THEME],
    ids=lambda control: control.key,
)
def test_other_preferences_do_not_advertise_or_accept_reset(control):
    assert not control.describe().get("resettable")
    with pytest.raises(ControlError, match="invalid_value"):
        control.validate(None)


def test_theme_reset_storage_failure_preserves_file_and_skips_notification(
    controls, monkeypatch
):
    controls.settings.save_setting(SettingsKey.UI_THEME, UiTheme.LIGHT)
    before = controls.settings.load_all_settings()
    notifications = []
    controls.on_change = lambda *args: notifications.append(args)

    def fail_replace(*args):
        raise OSError("synthetic write failure")

    monkeypatch.setattr("services.settings.os.replace", fail_replace)
    with pytest.raises(OSError, match="synthetic write failure"):
        controls.update_settings(
            {SettingsKey.UI_THEME: None, SettingsKey.AUTO_PASTE: False}
        )
    assert controls.settings.load_all_settings() == before
    assert notifications == []


def test_mcp_schema_and_theme_restoration_round_trip(controls, monkeypatch, db):
    monkeypatch.setenv("OPENWHISPER_UI", "omarchy")
    token = "theme-test-token-" + "a" * 32
    app = create_app(db.db_path, token, settings=controls.settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:

        def call(method, params):
            response = client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                },
            )
            assert response.status_code == 200
            return response.json()["result"]

        call(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "theme-test", "version": "1"},
            },
        )
        tool = next(
            item
            for item in call("tools/list", {})["tools"]
            if item["name"] == "update_settings"
        )
        schema = tool["inputSchema"]["properties"]["changes"]["additionalProperties"]
        assert {"type": "null"} in schema["anyOf"]
        assert "Only ui_theme accepts null" in tool["description"]

        changed = call(
            "tools/call",
            {
                "name": "update_settings",
                "arguments": {
                    "changes": {
                        SettingsKey.UI_THEME: UiTheme.LIGHT,
                        SettingsKey.AUTO_PASTE: False,
                    }
                },
            },
        )
        assert not changed.get("isError")
        result = changed["structuredContent"]
        assert result["previous"] == {
            SettingsKey.UI_THEME: UiTheme.OMARCHY,
            SettingsKey.AUTO_PASTE: True,
        }
        assert result["restore"] == {
            SettingsKey.UI_THEME: None,
            SettingsKey.AUTO_PASTE: True,
        }
        restored = call(
            "tools/call",
            {"name": "update_settings", "arguments": {"changes": result["restore"]}},
        )
        assert not restored.get("isError")
        assert restored["structuredContent"]["updated"] == result["previous"]
        assert SettingsKey.UI_THEME not in controls.settings.load_all_settings()

        invalid = call(
            "tools/call",
            {
                "name": "update_settings",
                "arguments": {
                    "changes": {
                        SettingsKey.AUTO_PASTE: None,
                    }
                },
            },
        )
        assert invalid["isError"] is True
        assert controls.settings.get(SettingsKey.AUTO_PASTE) is True
