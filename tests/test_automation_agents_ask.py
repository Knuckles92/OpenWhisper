"""Asking an installed agent with no tools (services/actions/agents.py ``ask``).

No real agent runs. The real command lines are built, then their program is
swapped for a fake CLI (or a fake ``opencode acp``) written by this module,
which records what it received and answers like the real one.
"""
import base64
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from services import installed_agents
from services.actions import agents
from services.installed_agents import InstalledAgent

WORDS = "What's 2+2? Ünïcödé 日本 \"quoted\" & | ; --help\nsecond line"
CLAUDE = InstalledAgent("claude_code", "/agents/claude.exe", "2.1.296")
CODEX = InstalledAgent("codex", "/agents/codex.exe", "0.162.1")
OPENCODE = InstalledAgent("opencode_cli", "/agents/opencode.exe", "2.0.18")

CLAUDE_HELP = "--tools <tools...> --no-session-persistence --disable-slash-commands --model"
CODEX_EXEC_HELP = "--json --ephemeral --ignore-user-config --skip-git-repo-check"
CODEX_FEATURES = (
    "shell_tool    stable   true\n"
    "apps          stable   false\n"
    "code_mode_host stable  true\n"
    "js_repl       removed  false\n"
    "fast_mode     stable   true\n"
)

FAKE_CLI = r'''
import json, os, subprocess, sys, time

mode, args = sys.argv[1], sys.argv[2:]
prompt = sys.stdin.buffer.read().decode("utf-8")
with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as handle:
    json.dump({"argv": args, "prompt": prompt, "cwd": os.getcwd()}, handle)


def out(event):
    sys.stdout.buffer.write((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8"))
    sys.stdout.flush()


if mode == "claude":
    # One stream-json user message; the answer echoes its text block.
    content = json.loads(prompt)["message"]["content"]
    out({"type": "system", "subtype": "init", "tools": []})
    out({"type": "result", "subtype": "success", "is_error": False,
         "result": "Answer: " + next(b["text"] for b in content if b["type"] == "text")})
elif mode == "claude-signed-out":
    out({"type": "result", "subtype": "success", "is_error": True,
         "result": "Not logged in · Please run /login"})
    sys.exit(1)
elif mode == "claude-empty":
    out({"type": "result", "subtype": "success", "is_error": False, "result": "  "})
elif mode == "codex":
    out({"type": "thread.started", "thread_id": "t1"})
    out({"type": "turn.started"})
    out({"type": "error", "message": "Reconnecting... 1/5"})
    out({"type": "item.completed", "item": {"type": "agent_message", "text": "Answer: " + prompt}})
    out({"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 3}})
elif mode == "codex-fail":
    out({"type": "thread.started", "thread_id": "t1"})
    out({"type": "turn.failed", "error": {"message": json.dumps(
        {"error": {"message": "The 'x' model is not supported."}})}})
    sys.exit(1)
elif mode == "crash":
    sys.stderr.write("error: unknown option '--frobnicate'\n")
    sys.exit(2)
elif mode == "hang":
    helper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    with open(os.environ["FAKE_HELPER_PID"], "w") as handle:
        handle.write(str(helper.pid))
    time.sleep(60)
'''

FAKE_ACP = r'''
import json, os, sys

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
mode = sys.argv[1]
log_path = os.environ["FAKE_ACP_LOG"]


def log(entry):
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def send(message):
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def answer(msg_id, text):
    for chunk in ("Answer: ", text):
        send({"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "s1", "update": {"sessionUpdate": "agent_message_chunk",
                                          "content": {"type": "text", "text": chunk}}}})
    send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "end_turn"}})


log({"overlay": json.loads(os.environ["OPENCODE_CONFIG_CONTENT"]),
     "project_config_off": os.environ.get("OPENCODE_DISABLE_PROJECT_CONFIG")})
model = "prov/fast"
waiting = None
for line in sys.stdin:
    msg = json.loads(line)
    method, params, msg_id = msg.get("method"), msg.get("params") or {}, msg.get("id")
    log({"method": method, "params": params, "result": msg.get("result")})
    if method is None:
        if waiting is not None and msg_id == 900:
            answer(*waiting)
        continue
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"sessionCapabilities": {"delete": {}},
                                  "promptCapabilities": {"image": mode == "image"}}}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"sessionId": "s1", "configOptions": [
            {"id": "model", "type": "select", "currentValue": model,
             "options": [{"value": "prov/fast"}, {"value": "prov/slow"}]}]}})
    elif method == "session/set_config_option":
        model = params["value"]
        send({"jsonrpc": "2.0", "id": msg_id, "result": {"configOptions": []}})
    elif method == "session/prompt":
        text = next(block["text"] for block in params["prompt"] if block["type"] == "text")
        if mode == "hang":
            continue
        if mode == "auth":
            send({"jsonrpc": "2.0", "id": msg_id,
                  "error": {"code": -32000, "message": "Provider auth required"}})
        elif mode == "permission":
            waiting = (msg_id, text)
            send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {
                "sessionId": "s1", "toolCall": {"title": "bash"},
                "options": [{"optionId": "yes", "kind": "allow_once"},
                            {"optionId": "no", "kind": "reject_once"}]}})
        else:
            answer(msg_id, text)
    elif method == "session/delete":
        send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
    elif msg_id is not None:
        send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "nope"}})
'''


