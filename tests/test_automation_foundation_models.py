"""Saved webhooks, programs, text filters and actions; permissions."""

import os
import sys

import pytest

from services.actions import model as actions
from services.actions.model import Action, ActionKind
from services.automation import models, permissions
from services.automation.models import Program, TextFilter, Webhook
from services.settings import SETTING_DEFAULTS, SettingsKey

PROGRAM = sys.executable


def _saved(mutator, settings=None, *args):
    settings = dict(settings or {})
    mutator(settings, *args)
    return settings


# -- hooks --------------------------------------------------------------------

def test_webhooks_save_load_and_validate():
    hook = Webhook("h1", "n8n", "http://localhost:5678/hook", ("history.saved", "bogus"))
    settings = _saved(models.save_webhook, None, hook)
    loaded, = models.load_webhooks(settings)
    assert loaded.events == ("history.saved",) and loaded.enabled
    with pytest.raises(ValueError, match="name already exists"):
        models.save_webhook(settings, Webhook("h2", "N8N", "https://a.example", ("history.saved",)))
    with pytest.raises(ValueError, match="http"):
        models.save_webhook(settings, Webhook("h2", "x", "ftp://a.example", ("history.saved",)))
    with pytest.raises(ValueError, match="event"):
        models.save_webhook(settings, Webhook("h2", "x", "https://a.example", ()))
    models.delete_webhook(settings, "h1")
    assert models.load_webhooks(settings) == []


def test_webhook_limit():
    settings = {}
    for n in range(models.MAX_WEBHOOKS):
        models.save_webhook(settings, Webhook(f"h{n}", f"hook {n}", "https://a.example",
                                              ("history.saved",)))
    with pytest.raises(ValueError, match="up to"):
        models.save_webhook(settings, Webhook("extra", "extra", "https://a.example",
                                              ("history.saved",)))
    # Editing an existing one is still fine at the limit.
    models.save_webhook(settings, Webhook("h0", "renamed", "https://a.example",
                                          ("history.saved",)))


def test_programs_need_a_full_path():
    with pytest.raises(ValueError, match="full path"):
        models.save_program({}, Program("p", "Script", "python", (), ("history.saved",)))
    settings = _saved(models.save_program, None,
                      Program("p", "Script", PROGRAM, ("x.py",), ("history.saved",), timeout_s=10))
    program, = models.load_programs(settings)
    assert program.args == ("x.py",) and program.timeout_s == 10


def test_filters_by_kind():
    http = TextFilter("f1", "Fix names", models.FILTER_HTTP, models.AFTER_CLEANUP,
                      url="http://127.0.0.1:9000/filter")
    settings = _saved(models.save_filter, None, http)
    with pytest.raises(ValueError, match="time limit"):
        models.save_filter(settings, TextFilter("f2", "Slow", models.FILTER_HTTP,
                                                models.BEFORE_CLEANUP,
                                                url="http://127.0.0.1:1", timeout_ms=9000))
    program = TextFilter("f3", "Local", models.FILTER_PROGRAM, models.BEFORE_CLEANUP,
                         program=PROGRAM)
    models.save_filter(settings, program)
    assert [f.id for f in models.load_filters(settings)] == ["f1", "f3"]


def test_malformed_rows_are_skipped():
    settings = {SettingsKey.AUTOMATION_FILTERS: [
        {"id": "a", "name": "A", "kind": "shell", "stage": "after_cleanup", "url": "x"},
        {"id": "b", "name": "B", "kind": "http", "stage": "later", "url": "x"},
        {"id": "c", "name": "C", "kind": "http", "stage": "after_cleanup"},
        "junk",
        {"id": "d", "name": "D", "kind": "http", "stage": "after_cleanup", "url": "http://x",
         "timeout_ms": 99999, "enabled": "yes"},
    ]}
    loaded, = models.load_filters(settings)
    assert loaded.id == "d" and loaded.timeout_ms == models.FILTER_TIMEOUT_MAX_MS
    assert loaded.enabled is True


@pytest.mark.parametrize("url,private", [
    ("http://localhost:1/x", True), ("http://127.0.0.1/x", True),
    ("http://192.168.1.20/x", True), ("http://homeassistant.local:8123", True),
    ("https://hooks.example.com/x", False), ("http://8.8.8.8", False),
])
def test_private_urls(url, private):
    assert models.url_is_private(url) is private


# -- actions ------------------------------------------------------------------

def _link(**changes):
    values = dict(id="a1", name="Ask Claude", kind=ActionKind.OPEN_LINK,
                  params={"template": "claude://claude.ai/new?q={text}"})
    values.update(changes)
    return Action(**values)


