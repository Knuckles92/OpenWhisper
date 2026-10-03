"""A small Agent Client Protocol (ACP) client over an agent's stdio.

ACP is JSON-RPC 2.0, one message per line. The client asks
(``initialize``, ``session/new``, ``session/prompt``, ...); the agent streams
``session/update`` notifications and may ask the client things of its own
(``session/request_permission``, ``fs/*``, ``terminal/*``), which OpenWhisper
refuses unless a handler says otherwise.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Sequence, Tuple
from services.agent_process import popen_agent

from services.installed_agents import (
    agent_child_env,
    agent_workspace_dir,
    kill_process_tree,
    popen_flags,
)

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
CLIENT_INFO = {"name": "openwhisper", "title": "OpenWhisper", "version": "1"}
#: OpenWhisper never lets an agent touch files or run commands.
CLIENT_CAPABILITIES = {
    "fs": {"readTextFile": False, "writeTextFile": False},
    "terminal": False,
}
_STDERR_TAIL_LINES = 40


class AcpError(RuntimeError):
    """An ACP request failed: an error reply, a timeout, or a dead agent."""

    def __init__(self, message: str, code: Optional[int] = None,
                 data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[AcpError] = None


NotificationHandler = Callable[[str, Dict[str, Any]], None]
RequestHandler = Callable[[str, Dict[str, Any]], Any]


class AcpConnection:
    """One agent process speaking ACP on stdio.

    Args:
        argv: Command line, e.g. ``[path_to_opencode, "acp"]``.
        env: Process environment.
        cwd: Working folder.
        on_notification: ``cb(method, params)`` for agent notifications,
            called on the reader thread.
        on_request: ``cb(method, params) -> result`` for agent requests;
            raise :class:`AcpError` to refuse. Unhandled requests are refused.
    """

    def __init__(self, argv: Sequence[str], *, env: Dict[str, str], cwd: str,
                 on_notification: Optional[NotificationHandler] = None,
                 on_request: Optional[RequestHandler] = None) -> None:
        self._argv = list(argv)
        self._env = env
        self._cwd = cwd
        self._on_notification = on_notification
        self._on_request = on_request
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: Dict[int, _Pending] = {}
        self._next_id = 1
        self._stderr_tail: Deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._closed = False

    def start(self) -> None:
        """Start the agent. Raises :class:`AcpError` when it cannot run."""
        try:
            self._proc = popen_agent(
                self._argv, cwd=self._cwd, env=self._env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1, **popen_flags(),
            )
        except OSError as exc:
            raise AcpError(f"could not start the agent: {exc}") from exc
        threading.Thread(target=self._read_stdout, name="acp-stdout",
                         daemon=True).start()
        threading.Thread(target=self._read_stderr, name="acp-stderr",
                         daemon=True).start()

    @property
    def alive(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None and not self._closed

    def stderr_tail(self) -> str:
        """The agent's last few stderr lines, for an error message."""
        with self._lock:
            return "\n".join(self._stderr_tail)

    def request(self, method: str, params: Dict[str, Any], timeout_s: float,
                *, cancel_event: Optional[threading.Event] = None,
                stall_s: Optional[float] = None,
                last_activity: Optional[Callable[[], float]] = None) -> Any:
        """Send a request and wait for its reply.

        Args:
            method: ACP method.
            params: Its params.
            timeout_s: Hard wall.
            cancel_event: When set, stop waiting and raise ``AcpError("canceled")``;
                the caller sends any protocol-level cancel itself.
            stall_s: Fail when ``last_activity()`` is older than this.
            last_activity: Monotonic time of the request's latest progress.

        Returns:
            The reply's ``result``.

        Raises:
            AcpError: On an error reply, timeout, stall, cancel, or exit.
        """
        pending = _Pending()
        with self._lock:
            if not self.alive:
                raise AcpError("the agent is not running")
            msg_id = self._next_id
            self._next_id += 1
            self._pending[msg_id] = pending
        try:
            self._write({"jsonrpc": "2.0", "id": msg_id, "method": method,
                         "params": params})
            started = time.monotonic()
            deadline = started + timeout_s
            while not pending.event.wait(0.25):
                now = time.monotonic()
                if cancel_event is not None and cancel_event.is_set():
                    raise AcpError("canceled")
                if not self.alive:
                    raise AcpError(self._exit_message())
                if now >= deadline:
                    raise AcpError(f"{method} timed out after {timeout_s:.0f}s")
                if stall_s is not None:
                    last = last_activity() if last_activity else started
                    if now - max(last, started) >= stall_s:
                        raise AcpError(
                            f"{method} stalled after {stall_s:.0f}s without progress"
                        )
            if pending.error is not None:
                raise pending.error
            return pending.result
        finally:
            with self._lock:
                self._pending.pop(msg_id, None)

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        """Send a notification; a dead agent is ignored."""
        try:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})
        except AcpError:
            logger.debug("ACP notify %s after exit", method)

    def close(self, wait_s: float = 3.0) -> None:
        """Close stdin, give the agent a moment to exit, then stop it."""
        self._closed = True
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=wait_s)
        except subprocess.TimeoutExpired:
            pass
        finally:
            kill_process_tree(proc)
        self._fail_pending("the agent was closed")

    # ---- internals ----

    def _write(self, msg: Dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise AcpError("the agent is not running")
            try:
                proc.stdin.write(line)
                proc.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AcpError(f"could not write to the agent: {exc}") from exc

    def _exit_message(self) -> str:
        proc = self._proc
        code = proc.poll() if proc is not None else None
        tail = self.stderr_tail().strip().splitlines()[-3:]
        detail = f": {' / '.join(tail)}" if tail else ""
        return f"the agent exited (code {code}){detail}"

    def _fail_pending(self, reason: str) -> None:
        with self._lock:
            pending = list(self._pending.values())
        for entry in pending:
            if entry.error is None and entry.result is None:
                entry.error = AcpError(reason)
            entry.event.set()

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    logger.debug("ACP agent wrote a non-JSON line (%d bytes)", len(line))
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except Exception:
            logger.debug("ACP reader stopped", exc_info=True)
        finally:
            self._fail_pending(self._exit_message())

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        try:
            for raw in proc.stderr:
                text = raw.rstrip()
                if text:
                    with self._lock:
                        self._stderr_tail.append(text[:400])
                    logger.debug("agent stderr: %s", text[:400])
        except Exception:
            pass

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if isinstance(method, str):
            if "id" in msg:
                self._answer(msg["id"], method, params)
            elif self._on_notification is not None:
                try:
                    self._on_notification(method, params)
                except Exception:
                    logger.exception("ACP notification handler failed for %s", method)
            return
        msg_id = msg.get("id")
        with self._lock:
            pending = self._pending.get(msg_id) if isinstance(msg_id, int) else None
        if pending is None:
            return
        error = msg.get("error")
        if isinstance(error, dict):
            pending.error = AcpError(str(error.get("message") or "error"),
                                     error.get("code"), error.get("data"))
        else:
            pending.result = msg.get("result")
        pending.event.set()

    def _answer(self, msg_id: Any, method: str, params: Dict[str, Any]) -> None:
        reply: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}
        try:
            if self._on_request is None:
                raise AcpError(f"{method} is not supported", -32601)
            reply["result"] = self._on_request(method, params)
        except AcpError as exc:
            reply["error"] = {"code": exc.code or -32601, "message": str(exc)}
        except Exception as exc:
            logger.exception("ACP request handler failed for %s", method)
            reply["error"] = {"code": -32603, "message": str(exc)}
        try:
            self._write(reply)
        except AcpError:
            pass