@pytest.fixture
def fakes(tmp_path, monkeypatch):
    """Write the fake programs; record files go in ``tmp_path``."""
    cli = tmp_path / "fake_cli.py"
    cli.write_text(FAKE_CLI, encoding="utf-8")
    acp = tmp_path / "fake_acp.py"
    acp.write_text(FAKE_ACP, encoding="utf-8")
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_HELPER_PID", str(tmp_path / "helper.pid"))
    monkeypatch.setenv("FAKE_ACP_LOG", str(tmp_path / "acp.jsonl"))
    monkeypatch.setattr(agents, "_help", lambda agent, *args: {
        ("--help",): CLAUDE_HELP,
        ("exec", "--help"): CODEX_EXEC_HELP,
        ("features", "list"): CODEX_FEATURES,
    }.get(args, ""))
    monkeypatch.setattr(installed_agents, "configured_default_model", lambda agent_id: "")
    installed = {a.id: a for a in (CLAUDE, CODEX, OPENCODE)}
    monkeypatch.setattr(agents, "_find", lambda agent_id: installed.get(agent_id))
    return {"cli": cli, "acp": acp, "record": record, "dir": tmp_path,
            "installed": installed}


def _use_cli(monkeypatch, fakes, builder, mode):
    """Run ``builder``'s real command line with the fake CLI as its program."""
    real = getattr(agents, builder)

    def build(agent, model, workdir, **kwargs):
        argv = real(agent, model, workdir, **kwargs)
        return [sys.executable, str(fakes["cli"]), mode, *argv[1:]]

    monkeypatch.setattr(agents, builder, build)


def _use_acp(monkeypatch, fakes, mode):
    monkeypatch.setattr(agents, "_opencode_command",
                        lambda agent: [sys.executable, str(fakes["acp"]), mode])


def _recorded(fakes):
    return json.loads(fakes["record"].read_text(encoding="utf-8"))


def _acp_log(fakes):
    path = fakes["dir"] / "acp.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _alive(pid):
    if os.name == "nt":
        import ctypes as c
        from ctypes import wintypes as w
        api = c.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes, api.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        api.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
        api.CloseHandle.argtypes = [w.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = w.DWORD()
            return bool(api.GetExitCodeProcess(handle, c.byref(code))) and code.value == 259
        finally:
            api.CloseHandle(handle)
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists() and stat.read_text().split(") ", 1)[1].startswith("Z"):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def _wait_dead(pid, seconds=5.0):
    deadline = time.monotonic() + seconds
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return not _alive(pid)


def _kill(pid):
    if not _alive(pid):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        import signal
        os.kill(pid, signal.SIGKILL)


# ---- command lines ----

def test_claude_command_line_has_no_tools_and_keeps_the_words_off_it(fakes, tmp_path):
    argv = agents._claude_ask_command(CLAUDE, "haiku", str(tmp_path))
    assert argv[:7] == [CLAUDE.path, "-p", "--input-format", "stream-json",
                        "--output-format", "stream-json", "--verbose"]
    assert argv[argv.index("--system-prompt") + 1] == agents.ASK_INSTRUCTION
    assert argv[argv.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in argv
    mcp = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text(encoding="utf-8"))
    assert mcp == {"mcpServers": {}}
    settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text(encoding="utf-8"))
    assert settings == {"disableAllHooks": True, "alwaysThinkingEnabled": False}
    assert "--no-session-persistence" in argv and "--disable-slash-commands" in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "--bare" not in argv and "--safe-mode" not in argv
    assert not any("2+2" in arg for arg in argv)


def test_claude_command_line_follows_its_help(fakes, monkeypatch, tmp_path):
    monkeypatch.setattr(agents, "_help", lambda agent, *args: "--tools --model")
    argv = agents._claude_ask_command(CLAUDE, "", str(tmp_path))
    assert "--no-session-persistence" not in argv and "--model" not in argv
    monkeypatch.setattr(agents, "_help", lambda agent, *args: "--print --model")
    with pytest.raises(agents._TooOld, match="claude update"):
        agents._claude_ask_command(CLAUDE, "", str(tmp_path))


