"""Running saved programs on events (Settings > Hooks), and ``run_once``.

A program receives the event's wire JSON and a newline on stdin, and
``OPENWHISPER_EVENT_TYPE``, ``OPENWHISPER_EVENT_ID`` and
``OPENWHISPER_EVENT_SEQ`` in an environment that is the user's own minus
OpenWhisper's secrets. It runs without a shell or a console window, in its
own process group, and when its time limit passes it is stopped together
with everything it started. Its stderr is discarded.

Each program runs one event at a time; up to ``MAX_QUEUED`` more wait, and
the oldest is dropped beyond that. Program paths, arguments and output are
never logged. Contract: docs/design/automation/design.md.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from services.automation.events import Event, EventBus, Subscription
    from services.automation.models import Program

logger = logging.getLogger(__name__)

#: Events waiting per program; the oldest is dropped beyond this.
MAX_QUEUED = 20
#: Recent runs kept per program (memory only).
MAX_RECORDS = 20
#: Most of a program's stdout that is kept; the rest is read and discarded.
MAX_STDOUT_BYTES = 1024 * 1024
TEST_EVENT_TYPE = "program.test"
SUBSCRIPTION_LABEL = "programs"
#: How long, after a program exits, processes it left behind may keep its
#: output open before they are stopped.
OUTPUT_GRACE_S = 1.0


@dataclass(frozen=True)
class RunRecord:
    event_id: str
    at: str
    ok: bool
    exit_code: Optional[int]
    elapsed_ms: int
    error: str = ""


def _iso_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _load_settings() -> Mapping:
    from services import settings

    return settings.settings_manager.load_all_settings() or {}


# -- running one program --------------------------------------------------------

def child_env(extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """The environment for a hook program: the user's, minus OpenWhisper's secrets.

    Keys OpenWhisper loaded from its ``.env`` and its own internal tokens are
    removed (``services.installed_agents.agent_child_env``); ``extra`` is
    added on top.
    """
    from services.installed_agents import agent_child_env

    return agent_child_env({str(k): str(v) for k, v in (extra or {}).items()})


def _default_cwd() -> Optional[str]:
    home = os.path.expanduser("~")
    return home if os.path.isdir(home) else None


def _start_error(exc: OSError) -> str:
    if isinstance(exc, FileNotFoundError):
        return "The program wasn't found."
    if isinstance(exc, PermissionError):
        return "The program isn't allowed to run (permission denied)."
    code = getattr(exc, "winerror", None) or exc.errno
    if code == 193:
        return "Windows can't run that file directly. Choose a program (.exe)."
    return f"The program couldn't start ({type(exc).__name__} {code})."


def run_once(argv: Sequence[str], stdin: bytes, *, timeout_s: float, cwd: str = "",
             env: Optional[Mapping[str, str]] = None) -> tuple[Optional[int], bytes, str]:
    """Run ``argv`` once without a shell or a window; never raises.

    Args:
        argv: The program and its arguments, passed as they are.
        stdin: Written to the program's stdin, which is then closed.
        timeout_s: When it passes, the program and every process it started
            are stopped.
        cwd: The working folder; "" is the user's home folder.
        env: The whole environment; None is ``child_env()``.

    Returns:
        ``(exit code, stdout, error)``: stdout is at most
        ``MAX_STDOUT_BYTES``; error is "" when the program ran to its end
        (whatever its exit code), else why it didn't, and the exit code is
        then None. Processes the program leaves running are stopped when it
        exits.
    """
    import subprocess

    from services.agent_process import kill_agent_process, popen_agent

    args = [str(a) for a in (argv or ())]
    if not args or not args[0].strip():
        return None, b"", "Choose the program to run."
    try:
        timeout = max(0.05, float(timeout_s))
    except (TypeError, ValueError):
        return None, b"", "The time limit isn't a number."
    if cwd and not os.path.isdir(cwd):
        return None, b"", "The working folder doesn't exist."
    kwargs = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "cwd": cwd or _default_cwd(),
        "env": {str(k): str(v) for k, v in env.items()} if env is not None else child_env(),
    }
    if sys.platform == "win32":
        # popen_agent adds CREATE_NO_WINDOW and a kill-on-close job object.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = popen_agent(args, **kwargs)
    except OSError as exc:
        return None, b"", _start_error(exc)
    except (ValueError, TypeError):
        return None, b"", "The program couldn't start (its arguments aren't valid)."

    output = bytearray()

    def read() -> None:
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    return
                room = MAX_STDOUT_BYTES - len(output)
                if room > 0:
                    output.extend(chunk[:room])
        except (OSError, ValueError):
            return

    def write() -> None:
        try:
            if stdin:
                proc.stdin.write(stdin)
        except (OSError, ValueError):
            pass  # It exited without reading everything.
        finally:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass

    reader = threading.Thread(target=read, name="program-stdout", daemon=True)
    writer = threading.Thread(target=write, name="program-stdin", daemon=True)
    reader.start()
    writer.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
    if not timed_out:
        reader.join(timeout=OUTPUT_GRACE_S)
    try:
        kill_agent_process(proc)
    except Exception as exc:
        logger.debug("Could not stop a program's processes: %s", type(exc).__name__)
    reader.join(timeout=2.0)
    writer.join(timeout=2.0)
    try:
        proc.stdout.close()
    except (OSError, ValueError):
        pass
    if timed_out:
        return None, bytes(output), f"Timed out after {timeout:g} s"
    return proc.returncode, bytes(output), ""


def _run_event(program: Program, event: Event) -> RunRecord:
    at, started = _iso_now(), time.monotonic()
    env = child_env({
        "OPENWHISPER_EVENT_TYPE": event.type,
        "OPENWHISPER_EVENT_ID": event.id,
        "OPENWHISPER_EVENT_SEQ": str(event.seq),
    })
    code, _stdout, error = run_once(
        [program.program, *program.args], (event.to_json() + "\n").encode("utf-8"),
        timeout_s=program.timeout_s, env=env,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if not error and code != 0:
        error = f"Exited with code {code}"
    return RunRecord(event.id, at, not error, code, elapsed_ms, error)


# -- records ----------------------------------------------------------------------

_records_lock = threading.Lock()
_records: dict[str, deque] = {}


def _remember(program_id: str, record: RunRecord) -> None:
    with _records_lock:
        _records.setdefault(program_id, deque(maxlen=MAX_RECORDS)).appendleft(record)


def runs(program_id: str) -> list[RunRecord]:
    """Recent runs of one program, newest first (memory only)."""
    with _records_lock:
        return list(_records.get(program_id, ()))


# -- per-program serial runners ---------------------------------------------------

@dataclass
class _Item:
    event: Event
    #: A test run carries its program and a Future; an event run looks its
    #: program up when it starts, so a change in Settings applies at once.
    program: Optional[Program] = None
    future: Optional[Future] = None


class _Runner:
    def __init__(self) -> None:
        self.events: deque = deque()
        self.tests: deque = deque()
        self.thread: Optional[threading.Thread] = None
        self.dropped = 0


_cond = threading.Condition()
_runners: dict[str, _Runner] = {}


def _enqueue(program_id: str, item: _Item) -> None:
    # Caller holds _cond.
    runner = _runners.setdefault(program_id, _Runner())
    if item.future is not None:
        runner.tests.append(item)
    else:
        if len(runner.events) >= MAX_QUEUED:
            dropped = runner.events.popleft()
            runner.dropped += 1
            if runner.dropped == 1 or runner.dropped % 20 == 0:
                logger.warning(
                    "Program hook %s is backing up; dropped its oldest event (%d so far)",
                    program_id, runner.dropped,
                )
            _remember(program_id, RunRecord(
                dropped.event.id, _iso_now(), False, None, 0,
                "Dropped: too many events were waiting.",
            ))
        runner.events.append(item)
    if runner.thread is None:
        runner.thread = threading.Thread(
            target=_drain, args=(program_id,), name="automation-program", daemon=True
        )
        runner.thread.start()


def _drain(program_id: str) -> None:
    """Run one program's waiting items one at a time until none are left."""
    while True:
        with _cond:
            runner = _runners[program_id]
            if runner.tests:
                item = runner.tests.popleft()
                program = item.program
            elif runner.events:
                item = runner.events.popleft()
                program = _current_program(program_id, item.event.type)
                if program is None:
                    continue
            else:
                runner.thread = None
                del _runners[program_id]
                return
        try:
            record = _run_event(program, item.event)
        except Exception as exc:
            logger.warning("Program hook %s failed: %s", program_id, type(exc).__name__)
            record = RunRecord(item.event.id, _iso_now(), False, None, 0,
                               f"The program couldn't run ({type(exc).__name__}).")
        _remember(program_id, record)
        if item.future is not None:
            item.future.set_result(record)


