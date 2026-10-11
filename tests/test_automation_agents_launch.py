"""Opening links and terminals for agent actions (services/actions/launch.py).

Nothing real opens: the Windows association lookup, AppKit, xdg-mime,
``open`` and every terminal are fakes; scripts are only written and read back.
"""
import os
import shlex
import subprocess
import sys
import types

import pytest

from services.actions import launch

NO_ASSOCIATION = 0x80070483
WORDS = "It's $(rm -rf ~) `id` \"quoted\" & | ; %PATH% ^\nsecond line -- --help"


@pytest.fixture(autouse=True)
def fresh_handler_cache():
    launch.forget_handlers()
    yield
    launch.forget_handlers()


class FakeAssoc:
    """``AssocQueryStringW`` from a table of (scheme, what) → (HRESULT, value)."""

    def __init__(self, table):
        self.table = table

    def __call__(self, scheme, what):
        return self.table.get((scheme, what), (NO_ASSOCIATION, ""))


class FakePopen:
    calls = []

    def __init__(self, args, **kwargs):
        FakePopen.calls.append((list(args), kwargs))
        self.pid = 4242

    def wait(self, timeout=None):
        return 0


@pytest.fixture
def popen(monkeypatch):
    FakePopen.calls = []
    monkeypatch.setattr(launch.subprocess, "Popen", FakePopen)
    return FakePopen.calls


# ---- handler detection: Windows ----

def test_windows_classic_handler_counts_only_while_its_program_exists(monkeypatch, tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(launch, "_assoc_query", FakeAssoc({
        ("claude-cli", launch._ASSOCSTR_COMMAND): (0, f'"{exe}" --handle-uri "%1"'),
        ("claude-cli", launch._ASSOCSTR_EXECUTABLE): (0, str(exe)),
    }))
    assert launch._windows_handler("claude-cli") is True
    exe.unlink()  # an uninstall that left its registration behind
    assert launch._windows_handler("claude-cli") is False


def test_windows_packaged_app_protocol_counts(monkeypatch):
    # MSIX apps (the ChatGPT app's codex://) leave only a bare "URL Protocol"
    # key; the association resolves to the AppX protocol handler, a COM object.
    monkeypatch.setattr(launch, "_assoc_query", FakeAssoc({
        ("codex", launch._ASSOCSTR_DELEGATEEXECUTE): (0, "{A56A841F-E974-45C1-8001-7E3F8A085917}"),
    }))
    assert launch._windows_handler("codex") is True


@pytest.mark.parametrize("table, expected", [
    ({}, False),                                                         # unknown scheme
    ({("stale", launch._ASSOCSTR_COMMAND): (NO_ASSOCIATION, ""),
      ("stale", launch._ASSOCSTR_DELEGATEEXECUTE): (0x80070490, "")}, False),  # key, no handler
    ({("stale", launch._ASSOCSTR_COMMAND): (0x80004005, ""),
      ("stale", launch._ASSOCSTR_DELEGATEEXECUTE): (0x80004005, "")}, None),   # lookup failed
])
def test_windows_without_a_handler(monkeypatch, table, expected):
    monkeypatch.setattr(launch, "_assoc_query", FakeAssoc(table))
    assert launch._windows_handler("stale") is expected


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows association API")
def test_windows_lookup_answers_for_an_unknown_scheme():
    assert launch._assoc_query("openwhisper-no-such-scheme", launch._ASSOCSTR_COMMAND)[0] \
        == NO_ASSOCIATION
    assert launch.handler_available("openwhisper-no-such-scheme") is False


# ---- handler detection: macOS and Linux ----

def _fake_appkit(monkeypatch, handled):
    class NSURL:
        @staticmethod
        def URLWithString_(text):
            return text

    class Workspace:
        def URLForApplicationToOpenURL_(self, url):
            return "file:///Applications/Claude.app" if url.split(":")[0] in handled else None

    class NSWorkspace:
        @staticmethod
        def sharedWorkspace():
            return Workspace()

    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(NSWorkspace=NSWorkspace))
    monkeypatch.setitem(sys.modules, "Foundation", types.SimpleNamespace(NSURL=NSURL))


def test_macos_asks_launch_services(monkeypatch):
    _fake_appkit(monkeypatch, {"claude"})
    assert launch._macos_handler("claude") is True
    assert launch._macos_handler("codex") is False


def test_macos_without_pyobjc_cannot_tell(monkeypatch):
    monkeypatch.setitem(sys.modules, "AppKit", None)
    assert launch._macos_handler("claude") is None