def test_codex_command_line_ignores_user_config_and_switches_tools_off(fakes, monkeypatch, tmp_path):
    monkeypatch.setattr(installed_agents, "configured_default_model", lambda agent_id: "gpt-6-sol")
    argv = agents._codex_ask_command(CODEX, "", str(tmp_path))
    assert argv[:3] == [CODEX.path, "exec", "--json"] and argv[-1] == "-"
    for flag in ("--ephemeral", "--skip-git-repo-check", "--ignore-user-config"):
        assert flag in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    assert argv[argv.index("-C") + 1] == installed_agents.agent_workspace_dir()
    overrides = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]
    assert {"approval_policy=never", "web_search=disabled", "project_doc_max_bytes=0"} <= set(overrides)
    instructions = next(o for o in overrides if o.startswith("model_instructions_file="))
    path = instructions.split("=", 1)[1]
    assert Path(path).read_text(encoding="utf-8") == agents.ASK_INSTRUCTION
    assert "\\" not in path
    # Every tool feature this Codex knows is off, enabled or not; unknown
    # names would stop the run.
    disabled = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--disable"]
    assert disabled == ["shell_tool", "code_mode_host", "apps", "js_repl"]
    assert argv[argv.index("-m") + 1] == "gpt-6-sol"  # the user's default, since config is ignored


@pytest.mark.parametrize("listing", ["", "Error: config.toml line 3: expected a value"])
def test_codex_without_a_feature_list_still_drops_the_shell(fakes, monkeypatch, tmp_path, listing):
    monkeypatch.setattr(agents, "_help", lambda agent, *args:
                        listing if args == ("features", "list") else CODEX_EXEC_HELP)
    argv = agents._codex_ask_command(CODEX, "gpt-6-luna", str(tmp_path))
    assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--disable"] == ["shell_tool"]
    assert argv[argv.index("-m") + 1] == "gpt-6-luna"


def test_an_old_codex_is_refused_before_it_runs(fakes, monkeypatch):
    monkeypatch.setattr(agents, "_help", lambda agent, *args: "--json --ephemeral")
    monkeypatch.setattr(agents, "_run_headless", lambda *a, **k: pytest.fail("ran"))
    result = agents.ask("codex", WORDS)
    assert not result.ok and "too old" in result.message


# ---- headless runs ----

def test_claude_answers_from_its_json_result(fakes, monkeypatch):
    _use_cli(monkeypatch, fakes, "_claude_ask_command", "claude")
    result = agents.ask("claude_code", WORDS, model="haiku")
    assert result.ok and result.message == "Claude Code answered"
    assert result.output == ("Answer: " + WORDS).strip()
    seen = _recorded(fakes)
    # On stdin as one stream-json user message, intact.
    assert json.loads(seen["prompt"]) == {"type": "user", "message": {
        "role": "user", "content": [{"type": "text", "text": WORDS}]}}
    assert not any("2+2" in arg for arg in seen["argv"])
    assert os.path.samefile(seen["cwd"], installed_agents.agent_workspace_dir())


PNG = b"\x89PNG\r\n\x1a\nnot-really-a-picture"


def test_claude_gets_the_screenshot_as_an_image_block(fakes, monkeypatch):
    _use_cli(monkeypatch, fakes, "_claude_ask_command", "claude")
    result = agents.ask("claude_code", WORDS, image=PNG)
    assert result.ok and result.output == ("Answer: " + WORDS).strip()
    content = json.loads(_recorded(fakes)["prompt"])["message"]["content"]
    assert content == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": base64.b64encode(PNG).decode("ascii")}},
        {"type": "text", "text": WORDS},
    ]


def test_codex_attaches_the_screenshot_and_removes_it_after(fakes, monkeypatch):
    _use_cli(monkeypatch, fakes, "_codex_ask_command", "codex")
    seen_files = []
    real_run = agents._run_headless

    def run(argv, prompt, **kwargs):
        image = argv[argv.index("--image") + 1]
        seen_files.append((image, Path(image).read_bytes()))
        return real_run(argv, prompt, **kwargs)

    monkeypatch.setattr(agents, "_run_headless", run)
    result = agents.ask("codex", WORDS, image=PNG)
    assert result.ok
    argv = _recorded(fakes)["argv"]
    # Right after exec, so "-" (stdin) is never read as another image.
    assert argv[:4] == ["exec", "--json", "--image", seen_files[0][0]] and argv[-1] == "-"
    assert seen_files[0][1] == PNG
    assert not Path(seen_files[0][0]).exists()