def _current_program(program_id: str, event_type: str) -> Optional[Program]:
    # Caller holds _cond.
    engine = _engine
    program = engine.programs.get(program_id) if engine is not None else None
    if program is None or event_type not in program.events:
        return None
    return program


# -- the subscription -------------------------------------------------------------

class _Engine:
    """Which programs want which events, and the bus subscription."""

    def __init__(self) -> None:
        self.programs: dict[str, Program] = {}
        self._configure_lock = threading.Lock()
        self._bus: Optional[EventBus] = None
        self._subscription: Optional[Subscription] = None
        self._types: frozenset = frozenset()
        self._last_seq = 0
        self._stopping = False

    def configure(self, programs: list, bus: EventBus) -> None:
        enabled = {p.id: p for p in programs if p.enabled and p.events}
        types = frozenset(t for p in enabled.values() for t in p.events)
        with self._configure_lock:
            with _cond:
                if self._stopping:
                    return
                self.programs = enabled
                for program_id, runner in _runners.items():
                    program = enabled.get(program_id)
                    runner.events = deque(
                        i for i in runner.events
                        if program is not None and i.event.type in program.events
                    )
                if bus is self._bus and types == self._types:
                    return
                old = self._subscription
                if bus is not self._bus:
                    self._bus, self._last_seq = bus, 0
                self._types = types
            # Subscribe before closing the old one; an event delivered to
            # both is dropped by its sequence number.
            new = (bus.subscribe(functools.partial(self._on_event, bus), types=types,
                                 label=SUBSCRIPTION_LABEL) if types else None)
            with _cond:
                self._subscription = new
            if old is not None:
                old.close()

    def stop(self) -> None:
        with self._configure_lock:
            with _cond:
                self._stopping = True
                self.programs = {}
                for runner in _runners.values():
                    runner.events.clear()
                subscription, self._subscription = self._subscription, None
            if subscription is not None:
                subscription.close()

    def _on_event(self, bus: EventBus, event: Event) -> None:
        with _cond:
            if self._stopping or bus is not self._bus or event.seq <= self._last_seq:
                return
            self._last_seq = event.seq
            for program in self.programs.values():
                if event.type in program.events:
                    _enqueue(program.id, _Item(event))


