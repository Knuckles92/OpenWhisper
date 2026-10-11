"""Handing dictated words to an installed agent (services/actions/agents.py).

Links, terminals and agent lookups are recorded instead of opened.
"""
import os
import types
from urllib.parse import parse_qs, urlsplit

import pytest

from services import installed_agents
from services.actions import agents, launch
from services.installed_agents import InstalledAgent

WORDS = "Fix the build & ship it? 100% #now / ünïcödé 日本 \"q\" 'x' --help\nline two"
HOME = os.path.expanduser("~")
CLAUDE = InstalledAgent("claude_code", "/agents/claude.exe", "2.1.296")
CODEX = InstalledAgent("codex", "/agents/codex.exe", "0.162.1")
OPENCODE = InstalledAgent("opencode_cli", "/agents/opencode.exe", "2.0.18")


@pytest.fixture
def desk(monkeypatch):
    """Handlers by scheme; opened links and terminals; installed agents by id."""
    state = types.SimpleNamespace(handlers={}, opened=[], terminals=[], broken=set(),
                                  installed={a.id: a for a in (CLAUDE, CODEX, OPENCODE)})

    def open_uri(uri):
        if urlsplit(uri).scheme in state.broken:
            raise launch.LaunchError("That app didn't open.")
        state.opened.append(uri)

    monkeypatch.setattr(launch, "handler_available", lambda scheme: state.handlers.get(scheme, False))
    monkeypatch.setattr(launch, "open_uri", open_uri)
    monkeypatch.setattr(launch, "open_in_terminal",
                        lambda argv, cwd="", env=None: state.terminals.append((argv, cwd, env)))
    monkeypatch.setattr(agents, "_find", lambda agent_id: state.installed.get(agent_id))
    return state


def _query(uri):
    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}{parts.path}", {
        key: values[0] for key, values in parse_qs(parts.query, keep_blank_values=True).items()}


# ---- Claude Code ----

def test_claude_prefers_the_desktop_app_and_leaves_the_folder_to_it(desk):
    desk.handlers.update({"claude": True, "claude-cli": True})
    result = agents.handoff("claude_code", WORDS)
    assert result.ok and result.message == "Opened Claude Code"
    (uri,) = desk.opened
    base, query = _query(uri)
    assert base == "claude://code/new" and query == {"q": WORDS}
    assert not desk.terminals


def test_claude_desktop_link_carries_a_chosen_folder(desk, tmp_path):
    desk.handlers["claude"] = True
    agents.handoff("claude_code", WORDS, folder=str(tmp_path))
    assert _query(desk.opened[0])[1] == {"q": WORDS, "folder": str(tmp_path)}


def test_every_reserved_character_is_encoded(desk):
    desk.handlers["claude"] = True
    agents.handoff("claude_code", WORDS)
    query = urlsplit(desk.opened[0]).query
    assert set(query.split("&")[0][2:]) <= set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789%-._~")


def test_claude_code_link_is_next_and_always_names_a_folder(desk, tmp_path):
    desk.handlers["claude-cli"] = True
    agents.handoff("claude_code", WORDS)
    agents.handoff("claude_code", WORDS, folder=str(tmp_path))
    first, second = (_query(uri) for uri in desk.opened)
    assert first == ("claude-cli://open", {"q": WORDS, "cwd": HOME})
    assert second == ("claude-cli://open", {"q": WORDS, "cwd": str(tmp_path)})


def test_claude_falls_back_to_a_terminal(desk):
    result = agents.handoff("claude_code", WORDS)
    assert result.ok and result.message == "Opened Claude Code in a terminal"
    (argv, cwd, env), = desk.terminals
    assert argv == [CLAUDE.path, "--", WORDS] and cwd == HOME
    assert isinstance(env, dict) and "OPENWHISPER_SIDECAR_TOKEN" not in env


def test_a_lone_command_word_reaches_claude_as_a_prompt(desk):
    # "claude -- purge" would run Claude Code's purge command.
    agents.handoff("claude_code", "purge", surface="terminal")
    assert desk.terminals[0][0] == [CLAUDE.path, "--", "purge "]