def test_a_signed_out_claude_says_how_to_sign_in(fakes, monkeypatch):
    _use_cli(monkeypatch, fakes, "_claude_ask_command", "claude-signed-out")
    result = agents.ask("claude_code", WORDS)
    assert not result.ok
    assert result.message == ("Claude Code is signed out. "
                              + installed_agents.sign_in_hint("claude_code"))


def test_an_empty_answer_is_not_success(fakes, monkeypatch):
    _use_cli(monkeypatch, fakes, "_claude_ask_command", "claude-empty")
    result = agents.ask("claude_code", WORDS)
    assert not result.ok and result.message == "Claude Code didn't answer."


def test_codex_answers_despite_a_retried_hiccup(fakes, monkeypatch):
    _use_cli(monkeypatch, fakes, "_codex_ask_command", "codex")
    result = agents.ask("codex", WORDS)
    assert result.ok and result.output == ("Answer: " + WORDS).strip()
    assert _recorded(fakes)["prompt"] == WORDS


def test_codex_errors_show_the_providers_message(fakes, monkeypatch):
    _use_cli(monkeypatch, fakes, "_codex_ask_command", "codex-fail")
    result = agents.ask("codex", WORDS)
    assert not result.ok and result.message == "Codex couldn't answer: The 'x' model is not supported."


def test_a_crash_shows_the_last_error_line(fakes, monkeypatch):
    _use_cli(monkeypatch, fakes, "_claude_ask_command", "crash")
    result = agents.ask("claude_code", WORDS)
    assert result.message == ("Claude Code stopped before answering: "
                              "error: unknown option '--frobnicate'")


def test_a_missing_program_reads_as_not_started(fakes, monkeypatch, tmp_path):
    real = agents._claude_ask_command
    monkeypatch.setattr(agents, "_claude_ask_command", lambda agent, model, workdir: [
        str(tmp_path / "gone.exe"), *real(agent, model, workdir)[1:]])
    assert agents.ask("claude_code", WORDS).message == "Claude Code couldn't be started."


def test_a_timeout_kills_the_agent_and_its_helpers(fakes, monkeypatch):
    _use_cli(monkeypatch, fakes, "_claude_ask_command", "hang")
    pid_file = fakes["dir"] / "helper.pid"
    started = time.monotonic()
    result = agents.ask("claude_code", WORDS, timeout_s=5.0)
    elapsed = time.monotonic() - started
    pid = int(pid_file.read_text()) if pid_file.exists() else None
    try:
        assert not result.ok and result.message == "Claude Code didn't answer in time."
        assert elapsed < 15
        assert pid is not None and _wait_dead(pid)
    finally:
        if pid is not None:
            _kill(pid)


def test_cancel_kills_the_agent_and_its_helpers(fakes, monkeypatch):
    _use_cli(monkeypatch, fakes, "_codex_ask_command", "hang")
    pid_file = fakes["dir"] / "helper.pid"
    cancel = threading.Event()

    def cancel_once_running():
        deadline = time.monotonic() + 20
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        cancel.set()

    threading.Thread(target=cancel_once_running, daemon=True).start()
    result = agents.ask("codex", WORDS, timeout_s=60, cancel=cancel)
    pid = int(pid_file.read_text()) if pid_file.exists() else None
    try:
        assert not result.ok and result.message == "Stopped asking Codex."
        assert result.stopped  # the action runtime reports it as canceled
        assert pid is not None and _wait_dead(pid)
    finally:
        if pid is not None:
            _kill(pid)


@pytest.mark.parametrize("agent_id, text, model, message", [
    ("pi", WORDS, "", "Choose Claude Code, Codex or OpenCode"),
    ("claude_code", " ", "", "nothing to ask Claude Code"),
    ("claude_code", WORDS, "haiku&calc", "model name can't be used"),
])
def test_refusals_run_nothing(fakes, monkeypatch, agent_id, text, model, message):
    monkeypatch.setattr(agents, "_run_headless", lambda *a, **k: pytest.fail("ran"))
    result = agents.ask(agent_id, text, model=model)
    assert not result.ok and message in result.message


def test_a_missing_agent_is_named(fakes):
    fakes["installed"].clear()
    assert agents.ask("codex", WORDS).message == "Codex isn't installed."


# ---- OpenCode over ACP ----

