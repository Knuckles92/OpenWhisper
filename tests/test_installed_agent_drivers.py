"""The meeting-tool MCP server and the Claude Code / Codex / OpenCode drivers.

No real agent runs: ``tests/fakes/fake_agent_cli.py`` and ``fake_acp_agent.py``
stand in for the CLIs and make real MCP calls back over HTTP.
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from meeting.agent.installed import drivers
from meeting.agent.installed.drivers import (
    ClaudeCodeDriver,
    CodexDriver,
    OpenCodeDriver,
    PassRequest,
    tool_contract,
)
from meeting.agent.installed.mcp_server import LoopbackMcpServer
from meeting.agent.tool_specs import mcp_tool_definitions
from services.installed_agents import InstalledAgent

FAKES = Path(__file__).parent / "fakes"
PATCH_CALL = {"name": "patch_state", "arguments": {"ops": [
    {"op": "set_topic", "text": "Beta", "evidence": ["sg_1"]}]}}


@pytest.fixture
def server():
    srv = LoopbackMcpServer()
    srv.start()
    yield srv
    srv.stop()


def _post(url, message, token=None, headers=None):
    request = urllib.request.Request(url, data=json.dumps(message).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {token}"} if token else {}),
                                              **(headers or {})})
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read()
        return response.status, (json.loads(body) if body else None)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, name, args, meta):
        self.calls.append((name, args, meta))
        return json.dumps({"results": [{"ok": True}]}), False


# ---- MCP server ----

def test_mcp_round_trip(server):
    handler = Recorder()
    endpoint = server.open_endpoint(mcp_tool_definitions(), handler)
    url = server.url(endpoint)
    status, init = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                               "params": {"protocolVersion": "2025-06-18"}}, endpoint.token)
    assert status == 200 and init["result"]["protocolVersion"] == "2025-06-18"
    status, _ = _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, endpoint.token)
    assert status == 202
    _, listed = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, endpoint.token)
    assert {t["name"] for t in listed["result"]["tools"]} >= {"patch_state", "ask_question"}
    assert endpoint.listed.is_set()
    _, called = _post(url, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {**PATCH_CALL, "_meta": {"k": "v"}}}, endpoint.token)
    assert called["result"]["isError"] is False
    assert handler.calls == [("patch_state", PATCH_CALL["arguments"], {"k": "v"})]
    # A newer client probes first; the unknown method sends it to initialize.
    _, discover = _post(url, {"jsonrpc": "2.0", "id": 4, "method": "server/discover"}, endpoint.token)
    assert discover["error"]["code"] == -32601


@pytest.mark.parametrize("token_kind, headers, status", [
    ("wrong", {}, 401),
    ("none", {}, 401),
    ("right", {"Origin": "https://evil.example"}, 403),
])
def test_mcp_refuses_strangers(server, token_kind, headers, status):
    endpoint = server.open_endpoint(mcp_tool_definitions(), Recorder())
    token = {"wrong": "nope", "none": None, "right": endpoint.token}[token_kind]
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(server.url(endpoint), {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
              token, headers)
    assert caught.value.code == status


def test_a_closed_endpoint_has_no_authority(server):
    handler = Recorder()
    endpoint = server.open_endpoint(mcp_tool_definitions(), handler)
    url, token = server.url(endpoint), endpoint.token
    endpoint.active = False  # closed, but the agent still holds the token
    _, reply = _post(url, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": PATCH_CALL}, token)
    assert reply["result"]["isError"] and "ended" in reply["result"]["content"][0]["text"]
    assert handler.calls == []
    server.close_endpoint(endpoint)
    with pytest.raises(urllib.error.HTTPError):
        _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token)


def test_unknown_tools_are_refused(server):
    endpoint = server.open_endpoint(mcp_tool_definitions(), Recorder())
    _, reply = _post(server.url(endpoint), {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": "Bash", "arguments": {}}},
                     endpoint.token)
    assert reply["result"]["isError"]


# ---- tool contract ----

def test_tool_contract_names_tools_as_the_agent_sees_them():
    text = tool_contract(mcp_tool_definitions(), "OpenCode", "openwhisper_")
    assert "openwhisper_patch_state(" in text
    assert "Where these instructions name a tool" in text
    assert text.rstrip().endswith("reply with one short sentence.")
    report = tool_contract(mcp_tool_definitions(), "Codex", closing="Reply with the report.")
    assert "- patch_state(" in report and report.rstrip().endswith("Reply with the report.")


# ---- headless drivers ----

CLAUDE_HELP = ("--mcp-config --strict-mcp-config --tools --system-prompt --system-prompt-file "
               "--permission-prompts --no-session-persistence --disable-slash-commands "
               "--effort --include-partial-messages")


def _request(handler, **overrides):
    values = dict(system_prompt="charter", user_prompt="transcript", tools=mcp_tool_definitions(),
                  handler=handler, model="haiku", effort="low", timeout_s=30, stall_s=20)
    values.update(overrides)
    return PassRequest(**values)


def test_claude_command_line(monkeypatch, tmp_path):
    monkeypatch.setattr(drivers, "_help_text", lambda agent, *args: CLAUDE_HELP)
    driver = ClaudeCodeDriver(InstalledAgent("claude_code", "claude.exe", "2.1.281"))
    driver.check_ready()
    argv, env = driver.build(_request(Recorder()), "http://127.0.0.1:1/mcp/t", "t", str(tmp_path))
    assert argv[:2] == ["claude.exe", "-p"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv and "--no-session-persistence" in argv
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert "mcp__openwhisper__patch_state" in allowed and all(a.startswith("mcp__openwhisper__") for a in allowed)
    assert argv[argv.index("--model") + 1] == "haiku"
    settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
    assert settings == {"disableAllHooks": True, "alwaysThinkingEnabled": False}
    mcp = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["openwhisper"]["headers"]["Authorization"] == "Bearer t"
    assert env == {}


def test_old_claude_code_is_refused(monkeypatch):
    monkeypatch.setattr(drivers, "_help_text", lambda agent, *args: "--mcp-config only")
    with pytest.raises(drivers.AgentUnavailable, match="claude update"):
        ClaudeCodeDriver(InstalledAgent("claude_code", "claude", "1.0.0")).check_ready()


def test_codex_command_line(monkeypatch, tmp_path):
    listing = "shell_tool   stable   true\napps   stable   true\nsqlite   removed   true\nmemories  stable  false\n"
    monkeypatch.setattr(drivers, "_help_text", lambda agent, *args: (
        listing if args[:1] == ("features",) else "--json --ephemeral --disable"))
    driver = CodexDriver(InstalledAgent("codex", "codex.exe", "0.158.0"))
    driver.check_ready()
    argv, env = driver.build(_request(Recorder(), model=""), "http://127.0.0.1:1/mcp/tok", "tok",
                             str(tmp_path))
    assert argv[:3] == ["codex.exe", "exec", "--json"] and argv[-1] == "-"
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    assert "mcp_servers.openwhisper.url=http://127.0.0.1:1/mcp/tok" in overrides
    assert "mcp_servers.openwhisper.default_tools_approval_mode=approve" in overrides
    assert "approval_policy=never" in overrides and "project_doc_max_bytes=0" in overrides
    # Only enabled, non-removed features are switched off; no quoted values.
    disabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--disable"]
    assert disabled == ["shell_tool", "apps"]
    assert not any('"' in o for o in overrides)
    assert "-m" not in argv
    assert env == {"OPENWHISPER_MCP_TOKEN": "tok"}


class _FakeCli:
    """Run the fake CLI in place of the agent, with the driver's own argv."""

    def __init__(self, mode):
        self.mode = mode

    def build(self, driver_build):
        def build(request, url, token, workdir):
            argv, env = driver_build(request, url, token, workdir)
            return [sys.executable, str(FAKES / "fake_agent_cli.py"), self.mode, *argv[1:]], env
        return build


