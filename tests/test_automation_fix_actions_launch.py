"""launch.py review fixes: Linux terminal scripts, xdg-open, link guards, child env.

Nothing real opens: Popen, run, the Windows opener and the handler lookup are fakes.
"""

import os
import shlex
import subprocess
import sys

import pytest

from services.actions import launch
from tests import test_automation_agents_launch as base
from tests.test_automation_r3c_child_env import load_dotenv

WORDS = base.WORDS  # shell metacharacters, quotes, a newline and option-like words
fresh_handler_cache = base.fresh_handler_cache


@pytest.fixture
def dotenv_key(monkeypatch, tmp_path):
    """A key OpenWhisper loaded from its .env; no child may inherit it."""
    load_dotenv(monkeypatch, tmp_path, {"OPENAI_API_KEY": "sk-from-openwhisper-dotenv"})
    monkeypatch.setenv("USER_OWN_VAR", "kept")


def _clean(env):
    return env is not None and "OPENAI_API_KEY" not in env and env.get("USER_OWN_VAR") == "kept"


class Proc:
    """A started process: ``code`` None is still running."""

    started = []

    def __init__(self, args, code=0, **kwargs):
        self.args, self.kwargs, self.code = list(args), kwargs, code
        self.pid = 4242
        self.waited = []

    def wait(self, timeout=None):
        self.waited.append(timeout)
        if self.code is None:
            if timeout is not None:
                raise subprocess.TimeoutExpired(self.args, timeout)
            return 0
        return self.code

    def poll(self):
        return self.code