def initialize(conn: AcpConnection, timeout_s: float = 30.0) -> Dict[str, Any]:
    """Run the ACP handshake; return the agent's ``initialize`` result."""
    result = conn.request("initialize", {
        "protocolVersion": PROTOCOL_VERSION,
        "clientCapabilities": CLIENT_CAPABILITIES,
        "clientInfo": CLIENT_INFO,
    }, timeout_s)
    return result if isinstance(result, dict) else {}


def _select_options(option: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a select config option's choices, grouped or not."""
    flat: List[Dict[str, Any]] = []
    for entry in option.get("options") or []:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("options"), list):
            flat.extend(e for e in entry["options"] if isinstance(e, dict))
        else:
            flat.append(entry)
    return flat


def config_option(session: Dict[str, Any], option_id: str) -> Optional[Dict[str, Any]]:
    """One of a session's ``configOptions`` by id, or None."""
    for option in session.get("configOptions") or []:
        if isinstance(option, dict) and option.get("id") == option_id:
            return option
    return None


def option_values(session: Dict[str, Any], option_id: str) -> List[str]:
    option = config_option(session, option_id)
    return [str(o.get("value")) for o in _select_options(option or {}) if o.get("value")]


def model_choices(session: Dict[str, Any]) -> List[Tuple[str, str]]:
    """``(value, label)`` for every model a ``session/new`` result offers."""
    option = config_option(session, "model")
    choices: List[Tuple[str, str]] = []
    if option is not None:
        for entry in _select_options(option):
            value = entry.get("value")
            if isinstance(value, str) and value:
                choices.append((value, str(entry.get("name") or value)))
        return choices
    models = session.get("models") or {}  # the pre-configOptions draft
    for entry in models.get("availableModels") or []:
        if isinstance(entry, dict) and isinstance(entry.get("modelId"), str):
            choices.append((entry["modelId"], str(entry.get("name") or entry["modelId"])))
    return choices


def list_opencode_models(path: str, timeout_s: float = 20.0) -> List[Tuple[str, str]]:
    """Every model the user's OpenCode can run, via a short ACP handshake.

    Starts ``opencode acp`` in OpenWhisper's empty workspace, opens and
    deletes one session, and exits. OpenCode's background service is not
    started.
    """
    conn = AcpConnection(
        [path, "acp"],
        env=agent_child_env({"OPENCODE_DISABLE_PROJECT_CONFIG": "1"}),
        cwd=agent_workspace_dir(),
    )
    conn.start()
    try:
        info = initialize(conn, timeout_s)
        session = conn.request("session/new", {
            "cwd": agent_workspace_dir(), "mcpServers": [],
        }, timeout_s)
        session = session if isinstance(session, dict) else {}
        choices = model_choices(session)
        session_id = session.get("sessionId")
        capabilities = (info.get("agentCapabilities") or {}).get("sessionCapabilities") or {}
        if session_id and "delete" in capabilities:
            try:
                conn.request("session/delete", {"sessionId": session_id}, 10.0)
            except AcpError:
                logger.debug("Could not delete the model-list session", exc_info=True)
        return choices
    finally:
        conn.close()
