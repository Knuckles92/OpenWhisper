"""T3 Code's ``t3`` command: open threads, a new thread, or one more message.

Each call signs in to the running T3 Code app as this computer's owner for as
long as the command runs, which is how T3 Code lets scripts on the same
computer drive it. The words go on standard input and a new thread's project
is the working folder (``.``), so nothing the user said or chose reaches a
command line: on Windows ``t3`` is a batch file, and cmd.exe re-reads its
arguments. Only ids T3 Code gave out do.

Every call blocks for a fraction of a second; run them on a worker.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from services.agent_process import kill_agent_process, popen_agent

logger = logging.getLogger(__name__)

#: Long enough for T3 Code to sign a call in and start a thread.
TIMEOUT_S = 30.0
#: Projects listed at once.
_LISTERS = 4
#: T3 Code's run statuses while an agent is at work.
_WORKING = frozenset({"preparing", "queued", "starting", "running"})
_STOPPED = frozenset({"interrupted", "cancelled", "rolled_back"})
#: Thread and project ids come from T3 Code; this keeps one from ever reading as a flag.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,199}")
NOT_INSTALLED = ("T3 Code's t3 command isn't installed. In T3 Code, choose Install "
                 "next to t3 command in Settings → About.")


class T3Error(Exception):
    """Why T3 Code didn't take a request, in words for the user."""


@dataclass(frozen=True)
class Project:
    """A T3 Code project: its id, its name, and the folder it works in."""

    id: str
    title: str
    folder: str


@dataclass(frozen=True)
class Thread:
    """One of a project's threads, as ``t3 thread list`` reports it."""

    id: str
    title: str
    #: T3 Code's status: a run status such as ``running`` or ``waiting``, or ``idle``.
    status: str
    #: When it last changed, as T3 Code's ISO time; newest sorts last.
    updated_at: str
    project: Project

    @property
    def state(self) -> str:
        """``working``, ``needs you``, ``done``, ``failed``, ``stopped`` or ``new``."""
        if self.status in _WORKING:
            return "working"
        if self.status == "waiting":
            return "needs you"
        if self.status == "completed":
            return "done"
        if self.status == "failed":
            return "failed"
        if self.status in _STOPPED:
            return "stopped"
        return "new"


def command() -> list[str] | None:
    """How to run ``t3`` on this computer, or None when it isn't installed.

    T3 Code puts the command on ``PATH`` when asked to; before that it is in
    its own folder, which a desktop app's environment may not include.
    """
    found = shutil.which("t3")
    if found:
        return [found]
    home = Path.home()
    if sys.platform == "win32":
        places = [home / ".t3" / "bin" / "t3.cmd"]
    else:
        places = [home / ".t3" / "bin" / "t3", home / ".local" / "bin" / "t3", home / "bin" / "t3"]
    for place in places:
        if place.is_file():
            return [str(place)]
    return None


def installed() -> bool:
    return command() is not None


def projects() -> list[Project]:
    """T3 Code's projects on this computer whose folders still exist.

    The ``t3`` command lists a project's threads but not the projects, so
    this reads the one table of T3 Code's own database that holds them,
    read-only. A database it can't read, now or after T3 Code changes it,
    gives no projects rather than an error.
    """
    path = Path(os.environ.get("T3CODE_HOME") or Path.home() / ".t3") / "userdata" / "statev2.sqlite"
    if not path.is_file():
        return []
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2.0)) as db:
            rows = db.execute(
                "SELECT project_id, title, workspace_root FROM projection_projects "
                "WHERE deleted_at IS NULL AND archived_at IS NULL").fetchall()
    except sqlite3.Error:
        logger.warning("Couldn't read T3 Code's projects", exc_info=True)
        return []
    found = [Project(str(pid), str(title or ""), str(root)) for pid, title, root in rows
             if isinstance(pid, str) and _ID.fullmatch(pid) and root and os.path.isdir(root)]
    return sorted(found, key=lambda project: project.title.casefold())