@pytest.mark.parametrize("which, returncode, stdout, expected", [
    ("/usr/bin/xdg-mime", 0, "claude-code-url-handler.desktop\n", True),
    ("/usr/bin/xdg-mime", 0, "\n", False),
    ("/usr/bin/xdg-mime", 1, "", None),
    (None, 0, "", None),
])
def test_linux_asks_xdg_mime(monkeypatch, which, returncode, stdout, expected):
    calls = []
    monkeypatch.setattr(launch.shutil, "which", lambda name: which if name == "xdg-mime" else None)

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout, "")

    monkeypatch.setattr(launch.subprocess, "run", run)
    assert launch._linux_handler("claude-cli") is expected
    if which:
        assert calls == [[which, "query", "default", "x-scheme-handler/claude-cli"]]


# ---- handler cache ----

@pytest.fixture
def platform_lookup(monkeypatch):
    """Every platform's lookup answers from ``answers`` and counts its calls."""
    calls = []
    answers = {}

    def lookup(scheme):
        calls.append(scheme)
        result = answers.get(scheme, False)
        if isinstance(result, Exception):
            raise result
        return result

    for name in ("_windows_handler", "_macos_handler", "_linux_handler"):
        monkeypatch.setattr(launch, name, lookup)
    return calls, answers


def test_handler_answers_are_cached_for_a_minute(monkeypatch, platform_lookup):
    calls, answers = platform_lookup
    answers["claude"] = True
    clock = [1000.0]
    monkeypatch.setattr(launch.time, "monotonic", lambda: clock[0])
    assert launch.handler_available("claude") is True
    assert launch.handler_available("Claude:") is True  # same scheme
    assert calls == ["claude"]
    clock[0] += launch.HANDLER_CACHE_S + 1
    answers["claude"] = False
    assert launch.handler_available("claude") is False
    assert calls == ["claude", "claude"]


def test_handler_lookup_never_raises(platform_lookup):
    calls, answers = platform_lookup
    answers["claude"] = OSError("boom")
    assert launch.handler_available("claude") is None


@pytest.mark.parametrize("scheme", ["", "has space", "1claude", "claude://x"])
def test_malformed_schemes_have_no_handler(platform_lookup, scheme):
    calls, _ = platform_lookup
    assert launch.handler_available(scheme) is False
    assert calls == []


# ---- terminals: common checks ----

def test_terminal_refuses_bad_input(tmp_path):
    with pytest.raises(launch.LaunchError):
        launch.open_in_terminal([])
    with pytest.raises(launch.LaunchError, match="folder"):
        launch.open_in_terminal(["agent", "hi"], cwd=str(tmp_path / "missing"))
    with pytest.raises(launch.LaunchError):
        launch.open_in_terminal(["agent", "a\x00b"], cwd=str(tmp_path))


def test_terminal_dispatches_with_home_as_the_default_folder(monkeypatch):
    seen = []
    for name in ("_windows_terminal", "_macos_terminal", "_linux_terminal"):
        monkeypatch.setattr(launch, name, lambda args, cwd, *rest: seen.append((args, cwd)))
    launch.open_in_terminal(["agent", WORDS])
    assert seen == [(["agent", WORDS], os.path.expanduser("~"))]


# ---- terminals: Windows ----

@pytest.mark.parametrize("program", ["claude.cmd", "codex.BAT", "opencode.ps1"])
def test_windows_refuses_scripts_that_would_parse_the_words(popen, tmp_path, program):
    with pytest.raises(launch.LaunchError, match="script"):
        launch._windows_terminal([str(tmp_path / program), WORDS], str(tmp_path), None)
    assert popen == []


def test_windows_refuses_non_programs(popen, tmp_path):
    with pytest.raises(launch.LaunchError):
        launch._windows_terminal([str(tmp_path / "claude"), "hi"], str(tmp_path), None)
    assert popen == []


def test_windows_starts_the_exe_in_a_new_console(popen, tmp_path):
    program = str(tmp_path / "claude.exe")
    launch._windows_terminal([program, "--", WORDS], str(tmp_path), {"A": "1"})
    (args, kwargs), = popen
    assert args == [program, "--", WORDS]
    assert kwargs["cwd"] == str(tmp_path) and kwargs["env"] == {"A": "1"}
    assert kwargs["creationflags"] == launch._CREATE_NEW_CONSOLE
    assert "shell" not in kwargs and "stdin" not in kwargs  # the console's own handles


def test_windows_refuses_a_command_line_over_the_limit(popen, tmp_path):
    with pytest.raises(launch.LaunchError, match="too long"):
        launch._windows_terminal([str(tmp_path / "claude.exe"), "x" * 40_000],
                                 str(tmp_path), None)
    assert popen == []