@pytest.mark.parametrize("driver_cls, mode", [(ClaudeCodeDriver, "claude"), (CodexDriver, "codex")])
def test_headless_pass_end_to_end(server, monkeypatch, driver_cls, mode):
    monkeypatch.setattr(drivers, "_help_text", lambda agent, *args: CLAUDE_HELP + " --json --ephemeral")
    monkeypatch.setenv("FAKE_TOOL_CALL", json.dumps(PATCH_CALL))
    driver = driver_cls(InstalledAgent(mode if mode == "codex" else "claude_code", "fake", "9.0.0"))
    driver.start(server)
    monkeypatch.setattr(driver, "build", _FakeCli(mode).build(driver.build))
    events = []
    handler = Recorder()
    outcome = driver.run_pass(_request(handler, on_event=lambda kind, tool: events.append(kind)))
    assert outcome.ok, outcome.error
    assert [c[0] for c in handler.calls] == ["patch_state"]
    assert outcome.usage["prompt_tokens"] > 0 and outcome.usage["completion_tokens"] > 0
    assert "tool" in events and events[-1] == "settled"
    assert server._endpoints == {}  # the pass's endpoint closed with it


def test_a_signed_out_claude_reads_as_a_sign_in_hint(server, monkeypatch):
    monkeypatch.setattr(drivers, "_help_text", lambda agent, *args: CLAUDE_HELP)
    driver = ClaudeCodeDriver(InstalledAgent("claude_code", "fake", "9.0.0"))
    driver.start(server)
    monkeypatch.setattr(driver, "build", _FakeCli("claude-fail").build(driver.build))
    outcome = driver.run_pass(_request(Recorder()))
    assert not outcome.ok and "not signed in" in outcome.error


