"""Opening links and terminals for actions (stream C2 owns this module).

Words reach a link percent-encoded and a terminal as one argv element; they
never pass through a shell, cmd.exe or AppleScript source, nor a terminal's
own command-line parsing (macOS and Linux terminals run a one-shot script
whose every argument is shell-quoted). Every child gets the user's
environment without OpenWhisper's own keys (``agent_child_env``).
"""

from __future__ import annotations

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
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

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
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
        except OSError as exc:
            raise LaunchError("No app on this computer opens that link.") from exc
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


# AssocQueryStringW: the lookup ShellExecute itself uses, so it also sees
# protocols that packaged (Microsoft Store / MSIX) apps declare in their
# manifest, which leave only a bare "URL Protocol" key in the registry.
_ASSOCF_NOTRUNCATE = 0x20
_ASSOCF_INIT_IGNOREUNKNOWN = 0x400
_ASSOCF_IS_PROTOCOL = 0x1000
_ASSOCSTR_COMMAND = 1
_ASSOCSTR_EXECUTABLE = 2
_ASSOCSTR_PROGID = 20
_S_OK = 0
#: HRESULT_FROM_WIN32(ERROR_NO_ASSOCIATION) and (ERROR_NOT_FOUND).
_NO_HANDLER = (0x80070483, 0x80070490)


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
    result = query(flags, what, scheme, None, buffer, ctypes.byref(size))
    return result & 0xFFFFFFFF, buffer.value


def _windows_handler(scheme: str) -> Optional[bool]:
    result, command = _assoc_query(scheme, _ASSOCSTR_COMMAND)
    if result == _S_OK and command:
        # A classic handler; one left behind by an uninstalled app opens nothing.
        found, program = _assoc_query(scheme, _ASSOCSTR_EXECUTABLE)
        if found == _S_OK and os.path.isabs(program):
            return os.path.isfile(program)
        return True
    progid_result, progid = _assoc_query(scheme, _ASSOCSTR_PROGID)
    if progid_result == _S_OK and progid.startswith("AppX"):
        return True  # a packaged app declares the protocol
    if result in _NO_HANDLER or progid_result in _NO_HANDLER or progid_result == _S_OK:
        return False
    return None


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
    command line holds only the script's path (terminals re-parse ``-e``
    arguments). ``env`` (default: the user's environment without
    OpenWhisper's keys) applies where the terminal is our child (Windows,
    Linux); macOS Terminal uses the user's own login environment.

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
    # shell, so their command line carries only the script's path.
    try:
        script = _one_shot_script(args, cwd, shell="/bin/sh", suffix=".sh")
    except OSError as exc:
        raise LaunchError("No terminal app could be started.") from exc
    commands = _terminal_commands([script])
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
    """Collect the terminal's exit status whenever it closes."""
    threading.Thread(target=proc.wait, name="terminal-reaper", daemon=True).start()


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