@pytest.mark.parametrize("length, expected", [
    (agents.CLAUDE_CLI_TEXT_MAX, "claude-cli"),
    (agents.CLAUDE_CLI_TEXT_MAX + 1, "terminal"),
])
def test_claude_code_link_limit(desk, length, expected):
    desk.handlers["claude-cli"] = True
    agents.handoff("claude_code", "a " * (length // 2) + "b" * (length % 2))
    assert ("terminal" if desk.terminals else urlsplit(desk.opened[0]).scheme) == expected


def test_claude_desktop_limit_sends_long_words_to_the_terminal(desk):
    desk.handlers.update({"claude": True, "claude-cli": True})
    agents.handoff("claude_code", "word " * 2000)  # 10,000 characters
    assert urlsplit(desk.opened[0]).scheme == "claude"
    too_long = "word " * (agents.CLAUDE_APP_TEXT_MAX // 5 + 1)
    assert agents.handoff("claude_code", too_long).message.endswith("in a terminal")
    refused = agents.handoff("claude_code", too_long, surface="app")
    assert not refused.ok and "too long" in refused.message
    assert len(desk.terminals) == 1


def test_links_stay_inside_the_command_line_budget(desk):
    # 4,000 CJK characters encode to 36,000; no handler could receive that.
    words = "日本語" * 1334
    assert agents.app_links("claude_code", words) == []
    desk.handlers.update({"claude": True, "claude-cli": True})
    assert agents.handoff("claude_code", words).message.endswith("in a terminal")


def test_a_link_that_fails_to_open_falls_through(desk):
    desk.handlers.update({"claude": True, "claude-cli": True})
    desk.broken.add("claude")
    assert agents.handoff("claude_code", WORDS).ok
    assert [urlsplit(uri).scheme for uri in desk.opened] == ["claude-cli"]


def test_unknown_handlers_are_tried_only_when_the_app_was_asked_for(desk):
    desk.handlers["claude"] = None
    agents.handoff("claude_code", WORDS)
    assert desk.opened == [] and len(desk.terminals) == 1
    assert agents.handoff("claude_code", WORDS, surface="app").ok
    assert urlsplit(desk.opened[0]).scheme == "claude"


def test_terminal_surface_ignores_app_links(desk):
    desk.handlers.update({"claude": True, "claude-cli": True})
    assert agents.handoff("claude_code", WORDS, surface="terminal").ok
    assert desk.opened == [] and len(desk.terminals) == 1


def test_app_surface_never_opens_a_terminal(desk):
    result = agents.handoff("claude_code", WORDS, surface="app")
    assert not result.ok and "Claude app" in result.message
    assert desk.terminals == []


def test_app_surface_reports_the_link_failure(desk):
    desk.handlers["claude"] = True
    desk.broken.add("claude")
    result = agents.handoff("claude_code", WORDS, surface="app")
    assert not result.ok and result.message == "That app didn't open."


def test_the_desktop_app_works_without_the_cli(desk):
    desk.installed.clear()
    desk.handlers["claude"] = True
    assert agents.handoff("claude_code", WORDS).ok


# ---- Codex and OpenCode ----

def test_codex_opens_the_chatgpt_app(desk, tmp_path):
    desk.handlers["codex"] = True
    assert agents.handoff("codex", WORDS).message == "Opened Codex"
    agents.handoff("codex", WORDS, folder=str(tmp_path))
    assert _query(desk.opened[0]) == ("codex://new", {"prompt": WORDS})
    assert _query(desk.opened[1]) == ("codex://new", {"prompt": WORDS, "path": str(tmp_path)})


def test_codex_terminal_keeps_the_words_out_of_its_commands(desk, tmp_path):
    assert agents.handoff("codex", "exec", folder=str(tmp_path)).ok
    (argv, cwd, _env), = desk.terminals
    assert argv == [CODEX.path, "--", "exec"] and cwd == str(tmp_path)


def test_opencode_opens_a_terminal_session_with_the_prompt(desk):
    desk.handlers["opencode"] = True  # its desktop app documents no prompt link
    assert agents.handoff("opencode_cli", WORDS).message == "Opened OpenCode in a terminal"
    assert desk.terminals[0][0] == [OPENCODE.path, f"--prompt={WORDS}"]
    assert desk.opened == []
    refused = agents.handoff("opencode_cli", WORDS, surface="app")
    assert not refused.ok and "Terminal" in refused.message


# ---- refusals ----

@pytest.mark.parametrize("agent_id, text, kwargs, message", [
    ("pi", WORDS, {}, "Choose Claude Code, Codex or OpenCode"),
    ("codex", "  \n", {}, "nothing to send to Codex"),
    ("codex", WORDS, {"surface": "window"}, "Choose where"),
    ("codex", WORDS, {"folder": "relative/folder"}, "folder doesn't exist"),
    ("codex", WORDS, {"folder": os.path.join(HOME, "no-such-folder-0w")}, "folder doesn't exist"),
])
def test_refusals(desk, agent_id, text, kwargs, message):
    result = agents.handoff(agent_id, text, **kwargs)
    assert not result.ok and message in result.message
    assert desk.opened == [] and desk.terminals == []


def test_a_missing_agent_is_named(desk):
    desk.installed.clear()
    result = agents.handoff("codex", WORDS)
    assert not result.ok and result.message == "Codex isn't installed."


def test_terminal_errors_reach_the_user(desk, monkeypatch):
    def refuse(argv, cwd="", env=None):
        raise launch.LaunchError("codex.cmd is a script, and Windows would read your words.")

    monkeypatch.setattr(launch, "open_in_terminal", refuse)
    result = agents.handoff("codex", WORDS)
    assert not result.ok and "script" in result.message


# ---- detection ----

def test_available_reads_the_last_scan_without_scanning(monkeypatch):
    monkeypatch.setattr(installed_agents, "cached_scan", lambda: None)
    monkeypatch.setattr(installed_agents, "scan_agents", lambda *a, **k: pytest.fail("scanned"))
    assert agents.available() == {}
    monkeypatch.setattr(installed_agents, "cached_scan",
                        lambda: {"claude_code": CLAUDE, "codex": None, "opencode_cli": None})
    assert agents.available() == {"claude_code": True, "codex": False, "opencode_cli": False}


def test_find_uses_the_scan_while_its_program_exists(monkeypatch, tmp_path):
    program = tmp_path / "claude.exe"
    program.write_bytes(b"")
    scanned = InstalledAgent("claude_code", str(program), "2.1.296")
    fresh = InstalledAgent("claude_code", "/elsewhere/claude", "2.1.300")
    monkeypatch.setattr(installed_agents, "cached_scan", lambda: {"claude_code": scanned})
    monkeypatch.setattr(installed_agents, "find_agent", lambda agent_id: fresh)
    assert agents._find("claude_code") is scanned
    program.unlink()
    assert agents._find("claude_code") is fresh
