"""Links on Windows open through Explorer (review round 3).

ShellExecute runs inside Explorer, so a link's app starts with Explorer's
environment, the user's own, and everything ShellExecute does with a handler
(filling in its command, unquoted program paths, rundll32 and DDE handlers,
the elevation prompt) is Windows' own.

The real-Windows checks open nothing visible: the "app" is pythonw.exe (no
window) running a recorder that appends its command line and one variable
to a file, and a scheme is registered under HKCU\\Software\\Classes\\<a unique
name> for one test and removed after it.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import uuid

import pytest

from services.actions import launch
from tests import test_automation_agents_launch as base

fresh_handler_cache = base.fresh_handler_cache
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows opens links through Explorer")

#: Set in this process only: a child that has it got OpenWhisper's environment.
MARK = "OPENWHISPER_TEST_LAUNCH_MARK"
PYTHONW = os.path.join(sys.base_prefix, "pythonw.exe")
#: Shell.Application created in this process: the same IShellDispatch2 Explorer's desktop has.
CLSID_SHELL_APPLICATION = "{13709620-C279-11CE-A49E-444553540000}"

RECORDER = """
import ctypes, json, os, sys
ctypes.windll.kernel32.GetCommandLineW.restype = ctypes.c_wchar_p
with open(sys.argv[1], "a", encoding="utf-8") as log:
    log.write(json.dumps({"cmd": ctypes.windll.kernel32.GetCommandLineW(),
                          "args": sys.argv[2:], "mark": os.environ.get(%r)}) + "\\n")
""" % MARK


@pytest.fixture(scope="module", autouse=True)
def _pythoncom_imported_off_the_main_thread():
    # Importing pythoncom initialises COM on the importing thread for good;
    # keep that off the test process's main thread, as the app keeps it off
    # the Qt thread.
    if sys.platform == "win32":
        _in_thread(lambda: __import__("pythoncom"))


def _in_thread(fn, *args):
    """``fn(*args)`` on a thread of its own with COM, as actions open links; returns or raises."""
    outcome = {}

    def run():
        try:
            with launch._com():
                outcome["value"] = fn(*args)
        except BaseException as exc:  # handed to the test
            outcome["error"] = exc

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(60)
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


# -- the opener, with Explorer faked ------------------------------------------------------


@pytest.fixture
def ways_out(monkeypatch):
    """Where a Windows link can go, recorded: Explorer, or ShellExecute in this process."""
    explorer, here = [], []
    monkeypatch.setattr(launch, "_com", contextlib.nullcontext)
    monkeypatch.setattr(launch, "_explorer_open", lambda uri: explorer.append(uri) or True)
    monkeypatch.setattr(launch.os, "startfile", here.append, raising=False)
    return explorer, here


@pytest.mark.parametrize("uri", [
    "claude-cli://open?q=hello%20there",
    "ms-mmsys:",                                     # a rundll32 handler
    "proton-vpn://connect",                          # an unquoted program path with spaces
    "codex://new?prompt=caf%C3%A9",                  # a packaged app
    "https://chatgpt.com/?q=" + "a%20" * 6000,       # long
    "s:caf\u00e9/\u4e2d\u6587?q=\u00fc\U0001F600",   # not ASCII
    "s:path\\",                                      # ends in a backslash
])
def test_every_link_goes_to_explorer_as_it_is(ways_out, uri):
    explorer, here = ways_out
    launch._windows_open(uri)
    assert explorer == [uri] and here == []


def test_without_explorers_desktop_the_link_opens_from_here(ways_out, monkeypatch):
    explorer, here = ways_out
    monkeypatch.setattr(launch, "_explorer_open", lambda uri: False)
    launch._windows_open("codex://new?prompt=hi")
    assert here == ["codex://new?prompt=hi"]


def test_when_nothing_opens_it_says_so(ways_out, monkeypatch):
    def fail(uri):
        raise OSError(1155, "No application is associated with the specified file")

    monkeypatch.setattr(launch, "_explorer_open", lambda uri: False)
    monkeypatch.setattr(launch.os, "startfile", fail, raising=False)
    with pytest.raises(launch.LaunchError, match="No app on this computer opens that link"):
        launch._windows_open("odd:x")


@windows_only
def test_explorer_is_asked_with_the_default_verb(monkeypatch):
    calls, allowed = [], []

    class Application:
        def GetIDsOfNames(self, name):
            return {"ShellExecute": 7}[name]

        def Invoke(self, dispid, lcid, flags, result_wanted, *args):
            calls.append((dispid, flags, args))

    monkeypatch.setattr(launch, "_desktop_application", lambda: (Application(), 65914))
    monkeypatch.setattr(launch, "_allow_foreground", allowed.append)
    assert _in_thread(launch._explorer_open, "s:x") is True
    # File, no parameters, no folder, the default verb, SW_SHOWNORMAL.
    assert calls == [(7, 1, ("s:x", "", "", "", 1))]
    assert allowed == [65914]


@windows_only
def test_no_desktop_or_no_answer_from_explorer_means_not_opened(monkeypatch):
    import pythoncom

    def unreachable():
        raise pythoncom.com_error(-2147023174, "The RPC server is unavailable.", None, None)

    monkeypatch.setattr(launch, "_desktop_application", unreachable)
    assert _in_thread(launch._explorer_open, "s:x") is False
    monkeypatch.setattr(launch, "_desktop_application", lambda: (None, 0))
    assert _in_thread(launch._explorer_open, "s:x") is False


# -- real Windows, hidden -----------------------------------------------------------------


def _has_desktop():
    """Whether this session's Explorer has a desktop (a CI service may have none)."""
    import ctypes
    from ctypes import wintypes

    application, hwnd = launch._desktop_application()
    if application is None:
        return False
    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    pid, theirs, ours = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return bool(kernel32.ProcessIdToSessionId(pid, ctypes.byref(theirs))
                and kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(ours))
                and theirs.value == ours.value)