@pytest.fixture
def popen(monkeypatch):
    """Popen that records every start; ``codes[program name]`` sets how it ends."""
    started, codes, failing = [], {}, set()

    def fake(args, **kwargs):
        name = os.path.basename(args[0])
        if name in failing:
            raise OSError("can't start")
        proc = Proc(args, codes.get(name, 0), **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(launch.subprocess, "Popen", fake)
    return started, codes, failing


# -- finding 10 and item 3: Linux terminals get only a script's path ---------------------------


def _script_of(proc):
    path = proc.args[-1]
    with open(path, encoding="utf-8") as handle:
        return path, handle.read()


def test_linux_terminals_get_one_private_script_with_every_argument_quoted(
        monkeypatch, popen, tmp_path):
    started, _codes, _failing = popen
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(launch.shutil, "which", base._which({"xterm"}))
    folder = str(tmp_path / "it's here")
    os.mkdir(folder)
    argv = ["/home/u/.local/bin/claude", "--", WORDS]

    launch._linux_terminal(argv, folder, {"A": "1"})

    (proc,) = started
    assert proc.args[:3] == ["/usr/bin/xterm", "-e", "/bin/sh"] and len(proc.args) == 4
    path, script = _script_of(proc)
    try:
        assert WORDS not in proc.args and not any("claude" in a for a in proc.args)
        lines = script.split("\n")
        assert lines[0] == "#!/bin/sh" and lines[1] == 'rm -f -- "$0"'
        assert shlex.split(lines[2]) == ["cd", "--", folder, "||", "exit", "1"]
        exec_part = script.split("\n", 3)[3]
        assert exec_part.startswith("exec ")
        assert shlex.split(exec_part[len("exec "):]) == argv
        if os.name != "nt":
            assert os.stat(path).st_mode & 0o777 == 0o700
        assert proc.kwargs["env"] == {"A": "1"} and proc.kwargs["start_new_session"] is True
    finally:
        os.remove(path)


@pytest.mark.parametrize("style_terminal, prefix", [
    ("gnome-terminal", ["--"]), ("kitty", []), ("wezterm", ["start", "--"]),
    ("konsole", ["-e"]), ("xfce4-terminal", ["-x"]),
])
def test_every_terminal_style_takes_just_the_script(monkeypatch, popen, tmp_path,
                                                    style_terminal, prefix):
    started, _codes, _failing = popen
    monkeypatch.setenv("TERMINAL", style_terminal)
    monkeypatch.setattr(launch.shutil, "which", base._which({style_terminal}))

    launch._linux_terminal(["/bin/agent", WORDS], str(tmp_path), None)

    proc = started[0]
    path, _script = _script_of(proc)
    os.remove(path)
    assert proc.args == [f"/usr/bin/{style_terminal}", *prefix, "/bin/sh", path]


def test_a_terminal_that_dies_at_once_falls_through_to_the_next(monkeypatch, popen, tmp_path):
    started, codes, failing = popen
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(launch.shutil, "which",
                        base._which({"x-terminal-emulator", "gnome-terminal", "konsole", "xterm"}))
    failing.add("x-terminal-emulator")  # doesn't start
    codes["gnome-terminal"] = 127       # starts, then dies (the bundle's libraries)
    codes["konsole"] = None             # keeps running: its window is open

    launch._linux_terminal(["/bin/agent", WORDS], str(tmp_path), None)

    assert [os.path.basename(p.args[0]) for p in started] == ["gnome-terminal", "konsole"]
    assert started[1].args[-1] == started[0].args[-1]  # the same script
    os.remove(started[1].args[-1])


def test_a_launcher_that_hands_off_and_exits_cleanly_counts(monkeypatch, popen, tmp_path):
    started, codes, _failing = popen
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(launch.shutil, "which", base._which({"gnome-terminal", "xterm"}))
    codes["gnome-terminal"] = 0  # gnome-terminal asks its server for a window and exits

    launch._linux_terminal(["/bin/agent", "hi"], str(tmp_path), None)

    assert len(started) == 1
    os.remove(started[0].args[-1])


def test_when_no_terminal_opens_the_script_is_removed(monkeypatch, popen, tmp_path):
    started, codes, _failing = popen
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(launch.shutil, "which", base._which({"xterm"}))
    codes["xterm"] = 1

    with pytest.raises(launch.LaunchError, match="terminal"):
        launch._linux_terminal(["/bin/agent", "hi"], str(tmp_path), None)
    assert not os.path.exists(started[0].args[-1])


# -- finding 11 and item 4: xdg-open / open, and the environment of every child ------------


@pytest.mark.parametrize("code, opened", [(None, True), (0, True), (3, False), (4, False)])
def test_xdg_open_counts_as_opened_while_its_handler_runs(popen, dotenv_key, code, opened):
    started, codes, _failing = popen
    codes["xdg-open"] = code

    if opened:
        launch._open_with(["xdg-open", "https://example.com/?q=x"])
    else:
        with pytest.raises(launch.LaunchError, match="No app"):
            launch._open_with(["xdg-open", "https://example.com/?q=x"])

    (proc,) = started
    assert proc.waited[0] == launch.OPEN_WAIT_S  # then reaped in the background if running
    assert proc.kwargs["start_new_session"] is True
    assert proc.kwargs["stdin"] == subprocess.DEVNULL and "shell" not in proc.kwargs
    assert _clean(proc.kwargs["env"])


def test_open_uri_uses_open_or_xdg_open_off_windows(monkeypatch):
    seen = []
    monkeypatch.setattr(launch, "_open_with", seen.append)
    monkeypatch.setattr(launch, "handler_available", lambda scheme: True)
    if sys.platform == "win32":
        pytest.skip("Windows opens links through their association")
    launch.open_uri("https://example.com/")
    assert seen == [["open" if sys.platform == "darwin" else "xdg-open", "https://example.com/"]]


def test_a_terminal_without_an_env_gets_the_users_without_openwhispers_keys(
        monkeypatch, dotenv_key, tmp_path):
    seen = []
    for name in ("_windows_terminal", "_linux_terminal"):
        monkeypatch.setattr(launch, name, lambda args, cwd, env: seen.append(env))
    monkeypatch.setattr(launch, "_macos_terminal", lambda args, cwd: seen.append(None))
    launch.open_in_terminal(["/bin/agent", "hi"], cwd=str(tmp_path))
    if sys.platform != "darwin":
        assert _clean(seen[0])


def test_xdg_mime_runs_without_openwhispers_keys(monkeypatch, dotenv_key):
    seen = []
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/usr/bin/xdg-mime")

    def run(args, **kwargs):
        seen.append(kwargs.get("env"))
        return subprocess.CompletedProcess(args, 0, "claude.desktop\n", "")

    monkeypatch.setattr(launch.subprocess, "run", run)
    assert launch._linux_handler("claude") is True
    assert _clean(seen[0])


def test_macos_terminal_runs_open_without_openwhispers_keys(monkeypatch, dotenv_key, tmp_path):
    seen = []

    def run(args, **kwargs):
        seen.append(kwargs.get("env"))
        os.remove(args[3])
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(launch.subprocess, "run", run)
    launch._macos_terminal(["/usr/local/bin/claude", "hi"], str(tmp_path))
    assert _clean(seen[0])


# -- finding 12: link length and handler guards ------------------------------------------------


@pytest.fixture
def opener(monkeypatch):
    """Records links handed to the system, on any platform; handlers by scheme."""
    opened, handlers = [], {}
    monkeypatch.setattr(launch, "_open_with", lambda command: opened.append(command[-1]))
    monkeypatch.setattr(launch, "_windows_open", opened.append)
    monkeypatch.setattr(launch, "handler_available",
                        lambda scheme: handlers.get(scheme, False))
    return opened, handlers


def test_a_link_too_long_for_any_handler_says_so(opener):
    opened, handlers = opener
    handlers["claude"] = True
    uri = "claude://claude.ai/new?q=" + "a" * launch.LINK_MAX_CHARS
    with pytest.raises(launch.LaunchError) as raised:
        launch.open_uri(uri)
    assert "too long" in str(raised.value) and "No app" not in str(raised.value)
    assert opened == []


@pytest.mark.parametrize("scheme, app", [
    ("claude", "the Claude app"), ("codex", "the ChatGPT app"), ("claude-cli", "Claude Code"),
])
def test_a_missing_handler_names_the_app_to_install(opener, scheme, app):
    opened, _handlers = opener
    with pytest.raises(launch.LaunchError) as raised:
        launch.open_uri(f"{scheme}://new?q=hello")
    assert f"{scheme}:" in str(raised.value) and app in str(raised.value)
    assert opened == []


def test_an_unknown_scheme_without_a_handler_is_not_opened(opener):
    opened, _handlers = opener
    with pytest.raises(launch.LaunchError, match="x-notes:"):
        launch.open_uri("x-notes://add?text=hi")
    assert opened == []


def test_web_links_and_unknown_handlers_still_open(opener):
    opened, handlers = opener
    handlers["claude"] = None  # can't tell: let the system try
    launch.open_uri("https://chatgpt.com/?q=hi")
    launch.open_uri("claude://claude.ai/new?q=hi")
    assert opened == ["https://chatgpt.com/?q=hi", "claude://claude.ai/new?q=hi"]


def test_the_limits_are_the_ones_agent_links_use():
    from services.actions import agents

    assert agents.LINK_MAX_CHARS == launch.LINK_MAX_CHARS == 30_000
    assert agents.CLAUDE_APP_TEXT_MAX == launch.CLAUDE_APP_TEXT_MAX == 14_000


# -- item 2: the association strings it asks for ---------------------------------------------------


def test_the_handler_lookup_asks_for_the_delegate_execute_handler():
    assert launch._ASSOCSTR_DELEGATEEXECUTE == 18  # ASSOCSTR_SUPPORTED_URI_PROTOCOLS is 19
    assert not hasattr(launch, "_ASSOCSTR_APPID")
