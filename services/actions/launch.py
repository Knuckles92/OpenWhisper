"""Opening links and terminals for actions (stream C2 owns this module).

Words reach a link percent-encoded and a terminal as one argv element; they
never pass through a shell, cmd.exe or AppleScript source, nor a terminal's
own command-line parsing (macOS and Linux terminals run a one-shot script
whose every argument is shell-quoted). Every child gets the user's
environment without OpenWhisper's own keys (``agent_child_env``); on
Windows a link opens through Explorer, the user's shell, so its app starts
with Explorer's environment rather than this process's.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: How long a handler lookup is trusted; apps are rarely installed mid-session.
HANDLER_CACHE_S = 60.0
#: Windows rejects longer command lines (32,767 characters with the program).
WINDOWS_COMMAND_LINE_MAX = 32_000
#: A link travels on its handler's command line, so longer ones can't open.
LINK_MAX_CHARS = 30_000
#: Claude Desktop cuts a ``claude://`` link's ``q`` near 14,000 characters.
CLAUDE_APP_TEXT_MAX = 14_000
#: How long ``xdg-open``/``open`` may take to answer; one still running has
#: started the link's app in the foreground (xdg-open's generic mode).
OPEN_WAIT_S = 2.0
#: How long a just-started Linux terminal is watched for dying at once.
TERMINAL_START_CHECK_S = 0.5

_SCHEME_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*")
#: What to install for a scheme a preset or an agent link uses.
_SCHEME_APPS = {"claude": "the Claude app", "claude-cli": "Claude Code",
                "codex": "the ChatGPT app"}
_handler_lock = threading.Lock()
_handler_cache: Dict[str, Tuple[float, Optional[bool]]] = {}


class LaunchError(RuntimeError):
    """Opening failed; the message is safe to show."""


def _child_env() -> Dict[str, str]:
    """The user's environment without OpenWhisper's own keys and settings."""
    from services.installed_agents import agent_child_env

    return agent_child_env()


def open_uri(uri: str) -> None:
    """Hand ``uri`` to the app registered for its scheme; blocks briefly.

    Raises:
        LaunchError: Nothing could open it, or it is too long to; the message
            says which app is missing when one is known.
    """
    if not uri or any(ch in uri for ch in "\r\n\x00"):
        raise LaunchError("That link can't be opened.")
    if len(uri) > LINK_MAX_CHARS:
        raise LaunchError("That's too long to send in a link. Try fewer words.")
    scheme = uri.split(":", 1)[0].lower()
    # Windows would offer to find an app in the Store, and xdg-open a
    # browser, for a scheme nothing handles.
    if scheme not in ("http", "https") and handler_available(scheme) is False:
        app = _SCHEME_APPS.get(scheme)
        raise LaunchError(f"No app on this computer opens {scheme}: links."
                          + (f" Install {app}." if app else ""))
    if sys.platform == "win32":
        _windows_open(uri)
        return
    _open_with(["open", uri] if sys.platform == "darwin" else ["xdg-open", uri])


def _open_with(command: List[str]) -> None:
    """Run ``open``/``xdg-open``; done once it hands the link over.

    In its generic mode xdg-open runs the link's app in the foreground, so
    one still running after :data:`OPEN_WAIT_S` has opened it.
    """
    try:
        proc = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=_child_env(), start_new_session=True,
        )
    except OSError as exc:
        raise LaunchError("No app on this computer opens that link.") from exc
    try:
        code = proc.wait(timeout=OPEN_WAIT_S)
    except subprocess.TimeoutExpired:
        _reap(proc)
        return
    if code != 0:
        raise LaunchError("No app on this computer opens that link.")


# ---- URL handlers ----

def handler_available(scheme: str) -> Optional[bool]:
    """Whether an app is registered for ``scheme``; None when it can't be told.

    Blocking (a registry, Launch Services or ``xdg-mime`` lookup); answers are
    cached for :data:`HANDLER_CACHE_S`.
    """
    scheme = (scheme or "").strip().lower().rstrip(":")
    if not _SCHEME_RE.fullmatch(scheme):
        return False
    now = time.monotonic()
    with _handler_lock:
        cached = _handler_cache.get(scheme)
        if cached is not None and now - cached[0] < HANDLER_CACHE_S:
            return cached[1]
    try:
        if sys.platform == "win32":
            found = _windows_handler(scheme)
        elif sys.platform == "darwin":
            found = _macos_handler(scheme)
        else:
            found = _linux_handler(scheme)
    except Exception as exc:  # a lookup must never break an action
        logger.debug("Handler lookup failed (%s)", type(exc).__name__)
        found = None
    with _handler_lock:
        _handler_cache[scheme] = (time.monotonic(), found)
    return found


def forget_handlers() -> None:
    """Drop cached handler lookups, for example after the user installs an app."""
    with _handler_lock:
        _handler_cache.clear()


# AssocQueryStringW: the lookup ShellExecute itself uses. A protocol's handler
# is a command line, or a COM object named by DelegateExecute: every packaged
# (Microsoft Store / MSIX) app's protocol, declared in its manifest, and
# Windows' own pages such as ms-settings:. ShellExecute prefers the COM object.
_ASSOCF_NOTRUNCATE = 0x20
_ASSOCF_INIT_IGNOREUNKNOWN = 0x400
_ASSOCF_IS_PROTOCOL = 0x1000
_ASSOCSTR_COMMAND = 1
_ASSOCSTR_EXECUTABLE = 2
_ASSOCSTR_DELEGATEEXECUTE = 18
_S_OK = 0
#: HRESULT_FROM_WIN32(ERROR_NO_ASSOCIATION) and (ERROR_NOT_FOUND).
_NO_HANDLER = (0x80070483, 0x80070490)
#: The handler's program isn't there.
_CO_E_APPNOTFOUND = 0x800401F5
_COINIT_APARTMENTTHREADED = 0x2
_COINIT_DISABLE_OLE1DDE = 0x4


@contextlib.contextmanager
def _com() -> Iterator[None]:
    """COM on this thread while the shell looks a link up or opens it.

    Windows reads some packaged apps' protocols (ms-actioncenter:) only
    through COM; without it, AssocQueryStringW says they have no handler.
    """
    import ctypes

    ole32 = ctypes.WinDLL("ole32")
    result = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED | _COINIT_DISABLE_OLE1DDE)
    try:
        yield
    finally:
        if result >= 0:  # S_OK or S_FALSE; a thread already in another apartment keeps it
            ole32.CoUninitialize()


def _assoc_query(scheme: str, what: int) -> Tuple[int, str]:
    """``AssocQueryStringW`` for a protocol: (HRESULT as unsigned, value)."""
    import ctypes
    from ctypes import wintypes

    query = ctypes.WinDLL("shlwapi").AssocQueryStringW
    query.argtypes = [ctypes.c_uint, ctypes.c_int, wintypes.LPCWSTR, wintypes.LPCWSTR,
                      wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    query.restype = ctypes.c_long
    size = wintypes.DWORD(2048)
    buffer = ctypes.create_unicode_buffer(size.value)
    flags = _ASSOCF_IS_PROTOCOL | _ASSOCF_INIT_IGNOREUNKNOWN | _ASSOCF_NOTRUNCATE
    with _com():
        result = query(flags, what, scheme, None, buffer, ctypes.byref(size))
    return result & 0xFFFFFFFF, buffer.value


def _windows_handler(scheme: str) -> Optional[bool]:
    """Whether ShellExecute has a handler for ``scheme``: a COM object or a command."""
    delegate_result, delegate = _assoc_query(scheme, _ASSOCSTR_DELEGATEEXECUTE)
    if delegate_result == _S_OK and delegate:
        return True
    result, command = _assoc_query(scheme, _ASSOCSTR_COMMAND)
    if result != _S_OK or not command:
        missing = result in _NO_HANDLER and delegate_result in _NO_HANDLER
        return False if missing else None
    # The lookup checks the program: one an uninstalled app left behind opens nothing.
    return _assoc_query(scheme, _ASSOCSTR_EXECUTABLE)[0] != _CO_E_APPNOTFOUND


#: ShellWindows: Explorer's list of its windows, the desktop among them.
_CLSID_SHELL_WINDOWS = "{9BA05972-F6A8-11CF-A442-00A0C90A8F39}"
_SW_SHOWNORMAL = 1
#: How long Explorer may take to take a link. ShellExecute returns once the
#: link's app has started or a rundll32 or DDE handler has the link, which
#: takes moments, and may wait while an elevation prompt is up. This leaves
#: room for a start slowed by a busy disk and for a person answering that
#: prompt, and still gets the action's worker back from an Explorer whose
#: desktop has hung.
EXPLORER_WAIT_S = 30.0
_NO_ANSWER = "Windows didn't answer. Try again."
#: Held while Explorer is asked. Its desktop takes one call at a time, and
#: no more calls queue up behind one it never answers.
_explorer_turn = threading.Lock()


def _windows_open(uri: str) -> None:
    """Open ``uri`` the way the user's shell does, in the user's environment.

    ShellExecute in this process would start the link's app as a child of
    OpenWhisper, with its environment (its ``.env`` keys, its CUDA folders on
    ``PATH``). Explorer's desktop runs ShellExecute in Explorer instead: the
    app starts with Explorer's environment, the user's own, and everything
    about how the link reaches it (filling in the handler's command,
    rundll32 and DDE handlers, packaged apps, the elevation prompt) is
    Windows' own. Explorer shows any problem itself.

    Raises:
        LaunchError: Explorer didn't answer in time, or, without Explorer's
            desktop, nothing opens the link.
    """
    if _ask_explorer(uri):
        return
    # Explorer's desktop isn't there (Explorer is restarting, or another
    # shell runs): only then does the link open from this process, and its
    # app get OpenWhisper's environment.
    with _com():
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
        except OSError as exc:
            raise LaunchError("No app on this computer opens that link.") from exc


def _ask_explorer(uri: str) -> bool:
    """:func:`_explorer_open` on a thread of its own, waiting :data:`EXPLORER_WAIT_S` at most.

    That thread has COM while it asks, and is the one that imports pywin32's
    ``pythoncom`` first, which initialises COM on its thread for good; the
    caller's thread (an action's worker) is left as it was.

    Raises:
        LaunchError: Explorer didn't answer in time. It may still open the
            link later, so nothing else opens it.
    """
    deadline = time.monotonic() + EXPLORER_WAIT_S
    if not _explorer_turn.acquire(timeout=EXPLORER_WAIT_S):
        raise LaunchError(_NO_ANSWER)
    outcome: List[object] = []

    def ask() -> None:
        try:
            with _com():
                try:
                    outcome.append(_explorer_open(uri))
                except Exception as exc:
                    # Its frames hold COM objects, which must go before COM does.
                    traceback.clear_frames(exc.__traceback__)
                    outcome.append(exc)
        finally:
            _explorer_turn.release()

    try:
        asker = threading.Thread(target=ask, name="explorer-open", daemon=True)
        asker.start()
    except BaseException:
        _explorer_turn.release()
        raise
    asker.join(max(0.0, deadline - time.monotonic()))
    if not outcome:
        logger.warning("Explorer didn't take a link within %.0f s", EXPLORER_WAIT_S)
        raise LaunchError(_NO_ANSWER)
    if isinstance(outcome[0], Exception):
        raise outcome[0]
    return bool(outcome[0])


def _explorer_open(uri: str) -> bool:
    """ShellExecute ``uri`` in Explorer; False when Explorer's desktop can't be asked.

    Needs COM on this thread; every COM object is released by the time this
    returns.
    """
    import pythoncom

    try:
        application, hwnd = _desktop_application()
        if application is None:
            return False
        _allow_foreground(hwnd)
        # The default verb, as ShellExecute without one.
        application.Invoke(application.GetIDsOfNames("ShellExecute"), 0,
                           pythoncom.DISPATCH_METHOD, False,
                           uri, "", "", "", _SW_SHOWNORMAL)
    except pythoncom.com_error as exc:
        logger.debug("Explorer couldn't be asked to open a link (%#x)",
                     (exc.hresult or 0) & 0xFFFFFFFF)
        return False
    return True


def _desktop_application():
    """Explorer's ``Shell.Application`` for its desktop, and the desktop window.

    IShellWindows::FindWindowSW finds the desktop; its folder view's
    ``Application`` (IShellDispatch2) lives in Explorer, so its ShellExecute
    runs there. Returns ``(None, 0)`` when there is no desktop.

    Raises:
        pythoncom.com_error: Explorer couldn't be asked.
    """
    import pythoncom
    from win32comext.shell import shell, shellcon

    windows = pythoncom.CoCreateInstance(_CLSID_SHELL_WINDOWS, None,
                                         pythoncom.CLSCTX_LOCAL_SERVER, pythoncom.IID_IDispatch)
    desktop, hwnd = windows.InvokeTypes(
        windows.GetIDsOfNames("FindWindowSW"), 0, pythoncom.DISPATCH_METHOD,
        (pythoncom.VT_DISPATCH, 0),
        ((pythoncom.VT_VARIANT | pythoncom.VT_BYREF, 1),
         (pythoncom.VT_VARIANT | pythoncom.VT_BYREF, 1), (pythoncom.VT_I4, 1),
         (pythoncom.VT_I4 | pythoncom.VT_BYREF, 2), (pythoncom.VT_I4, 1)),
        shellcon.CSIDL_DESKTOP, None, shellcon.SWC_DESKTOP, 0, shellcon.SWFO_NEEDDISPATCH)
    if desktop is None:
        return None, 0
    browser = desktop.QueryInterface(pythoncom.IID_IServiceProvider).QueryService(
        shell.SID_STopLevelBrowser, shell.IID_IShellBrowser)
    view = browser.QueryActiveShellView().GetItemObject(
        shellcon.SVGIO_BACKGROUND, pythoncom.IID_IDispatch)
    return view.Invoke(view.GetIDsOfNames("Application"), 0,
                       pythoncom.DISPATCH_PROPERTYGET, True), hwnd


def _allow_foreground(hwnd: int) -> None:
    """Let the Explorer that owns ``hwnd`` bring the app it starts to the front.

    The app starts from Explorer, not from here; OpenWhisper may bring a
    window forward (the user just pressed its hotkey), and this passes that
    on. It does nothing when OpenWhisper may not.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    pid = wintypes.DWORD()
    if user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)):
        user32.AllowSetForegroundWindow(pid.value)