def test_opencode_answers_in_a_locked_down_session_it_then_deletes(fakes, monkeypatch):
    _use_acp(monkeypatch, fakes, "ok")
    result = agents.ask("opencode_cli", WORDS)
    assert result.ok and result.message == "OpenCode answered"
    assert result.output == ("Answer: " + WORDS).strip()
    log = _acp_log(fakes)
    overlay = log[0]["overlay"]
    assert set(overlay) == {"agents"}  # no MCP servers of ours or anyone's
    for persona in ("build", "plan"):
        assert overlay["agents"][persona]["system"] == agents.ASK_INSTRUCTION
        assert overlay["agents"][persona]["permissions"] == [
            {"action": "*", "resource": "*", "effect": "deny"}]
    assert log[0]["project_config_off"] == "1"
    methods = [entry.get("method") for entry in log[1:]]
    assert methods == ["initialize", "session/new", "session/prompt", "session/delete"]
    new = log[2]["params"]
    assert new["mcpServers"] == [] and os.path.samefile(new["cwd"], installed_agents.agent_workspace_dir())
    assert log[3]["params"]["prompt"] == [{"type": "text", "text": WORDS}]


@pytest.mark.parametrize("mode, sent", [("image", True), ("ok", False)])
def test_opencode_gets_the_screenshot_only_when_it_takes_images(fakes, monkeypatch, mode, sent):
    _use_acp(monkeypatch, fakes, mode)
    result = agents.ask("opencode_cli", WORDS, image=PNG)
    assert result.ok and result.output == ("Answer: " + WORDS).strip()
    (prompt,) = [e for e in _acp_log(fakes) if e.get("method") == "session/prompt"]
    image = {"type": "image", "mimeType": "image/png",
             "data": base64.b64encode(PNG).decode("ascii")}
    text = {"type": "text", "text": WORDS}
    assert prompt["params"]["prompt"] == ([image, text] if sent else [text])


def test_opencode_switches_to_the_chosen_model(fakes, monkeypatch):
    _use_acp(monkeypatch, fakes, "ok")
    assert agents.ask("opencode_cli", WORDS, model="prov/slow").ok
    switch = [e for e in _acp_log(fakes) if e.get("method") == "session/set_config_option"]
    assert [s["params"]["value"] for s in switch] == ["prov/slow"]


def test_opencode_uses_the_users_default_only_when_it_is_offered(fakes, monkeypatch):
    _use_acp(monkeypatch, fakes, "ok")
    monkeypatch.setattr(installed_agents, "configured_default_model", lambda agent_id: "gone/model")
    assert agents.ask("opencode_cli", WORDS).ok
    assert not [e for e in _acp_log(fakes) if e.get("method") == "session/set_config_option"]


def test_opencode_refuses_a_model_it_does_not_offer(fakes, monkeypatch):
    _use_acp(monkeypatch, fakes, "ok")
    result = agents.ask("opencode_cli", WORDS, model="prov/missing")
    assert not result.ok and "no model prov/missing" in result.message
    methods = [e.get("method") for e in _acp_log(fakes)[1:]]
    assert "session/prompt" not in methods and methods[-1] == "session/delete"


def test_opencode_permission_requests_are_rejected(fakes, monkeypatch):
    _use_acp(monkeypatch, fakes, "permission")
    assert agents.ask("opencode_cli", WORDS).ok
    reply = next(e for e in _acp_log(fakes) if e.get("method") is None and e.get("result"))
    assert reply["result"] == {"outcome": {"outcome": "selected", "optionId": "no"}}


def test_opencode_that_cannot_start_says_so(fakes, monkeypatch, tmp_path):
    monkeypatch.setattr(agents, "_opencode_command", lambda agent: [str(tmp_path / "gone.exe")])
    assert agents.ask("opencode_cli", WORDS).message == "OpenCode couldn't be started."


def test_opencode_auth_errors_say_how_to_sign_in(fakes, monkeypatch):
    _use_acp(monkeypatch, fakes, "auth")
    result = agents.ask("opencode_cli", WORDS)
    assert result.message == "OpenCode is signed out. " + installed_agents.sign_in_hint("opencode_cli")


def test_opencode_timeout_and_cancel_end_the_session(fakes, monkeypatch):
    _use_acp(monkeypatch, fakes, "hang")
    started = time.monotonic()
    timed_out = agents.ask("opencode_cli", WORDS, timeout_s=2.0)
    assert timed_out.message == "OpenCode didn't answer in time."
    assert time.monotonic() - started < 15
    methods = [e.get("method") for e in _acp_log(fakes)]
    assert "session/cancel" in methods and methods[-1] == "session/delete"
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    canceled = agents.ask("opencode_cli", WORDS, timeout_s=60, cancel=cancel)
    assert canceled.message == "Stopped asking OpenCode." and canceled.stopped