def test_a_silent_agent_stalls_and_a_cancel_stops_it(server, monkeypatch):
    monkeypatch.setattr(drivers, "_help_text", lambda agent, *args: CLAUDE_HELP)
    driver = ClaudeCodeDriver(InstalledAgent("claude_code", "fake", "9.0.0"))
    driver.start(server)
    monkeypatch.setattr(driver, "build", _FakeCli("hang").build(driver.build))
    stalled = driver.run_pass(_request(Recorder(), stall_s=1.5))
    assert not stalled.ok and "stalled" in stalled.error
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    canceled = driver.run_pass(_request(Recorder(), cancel_event=cancel))
    assert canceled.canceled


def test_codex_errors_show_the_providers_message():
    state = drivers._RunState()
    driver = CodexDriver(InstalledAgent("codex", "codex", "0.158.0"))
    driver.handle_event({"type": "turn.failed", "error": {"message": json.dumps({
        "type": "error", "status": 400, "error": {
            "message": "The 'x' model is not supported when using Codex with a ChatGPT account."}})}},
        state, _request(Recorder()))
    outcome = driver.finish(state, 1, "")
    assert outcome.error == ("Codex: The 'x' model is not supported when using Codex "
                             "with a ChatGPT account.")


def test_claude_fails_fast_when_our_tools_did_not_connect():
    state = drivers._RunState()
    driver = ClaudeCodeDriver(InstalledAgent("claude_code", "claude", "2.1.281"))
    driver.handle_event({"type": "system", "subtype": "init", "mcp_servers": [
        {"name": "openwhisper", "status": "failed"}]}, state, _request(Recorder()))
    assert "could not reach" in state.fatal


# ---- OpenCode over ACP ----

class _FakeOpenCode(OpenCodeDriver):
    def _argv(self):
        return [sys.executable, str(FAKES / "fake_acp_agent.py")]