def _macos_handler(scheme: str) -> Optional[bool]:
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
        from Foundation import NSURL  # type: ignore[import-not-found]
    except ImportError:
        return None
    url = NSURL.URLWithString_(f"{scheme}://")
    if url is None:
        return None
    return NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(url) is not None


def _linux_handler(scheme: str) -> Optional[bool]:
    xdg_mime = shutil.which("xdg-mime")
    if not xdg_mime:
        return None
    try:
        completed = subprocess.run(
            [xdg_mime, "query", "default", f"x-scheme-handler/{scheme}"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=5, check=False, env=_child_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return bool(completed.stdout.strip())


# ---- terminals ----

def open_in_terminal(argv: Sequence[str], *, cwd: str = "",
                     env: Optional[Mapping[str, str]] = None) -> None:
    """Run ``argv`` in a new terminal window; returns once the window is asked for.

    Every element reaches the program as one argument: Windows starts the
    ``.exe`` directly in a new console; macOS and Linux run a one-shot
    script whose arguments are all single-quoted, so the terminal's own
    command line holds only the script (``/bin/sh <path>`` on Linux;
    terminals re-parse ``-e`` arguments). ``env`` (default: the user's
    environment without OpenWhisper's keys) applies where the terminal is
    our child (Windows, Linux); macOS Terminal uses the user's own login
    environment.

    Raises:
        LaunchError: No terminal could be opened; the message says why.
    """
    args = [str(arg) for arg in argv]
    if not args or not args[0]:
        raise LaunchError("There's no program to open.")
    if any("\x00" in arg for arg in args):
        raise LaunchError("Those words can't be passed to a program.")
    workdir = cwd or os.path.expanduser("~")
    if not os.path.isdir(workdir):
        raise LaunchError("The working folder doesn't exist.")
    env = dict(env) if env is not None else _child_env()
    if sys.platform == "win32":
        _windows_terminal(args, workdir, env)
    elif sys.platform == "darwin":
        _macos_terminal(args, workdir)
    else:
        _linux_terminal(args, workdir, env)


#: Windows runs these through cmd.exe or PowerShell, which would parse the words.
_SCRIPT_SUFFIXES = (".cmd", ".bat", ".ps1")
_CREATE_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)


def _windows_terminal(args: List[str], cwd: str, env: Optional[Mapping[str, str]]) -> None:
    program = args[0]
    name = os.path.basename(program)
    suffix = os.path.splitext(program)[1].lower()
    if suffix in _SCRIPT_SUFFIXES:
        raise LaunchError(
            f"{name} is a script, and Windows would read your words as commands "
            "on the way in. Install the program's own .exe build, or open it "
            "through its app instead."
        )
    if suffix != ".exe":
        raise LaunchError(f"{name} isn't a program OpenWhisper can open in a terminal.")
    if len(subprocess.list2cmdline(args)) > WINDOWS_COMMAND_LINE_MAX:
        raise LaunchError("That's too long to open in a terminal. Try a shorter request.")
    try:
        proc = subprocess.Popen(
            args, cwd=cwd, env=dict(env) if env is not None else None,
            creationflags=_CREATE_NEW_CONSOLE,
        )
    except OSError as exc:
        raise LaunchError(f"{name} couldn't be started in a terminal.") from exc
    _reap(proc)


def _one_shot_script(args: List[str], cwd: str, *, shell: str, suffix: str) -> str:
    """A private script that removes itself, enters ``cwd`` and runs ``args``.

    Every argument is single-quoted for the shell, so the words reach the
    program as one argument whatever they hold. Returns its path.

    Raises:
        OSError: It couldn't be written.
    """
    script = "\n".join((
        f"#!{shell}",
        'rm -f -- "$0"',
        f"cd -- {shlex.quote(cwd)} || exit 1",
        "exec " + " ".join(shlex.quote(arg) for arg in args),
        "",
    ))
    fd, path = tempfile.mkstemp(prefix="openwhisper-", suffix=suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(script)
        os.chmod(path, 0o700)
    except OSError:
        _remove(path)
        raise
    return path


def _macos_terminal(args: List[str], cwd: str) -> None:
    try:
        path = _one_shot_script(args, cwd, shell="/bin/zsh -l", suffix=".command")
    except OSError as exc:
        raise LaunchError("Terminal couldn't be opened.") from exc
    try:
        completed = subprocess.run(
            ["open", "-a", "Terminal", path], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
            env=_child_env(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _remove(path)
        raise LaunchError("Terminal couldn't be opened.") from exc
    if completed.returncode != 0:
        _remove(path)
        raise LaunchError("Terminal couldn't be opened.")


#: How each terminal takes a command; anything else gets the common ``-e``.
_TERMINAL_STYLES: Dict[str, Tuple[str, ...]] = {
    "gnome-terminal": ("--",),
    "ptyxis": ("--",),
    "kgx": ("--",),
    "foot": ("--",),
    "wezterm": ("start", "--"),
    "kitty": (),
    "xdg-terminal-exec": (),
    "xfce4-terminal": ("-x",),
    "mate-terminal": ("-x",),
    "terminator": ("-x",),
}
#: Tried in order after ``$TERMINAL``.
_LINUX_TERMINALS = (
    "x-terminal-emulator", "xdg-terminal-exec", "gnome-terminal", "konsole",
    "alacritty", "ghostty", "foot", "xterm", "kitty",
)


def _terminal_commands(args: List[str]) -> List[List[str]]:
    """Every terminal command line worth trying, ``$TERMINAL`` first."""
    commands: List[List[str]] = []
    preferred = os.environ.get("TERMINAL", "").strip()
    if preferred:
        try:
            words = shlex.split(preferred)
        except ValueError:
            words = []
        if words:
            program = shutil.which(words[0])
            if program:
                style = _TERMINAL_STYLES.get(os.path.basename(words[0]), ("-e",))
                commands.append([program, *words[1:], *style, *args])
    for name in _LINUX_TERMINALS:
        program = shutil.which(name)
        if program:
            commands.append([program, *_TERMINAL_STYLES.get(name, ("-e",)), *args])
    return commands


def _linux_terminal(args: List[str], cwd: str, env: Optional[Mapping[str, str]]) -> None:
    # Many terminals join or re-split what follows -e and hand it to a
    # shell, so their command line carries only the script's path. /bin/sh
    # reads the script: a temp folder mounted noexec can't run it directly,
    # and a launcher (gnome-terminal) would still exit 0 as if it had.
    try:
        script = _one_shot_script(args, cwd, shell="/bin/sh", suffix=".sh")
    except OSError as exc:
        raise LaunchError("No terminal app could be started.") from exc
    commands = _terminal_commands(["/bin/sh", script])
    for command in commands:
        name = os.path.basename(command[0])
        try:
            proc = subprocess.Popen(
                command, cwd=cwd, env=dict(env) if env is not None else None,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError as exc:
            logger.debug("Terminal %s didn't start (%s)", name, type(exc).__name__)
            continue
        try:
            # A launcher (gnome-terminal) exits 0 once its server has the
            # window; one that dies at once (say, on the bundle's libraries)
            # didn't open anything.
            code = proc.wait(timeout=TERMINAL_START_CHECK_S)
        except subprocess.TimeoutExpired:
            code = 0
            _reap(proc)
        if code != 0:
            logger.debug("Terminal %s stopped at once (code %s)", name, code)
            continue
        return  # the script removes itself when it runs
    _remove(script)
    if commands:
        raise LaunchError("No terminal app could be started.")
    raise LaunchError("No terminal app was found. Set $TERMINAL to the one you use.")


def _reap(proc: subprocess.Popen) -> None:
    """Collect the exit status of a terminal or link handler whenever it ends."""
    threading.Thread(target=proc.wait, name="terminal-reaper", daemon=True).start()


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