def recent_threads() -> list[Thread]:
    """Every project's threads that aren't settled, latest activity first.

    Projects are listed side by side. One that can't be listed is left out;
    when none can, the first reason is raised (T3 Code isn't running, say).
    """
    found = projects()
    if not found:
        return []
    errors: list[str] = []

    def listed(project: Project) -> list[Thread]:
        try:
            return _threads(project)
        except T3Error as exc:
            errors.append(str(exc))
            return []

    with ThreadPoolExecutor(max_workers=_LISTERS) as pool:
        threads = [thread for each in pool.map(listed, found) for thread in each]
    if not threads and errors:
        raise T3Error(errors[0])
    threads.sort(key=lambda thread: thread.updated_at, reverse=True)
    return threads


def _threads(project: Project) -> list[Thread]:
    out = _run(["thread", "list", "--project", project.id, "--json"], project.folder)
    try:
        items = json.loads(out)
    except ValueError as exc:
        raise T3Error("T3 Code listed its threads in a way OpenWhisper can't read. "
                      "Update T3 Code or OpenWhisper.") from exc
    found = []
    for item in items if isinstance(items, list) else ():
        if not isinstance(item, dict) or item.get("settled") is True:
            continue
        thread_id, title = item.get("threadId"), item.get("title")
        if isinstance(thread_id, str) and _ID.fullmatch(thread_id) and isinstance(title, str):
            found.append(Thread(thread_id, title, str(item.get("status") or "idle"),
                                str(item.get("updatedAt") or ""), project))
    return found


def start(folder: str, text: str) -> str:
    """Start a thread in ``folder``'s project with ``text`` as its first message.

    T3 Code names it from the message and runs it in the project's default
    model and permission mode, as a thread started in the app would be.

    Returns:
        The new thread's id.
    """
    out = _run(["thread", "start", ".", "-", "--generate-title", "--json"], folder, text)
    try:
        return str(json.loads(out)["threadId"])
    except (ValueError, KeyError, TypeError):
        # The command succeeded, so the thread exists; only its id is unknown.
        logger.warning("T3 Code started a thread but its answer couldn't be read")
        return ""


def send(folder: str, thread_id: str, text: str) -> None:
    """Add ``text`` to a thread; a thread at work takes it as its next turn."""
    if not _ID.fullmatch(thread_id):
        raise T3Error("That isn't a T3 Code thread.")
    _run(["thread", "send", thread_id, "-", "--json"], folder, text)


def _run(args: list[str], folder: str, stdin: str = "") -> str:
    argv = command()
    if argv is None:
        raise T3Error(NOT_INSTALLED)
    if not folder or not os.path.isdir(folder):
        raise T3Error("That project folder doesn't exist anymore. Choose another.")
    try:
        proc = popen_agent(argv + args, cwd=folder, stdin=subprocess.PIPE,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        raise T3Error(f"Couldn't run T3 Code's t3 command: {exc}") from exc
    try:
        out, err = proc.communicate(stdin.encode("utf-8"), timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise T3Error("T3 Code didn't answer in time. Check that it's open, then try again.") from exc
    finally:
        kill_agent_process(proc)
    if proc.returncode != 0:
        raise T3Error(_failure(err.decode("utf-8", "replace")))
    return out.decode("utf-8", "replace")


def _failure(stderr: str) -> str:
    """T3 Code's own one-line reason, said for this app where it would read oddly."""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if any("No running T3 Code server" in line for line in lines):
        return "T3 Code isn't running. Open it, then send again."
    if any("No T3 Code project matches" in line for line in lines):
        # T3 Code's words name "." here, the folder OpenWhisper ran it in.
        return "That folder isn't a project in T3 Code. Add it there, or choose another folder."
    return lines[-1][:300] if lines else "T3 Code didn't take it."
