"""Agent actions review fixes: Claude's verbose JSON, npm shims, the .exe for terminals."""

import json
import os

import pytest

from services import installed_agents
from services.actions import agents
from services.installed_agents import InstalledAgent
from tests import test_automation_agents_handoff as handoff_tests

desk = handoff_tests.desk  # links, terminals and agent lookups, recorded

#: What `npm install -g @openai/codex` writes as codex.cmd (npm's cmd-shim).
CODEX_SHIM = "\r\n".join([
    "@ECHO off", "GOTO start", ":find_dp0", "SET dp0=%~dp0", "EXIT /b", ":start", "SETLOCAL",
    "CALL :find_dp0", "", 'IF EXIST "%dp0%\\node.exe" (', '  SET "_prog=%dp0%\\node.exe"',
    ") ELSE (", '  SET "_prog=node"', ")", "",
    "endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & set PATHEXT=%PATHEXT:;.JS;=;% & "
    '"%_prog%"  "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*', "",
])
CODEX_JS = os.path.join("node_modules", "@openai", "codex", "bin", "codex.js")
CODEX_EXE = os.path.join("node_modules", "@openai", "codex", "node_modules", "@openai",
                         "codex-win32-x64", "vendor", "x86_64-pc-windows-msvc", "bin",
                         "codex.exe")

RESULT = {"type": "result", "subtype": "success", "is_error": False,
          "result": "Lima.", "session_id": "x"}


# -- finding 4: `claude -p --output-format json` with verbose output on ---------------------


@pytest.mark.parametrize("stdout", [
    json.dumps(RESULT),
    # What Claude Code prints when verbose is on in the user's config: every
    # message of the turn as one JSON array.
    json.dumps([
        {"type": "system", "subtype": "init", "session_id": "x"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Lima."}]}},
        RESULT,
    ]),
    # Pretty-printed across lines.
    json.dumps([{"type": "system", "subtype": "init"}, RESULT], indent=2),
], ids=["object", "verbose-array", "indented-array"])
def test_claude_answers_from_either_json_shape(stdout):
    result = agents._claude_answer(agents._Run("done", 0, stdout + "\n", ""))
    assert (result.ok, result.output) == (True, "Lima.")


def test_claude_verbose_errors_are_reported():
    failed = dict(RESULT, subtype="error_max_turns", is_error=True, result="")
    stdout = json.dumps([{"type": "system", "subtype": "init"}, failed])
    result = agents._claude_answer(agents._Run("done", 1, stdout, ""))
    assert not result.ok and result.message == "Claude Code couldn't answer: error_max_turns"


def test_claude_output_without_a_result_says_it_stopped():
    stdout = json.dumps([{"type": "system", "subtype": "init"}])
    result = agents._claude_answer(agents._Run("done", 1, stdout, "boom"))
    assert not result.ok and result.message == "Claude Code stopped before answering: boom"


# -- finding 9 and the .exe preference: a terminal never starts an npm .cmd shim -------------


def _npm(tmp_path, *, node=True, script=True, exe=False):
    """An npm global folder holding Codex the way npm installs it on Windows."""
    folder = tmp_path / "npm"
    folder.mkdir()
    (folder / "codex.cmd").write_bytes(CODEX_SHIM.encode())
    package = os.path.join("node_modules", "@openai", "codex", "package.json")
    for wanted, relative in ((node, "node.exe"), (script, CODEX_JS), (exe, CODEX_EXE),
                             (True, package)):
        if wanted:
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")
    return folder


@pytest.fixture
def no_node_on_path(monkeypatch):
    monkeypatch.setattr(agents.shutil, "which", lambda name, *a, **k: None)


def test_a_shim_runs_its_script_with_the_node_beside_it(tmp_path, no_node_on_path):
    folder = _npm(tmp_path)
    assert agents.npm_shim_command(str(folder / "codex.cmd")) == [
        str(folder / "node.exe"), str(folder / CODEX_JS)]


def test_a_shim_runs_its_script_with_node_from_path(tmp_path, monkeypatch):
    folder = _npm(tmp_path, node=False)
    node = tmp_path / "nodejs" / "node.exe"
    node.parent.mkdir()
    node.write_bytes(b"")
    monkeypatch.setattr(agents.shutil, "which",
                        lambda name, *a, **k: str(node) if name == "node.exe" else None)
    assert agents.npm_shim_command(str(folder / "codex.cmd")) == [str(node), str(folder / CODEX_JS)]


def test_without_node_the_packages_own_exe_runs(tmp_path, no_node_on_path):
    folder = _npm(tmp_path, node=False, exe=True)
    assert agents.npm_shim_command(str(folder / "codex.cmd")) == [str(folder / CODEX_EXE)]


@pytest.mark.parametrize("layout, source", [
    ({"node": False}, CODEX_SHIM),
    ({"script": False}, CODEX_SHIM),
    ({}, "@ECHO off\r\npowershell -File evil.ps1 %*\r\n"),
    ({}, CODEX_SHIM.replace("node.exe", "sh.exe").replace('"_prog=node"', '"_prog=sh"')),
], ids=["no-node", "no-script", "not-npm", "not-node"])
def test_a_shim_that_cannot_be_resolved_is_left_alone(tmp_path, no_node_on_path, layout, source):
    folder = _npm(tmp_path, **layout)
    (folder / "codex.cmd").write_bytes(source.encode())
    assert agents.npm_shim_command(str(folder / "codex.cmd")) is None


def test_a_handoff_through_an_npm_shim_starts_node_with_the_script(desk, tmp_path,
                                                                   monkeypatch, no_node_on_path):
    folder = _npm(tmp_path)
    shim = str(folder / "codex.cmd")
    desk.installed["codex"] = InstalledAgent("codex", shim, "0.162.1")
    monkeypatch.setattr(installed_agents, "candidate_paths", lambda agent_id: [shim])

    result = agents.handoff("codex", "fix the build & ship", surface="terminal")

    assert result.ok
    (argv, _cwd, _env), = desk.terminals
    assert argv == [str(folder / "node.exe"), str(folder / CODEX_JS), "--", "fix the build & ship"]


def test_a_terminal_prefers_an_installed_exe_over_a_newer_npm_shim(desk, tmp_path, monkeypatch,
                                                                    no_node_on_path):
    folder = _npm(tmp_path)
    shim = str(folder / "codex.cmd")
    exe = tmp_path / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    desk.installed["codex"] = InstalledAgent("codex", shim, "0.162.1")  # the newest one
    monkeypatch.setattr(installed_agents, "candidate_paths", lambda agent_id: [shim, str(exe)])

    agents.handoff("codex", "hello there", surface="terminal")

    assert desk.terminals[0][0] == [str(exe), "--", "hello there"]


def test_an_unresolvable_shim_reaches_the_terminal_refusal(desk, tmp_path, monkeypatch,
                                                           no_node_on_path):
    folder = _npm(tmp_path, node=False)
    shim = str(folder / "codex.cmd")
    desk.installed["codex"] = InstalledAgent("codex", shim, "0.162.1")
    monkeypatch.setattr(installed_agents, "candidate_paths", lambda agent_id: [shim])

    agents.handoff("codex", "hello there", surface="terminal")

    assert desk.terminals[0][0] == [shim, "--", "hello there"]