def _recorder(tmp_path):
    if not os.path.isfile(PYTHONW):
        pytest.skip("no pythonw.exe next to this Python")
    script = tmp_path / "recorder.py"
    script.write_text(RECORDER, encoding="utf-8")
    return script, tmp_path / "record.jsonl"


def _records(log, count, timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists():
            lines = log.read_text(encoding="utf-8").splitlines()
            if len(lines) >= count:
                return [json.loads(line) for line in lines]
        time.sleep(0.05)
    raise AssertionError(f"the handler didn't run (expected {count} records)")


def _shortcut(path, target, arguments):
    import pythoncom
    from win32comext.shell import shell

    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None,
                                      pythoncom.CLSCTX_INPROC_SERVER, shell.IID_IShellLink)
    link.SetPath(target)
    link.SetArguments(arguments)
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)


@windows_only
def test_what_a_link_opens_gets_explorers_environment_not_openwhispers(tmp_path, monkeypatch):
    # The app a link opens starts from Explorer. ShellExecute in this process
    # (os.startfile, and the classic-handler CreateProcess before it) would
    # hand it this process's environment: .env keys, CUDA folders on PATH.
    if not _in_thread(_has_desktop):
        pytest.skip("Explorer's desktop isn't running")
    script, log = _recorder(tmp_path)
    shortcut = tmp_path / "recorder.lnk"  # opens like a link, with no registration
    _in_thread(_shortcut, shortcut, PYTHONW, f'"{script}" "{log}" opened')
    monkeypatch.setenv(MARK, "from-openwhisper")

    _in_thread(launch._windows_open, str(shortcut))
    (through_explorer,) = _records(log, 1)
    _in_thread(os.startfile, str(shortcut))
    _, from_here = _records(log, 2)

    assert through_explorer["args"] == from_here["args"] == ["opened"]
    assert from_here["mark"] == "from-openwhisper"  # what the old way passed on
    assert through_explorer["mark"] is None


def _shell_execute_here(uri):
    """ShellExecuteExW in this process, never with any UI: the reference."""
    import ctypes
    from ctypes import wintypes

    class Info(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong),
                    ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(Info)]
    info = Info(cbSize=ctypes.sizeof(Info), fMask=0x400 | 0x100,  # FLAG_NO_UI | NOASYNC
                lpFile=uri, nShow=1)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        raise AssertionError(f"ShellExecuteExW failed ({ctypes.get_last_error()})")