# ---- terminals: macOS ----

def test_macos_writes_a_one_shot_script_with_every_argument_quoted(monkeypatch, tmp_path):
    opened = []

    def run(args, **kwargs):
        opened.append(args)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(launch.subprocess, "run", run)
    folder = str(tmp_path / "it's here")
    os.mkdir(folder)
    argv = ["/opt/homebrew/bin/codex", "--", WORDS]
    launch._macos_terminal(argv, folder)
    (command,) = opened
    assert command[:3] == ["open", "-a", "Terminal"] and command[3].endswith(".command")
    with open(command[3], encoding="utf-8") as handle:
        script = handle.read()
    os.remove(command[3])
    lines = script.split("\n")
    assert lines[0] == "#!/bin/zsh -l"
    assert lines[1] == 'rm -f -- "$0"'
    assert shlex.split(lines[2]) == ["cd", "--", folder, "||", "exit", "1"]
    exec_part = script.split("\n", 3)[3]
    assert exec_part.startswith("exec ")
    assert shlex.split(exec_part[len("exec "):]) == argv


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_macos_script_is_private_and_executable(monkeypatch, tmp_path):
    seen = []

    def run(args, **kwargs):
        seen.append(os.stat(args[3]).st_mode & 0o777)
        os.remove(args[3])
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(launch.subprocess, "run", run)
    launch._macos_terminal(["/usr/local/bin/claude", "hi"], str(tmp_path))
    assert seen == [0o700]


def test_macos_failure_removes_the_script(monkeypatch, tmp_path):
    paths = []

    def run(args, **kwargs):
        paths.append(args[3])
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(launch.subprocess, "run", run)
    with pytest.raises(launch.LaunchError, match="Terminal"):
        launch._macos_terminal(["/usr/local/bin/claude", "hi"], str(tmp_path))
    assert not os.path.exists(paths[0])


# ---- terminals: Linux ----

def _which(available):
    return lambda name: f"/usr/bin/{name}" if name in available else None


def test_linux_prefers_terminal_env_with_its_own_arguments(monkeypatch):
    monkeypatch.setattr(launch.shutil, "which", _which({"kitty", "gnome-terminal"}))
    monkeypatch.setenv("TERMINAL", "kitty --single-instance")
    argv = ["/home/u/.local/bin/claude", "--", WORDS]
    commands = launch._terminal_commands(argv)
    assert commands[0] == ["/usr/bin/kitty", "--single-instance", *argv]
    assert commands[1] == ["/usr/bin/gnome-terminal", "--", *argv]
    assert commands[2] == ["/usr/bin/kitty", *argv]


@pytest.mark.parametrize("name, style", [
    ("alacritty", ["-e"]), ("xterm", ["-e"]), ("konsole", ["-e"]), ("ghostty", ["-e"]),
    ("foot", ["--"]), ("xfce4-terminal", ["-x"]), ("wezterm", ["start", "--"]),
    ("xdg-terminal-exec", []), ("x-terminal-emulator", ["-e"]),
])
def test_linux_terminal_styles(monkeypatch, name, style):
    monkeypatch.setattr(launch.shutil, "which", _which({name}))
    monkeypatch.setenv("TERMINAL", name)
    commands = launch._terminal_commands(["/bin/agent", WORDS])
    assert commands[0] == [f"/usr/bin/{name}", *style, "/bin/agent", WORDS]


def test_linux_tries_the_next_terminal_when_one_fails(monkeypatch, tmp_path):
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(launch.shutil, "which", _which({"gnome-terminal", "xterm"}))
    started = []

    class Popen(FakePopen):
        def __init__(self, args, **kwargs):
            if "gnome-terminal" in args[0]:
                raise OSError("no session bus")
            started.append((list(args), kwargs))

    monkeypatch.setattr(launch.subprocess, "Popen", Popen)
    launch._linux_terminal(["/bin/agent", WORDS], str(tmp_path), {"A": "1"})
    (args, kwargs), = started
    # Only the one-shot script's path: terminals re-parse what follows -e.
    script = args[2]
    os.remove(script)
    assert args == ["/usr/bin/xterm", "-e", script] and script.endswith(".sh")
    assert kwargs["cwd"] == str(tmp_path) and kwargs["env"] == {"A": "1"}
    assert kwargs["start_new_session"] is True and "shell" not in kwargs


def test_linux_without_a_terminal_says_so(monkeypatch, tmp_path):
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setattr(launch.shutil, "which", _which(set()))
    with pytest.raises(launch.LaunchError, match=r"\$TERMINAL"):
        launch._linux_terminal(["/bin/agent", "hi"], str(tmp_path), None)
