"""A restored backup brings actions back the way the app reads them, safely off."""

from services.actions.model import load_actions
from services.backup import _sanitize_settings
from services.settings import SettingsKey

LINK_TEMPLATE = "https://api.example.com/x?key=LINK-KEY&q={text}"


def _restored(*rows):
    return _sanitize_settings({SettingsKey.ACTIONS: list(rows)})


def test_a_kind_with_stray_spaces_comes_back_off_like_the_app_reads_it():
    restored = _restored(
        {"id": "x1", "name": "Notes", "kind": "program ",  # the loader strips it
         "params": {"program": r"C:\Windows\System32\cmd.exe", "args": ["/c", "calc"],
                    "output": "none", "timeout_s": 30},
         "hotkey": "ctrl+alt+9", "spoken_names": ["notes"], "enabled": True},
        {"id": "x2", "name": "Hook", "kind": " Webhook", "params": {"url": "https://h.example"},
         "enabled": True},
    )
    actions = {a.id: a for a in load_actions(restored)}
    assert actions["x1"].kind == "program" and not actions["x1"].enabled
    # Saved exactly as the app reads it, so nothing differs on the next load.
    assert restored[SettingsKey.ACTIONS][0]["kind"] == "program"
    assert "x2" not in actions  # not a kind the app knows: dropped


def test_webhook_program_and_agent_actions_come_back_off():
    restored = _restored(
        {"id": "w", "name": "W", "kind": "webhook", "params": {"url": "https://h.example/x"}},
        {"id": "p", "name": "P", "kind": "program", "params": {"program": "notes.exe"}},
        {"id": "a", "name": "A", "kind": "agent", "params": {"agent": "claude_code"}},
        {"id": "l", "name": "L", "kind": "open_link", "params": {"template": LINK_TEMPLATE}},
        {"id": "c", "name": "C", "kind": "clipboard"},
    )
    enabled = {a.id: a.enabled for a in load_actions(restored)}
    assert enabled == {"w": False, "p": False, "a": False, "l": True, "c": True}


def test_urls_inside_actions_are_scrubbed_except_a_link_template():
    restored = _restored(
        {"id": "w", "name": "Slack", "kind": "webhook",
         "params": {"url": "https://user:hunter2@hooks.example.com/hook?token=ACTION-SECRET",
                    "output": "none"}},
        {"id": "p", "name": "P", "kind": "program",
         "params": {"program": "curl.exe", "args": ["https://u:pw@example.com/a?sig=SECRET"]}},
        {"id": "l", "name": "Link", "kind": "open_link", "params": {"template": LINK_TEMPLATE}},
    )
    actions = {a.id: a for a in load_actions(restored)}
    assert actions["w"].param("url") == "https://hooks.example.com/hook"
    assert actions["p"].param("args") == ["https://example.com/a"]
    assert actions["l"].param("template") == LINK_TEMPLATE
    assert "SECRET" not in str(restored[SettingsKey.ACTIONS]).replace("LINK-KEY", "")


def test_snippets_keep_their_urls():
    restored = _sanitize_settings({"dictation_snippets": [{"text": "https://x.example/?a=1"}]})
    assert restored["dictation_snippets"] == [{"text": "https://x.example/?a=1"}]