def test_actions_round_trip_and_drop_unknown_params():
    action = _link(params={"template": "claude://claude.ai/new?q={text}", "evil": "x"},
                   spoken_names=("Claude", "claude", " ask   Claude "))
    settings = _saved(actions.save_action, None, action)
    loaded, = actions.load_actions(settings)
    assert loaded.params == {"template": "claude://claude.ai/new?q={text}"}
    assert loaded.spoken_names == ("Claude", "ask Claude")
    assert actions.find_action(settings, "a1") == loaded


def test_public_dict_hides_urls_paths_and_folders():
    agent = Action("ag", "Agent", ActionKind.AGENT,
                   {"agent": "codex", "folder": os.path.abspath(os.sep)})
    data = agent.public_dict()
    assert "params" not in data and data["agent"] == "codex" and "folder" not in data
    hook = Action("w", "Hook", ActionKind.WEBHOOK, {"url": "https://x.example/secret"})
    assert "secret" not in str(hook.public_dict())


def test_links_are_percent_encoded_and_dangerous_schemes_refused():
    assert actions.expand_link("claude://claude.ai/new?q={text}", "a & b?/c") == (
        "claude://claude.ai/new?q=a%20%26%20b%3F%2Fc"
    )
    for template in ("file:///etc/passwd", "javascript:alert(1)", "DATA:text/html,x",
                     "no scheme {text}", ""):
        with pytest.raises(ValueError):
            actions.expand_link(template, "x")
    with pytest.raises(ValueError):
        actions.save_action({}, _link(params={"template": "file:///{text}"}))


def test_spoken_names_must_be_unique_across_actions():
    settings = _saved(actions.save_action, None, _link(spoken_names=("Claude",)))
    with pytest.raises(ValueError, match="already names Ask Claude"):
        actions.save_action(settings, _link(id="a2", name="Other", spoken_names=("claude",)))
    with pytest.raises(ValueError, match="already names Ask Claude"):
        actions.save_action(settings, _link(id="a2", name="Claude"))


def test_action_hotkeys_conflict_with_other_bindings():
    settings = {SettingsKey.TEXT_TRANSFORMS: [
        {"id": "t", "name": "Polish", "instruction": "x", "hotkey": "ctrl+alt+p"}
    ]}
    with pytest.raises(ValueError, match="Polish"):
        actions.save_action(settings, _link(hotkey="ctrl+alt+p"))
    actions.save_action(settings, _link(hotkey="ctrl+alt+k"))
    assert actions.action_hotkey_conflict("ctrl+alt+k", settings) == "Ask Claude"
    assert actions.action_hotkey_conflict("ctrl+alt+k", settings, exclude_id="a1") == ""


def test_shaping_must_point_at_something_that_exists():
    with pytest.raises(ValueError, match="profile"):
        actions.save_action({}, _link(shaping="profile:nope"))
    with pytest.raises(ValueError, match="transform"):
        actions.save_action({SettingsKey.TEXT_TRANSFORMS: []}, _link(shaping="transform:nope"))
    settings = _saved(actions.save_action, None, _link(shaping="transform:polish"))
    loaded, = actions.load_actions(settings)
    assert loaded.transform_id() == "polish" and loaded.profile_id() == ""


@pytest.mark.parametrize("kind,params,message", [
    (ActionKind.AGENT, {"agent": "gpt"}, "Claude Code"),
    (ActionKind.AGENT, {"folder": "relative/dir"}, "full path"),
    (ActionKind.WEBHOOK, {"url": "nope"}, "address"),
    (ActionKind.PROGRAM, {"program": "python"}, "full path"),
])
def test_kind_parameters_are_checked(kind, params, message):
    with pytest.raises(ValueError, match=message):
        actions.save_action({}, Action("x", "X", kind, params))


def test_presets_validate_once_filled_in():
    for preset in actions.PRESETS:
        params = dict(preset.params)
        if preset.kind == ActionKind.WEBHOOK:
            params["url"] = "https://hooks.example.com/x"
        if preset.kind == ActionKind.PROGRAM:
            params["program"] = PROGRAM
        actions.validate_action(
            Action(preset.id, preset.name, preset.kind, params, shaping=preset.shaping), {}
        )


# -- permissions --------------------------------------------------------------

def test_permissions_default_and_ignore_malformed_values():
    assert permissions.granted({}, permissions.LIVE_EVENTS)
    for key in (permissions.RUN_ACTIONS, permissions.CONTROL_RECORDING, permissions.TEXT_FILTERS):
        assert not permissions.granted({}, key)
        assert not permissions.granted({key: "true"}, key)
        assert permissions.granted({key: True}, key)
        with pytest.raises(permissions.PermissionDenied):
            permissions.require({}, key)
    assert not permissions.granted({}, "mcp_enabled")
    assert set(permissions.describe({})) == {
        "live_events", "run_actions", "control_recording", "text_filters"
    }
    assert SETTING_DEFAULTS[SettingsKey.API_LIVE_EVENTS] is True
