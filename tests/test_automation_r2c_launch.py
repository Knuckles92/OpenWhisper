"""Windows link handlers that are COM objects count (review round 2), and Linux
terminals run their one-shot script through /bin/sh.

Nothing real opens: the association lookup, Popen and the opener are fakes. The
association tables are what AssocQueryStringW answers on a real Windows 11.
How links open on Windows since review round 3: test_automation_r3c_launch.
"""

import os
import sys
import threading

import pytest

from services.actions import launch
from tests import test_automation_agents_launch as base
from tests import test_automation_fix_actions_launch as round1
from tests.test_automation_hooks_programs import wait_until

fresh_handler_cache = base.fresh_handler_cache
popen = round1.popen
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
    ("codex", DELEGATE_EXECUTE): (0, APPX_PROTOCOL),
    ("ms-settings", DELEGATE_EXECUTE): (0, WINDOWS_PAGES),
    # Both registered: ShellExecute uses the COM object.
    ("tel", COMMAND): (0, r'C:\WINDOWS\system32\OpenWith.exe "%1"'),
    ("tel", DELEGATE_EXECUTE): (0, "{e44e9428-bdbc-4987-a099-40dc8fd255e7}"),
})


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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows opens links through associations")
def test_open_uri_on_windows_opens_the_link_as_it_is(monkeypatch):
    seen, schemes = [], []
    monkeypatch.setattr(launch, "handler_available", lambda scheme: schemes.append(scheme) or True)
    monkeypatch.setattr(launch, "_windows_open", seen.append)
    launch.open_uri("Claude-CLI://open?q=hi")
    assert seen == ["Claude-CLI://open?q=hi"] and schemes == ["claude-cli"]


# -- N5: a Linux terminal runs the one-shot script through /bin/sh ------------------------


@pytest.mark.parametrize("terminal, prefix", [
    ("xterm", ["-e"]), ("gnome-terminal", ["--"]), ("kitty", []),
])
def test_a_linux_terminal_runs_the_script_through_sh(monkeypatch, popen, tmp_path,
                                                      terminal, prefix):
    # A noexec /tmp (or $TMPDIR) can't execute the script itself, while a
    # launcher such as gnome-terminal still exits 0, so "Opened" would show.
    started, _codes, _failing = popen
    monkeypatch.setenv("TERMINAL", terminal)
    monkeypatch.setattr(launch.shutil, "which", base._which({terminal}))

    launch._linux_terminal(["/bin/agent", "hi"], str(tmp_path), None)

    (proc,) = started
    script = proc.args[-1]
    os.remove(script)
    assert proc.args == [f"/usr/bin/{terminal}", *prefix, "/bin/sh", script]


@pytest.mark.skipif(os.name == "nt", reason="POSIX terminals")
def test_the_script_runs_even_where_it_cant_be_executed(monkeypatch, tmp_path):
    real_script = launch._one_shot_script

    def unexecutable(*args, **kwargs):
        path = real_script(*args, **kwargs)
        os.chmod(path, 0o600)  # as in a folder mounted noexec
        return path

    monkeypatch.setattr(launch, "_one_shot_script", unexecutable)
    # A launcher-style terminal: it starts the command and exits 0 at once.
    terminal = tmp_path / "launcher-terminal"
    terminal.write_text('#!/bin/sh\nshift\n"$@" &\nexit 0\n', encoding="utf-8")
    terminal.chmod(0o755)
    monkeypatch.setenv("TERMINAL", str(terminal))
    monkeypatch.setattr(launch.shutil, "which",
                        lambda name: str(terminal) if name == str(terminal) else None)
    marker = tmp_path / "ran.txt"

    launch._linux_terminal(
        [sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write('ran')", str(marker)],
        str(tmp_path), None)

    wait_until(marker.exists, timeout=15)
