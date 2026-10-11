"""Opening links on Windows (review round 2): handlers that are COM objects, and
classic handlers started in the user's environment rather than OpenWhisper's.

Nothing real opens: the association lookup, Popen and the shell are fakes. The
association tables are what AssocQueryStringW answers on a real Windows 11.
"""

import sys
import threading

import pytest

from services import credentials
from services.actions import launch
from tests import test_automation_agents_launch as base

fresh_handler_cache = base.fresh_handler_cache
FakeAssoc = base.FakeAssoc
NO_ASSOCIATION = base.NO_ASSOCIATION
COMMAND, EXECUTABLE, DELEGATE_EXECUTE = 1, 2, 18  # ASSOCSTR_*
APPX_PROTOCOL = "{A56A841F-E974-45C1-8001-7E3F8A085917}"
WINDOWS_PAGES = "{4ed3a719-cea8-4bd9-910d-e252f997afc2}"
CLAUDE_CODE = r"C:\Users\u\.local\bin\claude.exe"

#: How schemes are registered: a classic command, a packaged app, Windows' own pages.
REGISTERED = FakeAssoc({
    ("claude-cli", COMMAND): (0, f'"{CLAUDE_CODE}" --handle-uri "%1"'),
    ("claude-cli", EXECUTABLE): (0, CLAUDE_CODE),
    ("https", COMMAND): (0, r'"C:\Program Files\Browser\browser.exe" --single-argument %1'),
    ("https", EXECUTABLE): (0, r"C:\Program Files\Browser\browser.exe"),
    ("codex", DELEGATE_EXECUTE): (0, APPX_PROTOCOL),
    ("ms-settings", DELEGATE_EXECUTE): (0, WINDOWS_PAGES),
    # Both registered: ShellExecute uses the COM object.
    ("tel", COMMAND): (0, r'C:\WINDOWS\system32\OpenWith.exe "%1"'),
    ("tel", DELEGATE_EXECUTE): (0, "{e44e9428-bdbc-4987-a099-40dc8fd255e7}"),
})


@pytest.fixture
def dotenv_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-openwhisper-dotenv")
    monkeypatch.setenv("USER_OWN_VAR", "kept")
    monkeypatch.setattr(credentials, "dotenv_injected_names", lambda: {"ANTHROPIC_API_KEY"})


@pytest.fixture
def windows(monkeypatch):
    """The Windows opener's two ways out, recorded: CreateProcess and ShellExecute."""
    started, shell = [], []

    class Proc:
        def __init__(self, args, **kwargs):
            started.append((args, kwargs))

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(launch.subprocess, "Popen", Proc)
    monkeypatch.setattr(launch, "_shell_open", shell.append)
    monkeypatch.setattr(launch, "_assoc_query", REGISTERED)
    return started, shell


# -- #3: DelegateExecute handlers count -------------------------------------------------


@pytest.mark.parametrize("scheme", ["ms-settings", "codex", "tel"])
def test_a_com_handler_counts(monkeypatch, scheme):
    monkeypatch.setattr(launch, "_assoc_query", REGISTERED)
    assert launch._windows_handler(scheme) is True


@pytest.mark.parametrize("table, expected", [
    ({}, False),                                                        # nothing at all
    ({("bingmaps", 20): (0, "bingmaps")}, False),                       # a bare key
    ({("bingmaps", COMMAND): (0x80004005, ""),
      ("bingmaps", DELEGATE_EXECUTE): (0x80004005, "")}, None),         # lookup failed
])
def test_only_no_handler_at_all_is_refused(monkeypatch, table, expected):
    monkeypatch.setattr(launch, "_assoc_query", FakeAssoc(table))
    assert launch._windows_handler("bingmaps") is expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows opens links through associations")
@pytest.mark.parametrize("uri", ["ms-settings:display", "ms-settings:sound"])
def test_windows_own_pages_open(monkeypatch, uri):
    opened = []
    monkeypatch.setattr(launch, "_assoc_query", REGISTERED)
    monkeypatch.setattr(launch.os, "startfile", opened.append)
    launch.open_uri(uri)
    assert opened == [uri]


def _delegate_with_com(scheme):
    """The DelegateExecute answer on a fresh thread that has COM, as ShellExecute has."""
    import ctypes

    answers = []

    def look():
        ole32 = ctypes.WinDLL("ole32")
        initialized = ole32.CoInitializeEx(None, 0x6)
        try:
            answers.append(launch._assoc_query(scheme, DELEGATE_EXECUTE))
        finally:
            if initialized >= 0:
                ole32.CoUninitialize()

    worker = threading.Thread(target=look)
    worker.start()
    worker.join(10)
    return answers[0]


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows association API")
def test_a_worker_thread_sees_windows_own_packaged_protocols():
    # Windows reads some packaged apps' protocols (ms-actioncenter:, the
    # notification centre) only through COM; a worker thread without COM was
    # told they had no handler. Read-only: nothing is opened.
    scheme = "ms-actioncenter"
    if _delegate_with_com(scheme)[0] != 0:
        pytest.skip(f"{scheme}: isn't registered on this computer")
    found = []
    worker = threading.Thread(target=lambda: found.append(launch._windows_handler(scheme)))
    worker.start()
    worker.join(10)
    assert found == [True]