@contextlib.contextmanager
def _scheme(template):
    import winreg

    scheme = "owtest" + uuid.uuid4().hex[:12]
    base_key = r"Software\Classes" + "\\" + scheme
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base_key) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, "URL:OpenWhisper test")
            winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, base_key + r"\shell\open\command") as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, template)
        yield scheme
    finally:
        for sub in (r"\shell\open\command", r"\shell\open", r"\shell", ""):
            with contextlib.suppress(OSError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, base_key + sub)
        launch.forget_handlers()


@pytest.fixture
def spaced_python(tmp_path):
    """A folder with a space in its name that holds pythonw.exe (a junction to this Python)."""
    import _winapi

    folder = tmp_path / "py dir"
    _winapi.CreateJunction(sys.base_prefix, str(folder))
    try:
        yield folder
    finally:
        os.rmdir(folder)  # the junction only


LINKS = [
    "{s}://new?q=caf%C3%A9%20%22quoted%22&b=1#frag",
    "{s}:caf\u00e9/\u4e2d\u6587?q=\u00fc\U0001F600",
    "{s}://x?q=" + "a%20" * 5000,  # 20,000 characters
]


@windows_only
@pytest.mark.parametrize("template", [
    '"{pythonw}" "{script}" "{log}" "%1"',
    '"{pythonw}" "{script}" "{log}" %1',
    '{spaced}\\pythonw.exe "{script}" "{log}" "%1"',  # the program path isn't quoted
], ids=["quoted", "link-unquoted", "program-path-unquoted"])
def test_a_link_reaches_its_handler_as_shellexecute_passes_it(
        tmp_path, monkeypatch, spaced_python, template):
    # The Explorer path's ShellExecute call, made on this process's own
    # Shell.Application (Explorer can't see a scheme a sandboxed test
    # registers), against ShellExecuteExW: the same command line each time.
    import pythoncom

    script, log = _recorder(tmp_path)
    command = template.format(pythonw=PYTHONW, script=script, log=log, spaced=spaced_python)
    monkeypatch.setattr(launch, "_desktop_application", lambda: (pythoncom.CoCreateInstance(
        CLSID_SHELL_APPLICATION, None, pythoncom.CLSCTX_INPROC_SERVER,
        pythoncom.IID_IDispatch), 0))
    with _scheme(command) as scheme:
        for index, link in enumerate(LINKS):
            uri = link.format(s=scheme)
            _in_thread(_shell_execute_here, uri)  # never any UI; fails first if it can't
            _records(log, 2 * index + 1)
            _in_thread(launch._windows_open, uri)
            records = _records(log, 2 * index + 2)
            reference, ours = records[-2:]
            assert ours["cmd"] == reference["cmd"]
            assert ours["args"] == reference["args"] and scheme in ours["args"][-1]


# -- the association lookup still says when an app is missing ------------------------------


def test_a_handler_whose_program_is_gone_counts_as_missing(monkeypatch):
    # An uninstalled app's registration left behind (a real one: Signal's sgnl:).
    monkeypatch.setattr(launch, "_assoc_query", base.FakeAssoc({
        ("sgnl", launch._ASSOCSTR_COMMAND): (0, r'"C:\gone\Signal.exe" "%1"'),
        ("sgnl", launch._ASSOCSTR_EXECUTABLE): (0x800401F5, ""),  # CO_E_APPNOTFOUND
    }))
    assert launch._windows_handler("sgnl") is False


def test_a_rundll32_handler_counts(monkeypatch):
    # ASSOCSTR_EXECUTABLE names the DLL: it exists, so the link can open.
    monkeypatch.setattr(launch, "_assoc_query", base.FakeAssoc({
        ("ms-mmsys", launch._ASSOCSTR_COMMAND):
            (0, r"C:\WINDOWS\system32\rundll32.exe shell32.dll,Control_RunDLL mmsys.cpl,,0"),
        ("ms-mmsys", launch._ASSOCSTR_EXECUTABLE): (0, r"C:\WINDOWS\system32\shell32.dll"),
    }))
    assert launch._windows_handler("ms-mmsys") is True