_engine: Optional[_Engine] = None


def _configure(engine: _Engine) -> None:
    from services.automation import events
    from services.automation.models import load_programs

    programs = load_programs(_load_settings())
    keep = {p.id for p in programs}
    with _records_lock:
        for program_id in [p for p in _records if p not in keep]:
            del _records[program_id]
    engine.configure(programs, events.bus())


def start() -> None:
    """Begin running enabled programs on their events; idempotent."""
    global _engine
    with _cond:
        if _engine is None:
            _engine = _Engine()
        engine = _engine
    _configure(engine)


def stop() -> None:
    """Stop; waiting events are dropped, a running program finishes within its limit."""
    global _engine
    with _cond:
        engine, _engine = _engine, None
    if engine is not None:
        engine.stop()


def reload() -> None:
    """Saved programs changed."""
    with _cond:
        engine = _engine
    if engine is not None:
        _configure(engine)


def run_test(program_id: str) -> Future:
    """Run one saved program with a ``program.test`` event; Future[RunRecord].

    Works whether or not the program is enabled. It waits for a run of the
    same program already under way, then goes ahead of its waiting events.
    The Future never holds an exception.
    """
    from services.automation.models import find_program
    from services.automation.webhooks import _synthetic_event

    future: Future = Future()
    try:
        program = find_program(_load_settings(), program_id)
    except Exception as exc:
        program = None
        logger.warning("Could not read saved programs: %s", type(exc).__name__)
    if program is None:
        future.set_result(RunRecord("", _iso_now(), False, None, 0, "Save the program first."))
        return future
    event = _synthetic_event(TEST_EVENT_TYPE, {
        "message": "This is a test run from OpenWhisper.",
        "program_id": program.id,
    })
    with _cond:
        _enqueue(program.id, _Item(event, program, future))
    return future