# -- N2: a classic handler starts in the user's environment -------------------------------


def test_a_classic_handler_starts_in_the_users_environment(windows, dotenv_key):
    started, shell = windows
    launch._windows_open("claude-cli://open?q=hello%20there", "claude-cli")

    ((command, kwargs),) = started
    assert command == f'"{CLAUDE_CODE}" --handle-uri "claude-cli://open?q=hello%20there"'
    assert kwargs["executable"] == CLAUDE_CODE
    env = kwargs["env"]
    assert "ANTHROPIC_API_KEY" not in env and env["USER_OWN_VAR"] == "kept"
    # Like ShellExecute: a console program gets its own console, and nothing
    # of OpenWhisper's (no pipes, no inherited handles).
    assert kwargs["creationflags"] == launch._CREATE_NEW_CONSOLE
    assert not {"stdin", "stdout", "stderr", "shell"} & set(kwargs)
    assert shell == []


def test_a_web_link_reaches_the_browser_the_same_way(windows, dotenv_key):
    started, shell = windows
    uri = "https://chatgpt.com/?q=caf%C3%A9%20%22quoted%22"
    launch._windows_open(uri, "https")
    ((command, kwargs),) = started
    assert command.endswith(f"--single-argument {uri}")
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]


@pytest.mark.parametrize("scheme", ["codex", "ms-settings", "tel"])
def test_a_com_handler_is_left_to_windows(windows, scheme):
    # Windows activates the app (a packaged app, Settings): it never starts
    # from this process, so it doesn't get this process's environment.
    started, shell = windows
    launch._windows_open(f"{scheme}:x", scheme)
    assert started == [] and shell == [f"{scheme}:x"]


def test_an_unknown_handler_is_left_to_windows(windows, monkeypatch):
    started, shell = windows
    monkeypatch.setattr(launch, "_assoc_query", FakeAssoc({
        ("odd", COMMAND): (0x80004005, ""), ("odd", DELEGATE_EXECUTE): (0x80004005, "")}))
    launch._windows_open("odd:x", "odd")
    assert started == [] and shell == ["odd:x"]


def test_a_handler_that_wont_start_says_so(windows, monkeypatch):
    def fail(args, **kwargs):
        raise FileNotFoundError(2, "not found")

    monkeypatch.setattr(launch.subprocess, "Popen", fail)
    with pytest.raises(launch.LaunchError, match="No app"):
        launch._windows_open("claude-cli://open?q=hi", "claude-cli")


def test_the_shell_failing_says_so(windows, monkeypatch):
    def fail(uri):
        raise OSError(1155, "no association")

    monkeypatch.setattr(launch, "_shell_open", fail)
    with pytest.raises(launch.LaunchError, match="No app"):
        launch._windows_open("codex://new?prompt=hi", "codex")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows opens links through associations")
def test_open_uri_on_windows_uses_the_association(monkeypatch):
    seen = []
    monkeypatch.setattr(launch, "handler_available", lambda scheme: True)
    monkeypatch.setattr(launch, "_windows_open", lambda uri, scheme: seen.append((uri, scheme)))
    launch.open_uri("Claude-CLI://open?q=hi")
    assert seen == [("Claude-CLI://open?q=hi", "claude-cli")]


# What ShellExecuteExW put on a recorder's command line for each template
# (Windows 11, link "s:x"): the link for %1 %L %V %D %0, nothing for %* and
# %2..%9, a percent sign for %%.
@pytest.mark.parametrize("template, expected", [
    ('"p.exe" "%1"', '"p.exe" "s:x"'),
    ('"p.exe" %1', '"p.exe" s:x'),
    ('"p.exe" "%L" %l', '"p.exe" "s:x" s:x'),
    ('"p.exe" --open-url -- "%1"', '"p.exe" --open-url -- "s:x"'),
    ('"p.exe" [%*] [%2] [%9]', '"p.exe" [] [] []'),
    ('"p.exe" [100%%] [%V] [%D] [%0]', '"p.exe" [100%] [s:x] [s:x] [s:x]'),
    ('"p.exe"', '"p.exe"'),  # ShellExecute doesn't add the link either
    ('"p.exe" "%1" %', '"p.exe" "s:x" %'),
])
def test_the_command_is_filled_in_as_shellexecute_does(template, expected):
    assert launch._fill_command(template, "s:x") == expected


def test_the_link_goes_in_as_it_is():
    link = r"s:a\1 %1 \\ $(x) & | ^ é 😀"
    assert launch._fill_command('"p.exe" "%1"', link) == f'"p.exe" "{link}"'