def _acp_log(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_opencode_pass_end_to_end(server, monkeypatch, tmp_path):
    log = tmp_path / "acp.jsonl"
    monkeypatch.setenv("FAKE_ACP_LOG", str(log))
    monkeypatch.setenv("FAKE_TOOL_CALL", json.dumps(PATCH_CALL))
    monkeypatch.setattr(drivers, "_TOOLS_REGISTERED_GRACE_S", 0.0)
    driver = _FakeOpenCode(InstalledAgent("opencode", "fake", "2.0.18"))
    driver.start(server, {"build": "COPILOT", "plan": "NOTE TAKER"})
    try:
        handler = Recorder()
        outcome = driver.run_pass(_request(handler, model="prov/fast", persona="plan",
                                           system_prompt="NOTE TAKER"))
        assert outcome.ok, outcome.error
        assert outcome.text == "Done."
        assert [c[0] for c in handler.calls] == ["patch_state"]
        assert handler.calls[0][2]["ai.opencode/sessionID"].startswith("ses_")
    finally:
        driver.close()
    entries = _acp_log(log)
    sets = [e["params"] for e in entries if e["method"] == "session/set_config_option"]
    assert {"configId": "mode", "value": "plan"} in [
        {k: s[k] for k in ("configId", "value")} for s in sets]
    assert {"configId": "model", "value": "prov/fast"} in [
        {k: s[k] for k in ("configId", "value")} for s in sets]
    assert {"configId": "effort", "value": "low"} in [
        {k: s[k] for k in ("configId", "value")} for s in sets]
    prompt = next(e["params"] for e in entries if e["method"] == "session/prompt")
    # The charter lives in the agent's system prompt, never in the turn.
    assert prompt["prompt"] == [{"type": "text", "text": "transcript"}]
    # The warm-up session and the pass's session are both deleted.
    import time
    deadline = time.time() + 5
    while time.time() < deadline and sum(e["method"] == "session/delete" for e in _acp_log(log)) < 2:
        time.sleep(0.05)
    assert sum(e["method"] == "session/delete" for e in _acp_log(log)) == 2


def test_opencode_overlay_locks_down_both_agents(server):
    driver = OpenCodeDriver(InstalledAgent("opencode", "fake", "2.0.18"))
    driver._server = server
    driver._router = server.open_endpoint([], Recorder())
    driver._personas = {"build": "COPILOT"}
    overlay = driver._overlay()
    for name in ("build", "plan"):
        rules = overlay["agents"][name]["permissions"]
        assert rules[0] == {"action": "*", "resource": "*", "effect": "deny"}
        assert rules[-1]["action"] == "openwhisper_*"
    assert "COPILOT" in overlay["agents"]["build"]["system"]
    mcp = overlay["mcp"]["servers"]["openwhisper"]
    assert mcp["codemode"] is False and mcp["type"] == "remote"


def test_opencode_refuses_every_other_permission():
    driver = OpenCodeDriver(InstalledAgent("opencode", "fake", "2.0.18"))
    options = [{"optionId": "a", "kind": "allow_once"}, {"optionId": "r", "kind": "reject_once"}]
    ours = driver._on_request("session/request_permission", {
        "toolCall": {"title": "openwhisper_patch_state"}, "options": options})
    bash = driver._on_request("session/request_permission", {
        "toolCall": {"title": "bash"}, "options": options})
    assert ours["outcome"]["optionId"] == "a" and bash["outcome"]["optionId"] == "r"
    with pytest.raises(drivers.AcpError):
        driver._on_request("fs/read_text_file", {"path": "C:/secrets"})


def test_opencode_rejects_a_model_its_providers_lack(server, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_ACP_LOG", str(tmp_path / "acp.jsonl"))
    monkeypatch.setattr(drivers, "_TOOLS_REGISTERED_GRACE_S", 0.0)
    driver = _FakeOpenCode(InstalledAgent("opencode", "fake", "2.0.18"))
    driver.start(server, {"build": "COPILOT"})
    try:
        outcome = driver.run_pass(_request(Recorder(), model="gone/model"))
    finally:
        driver.close()
    assert not outcome.ok and "no model 'gone/model'" in outcome.error
