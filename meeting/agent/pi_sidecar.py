"""Pi sidecar agent core: Node process hosting the Pi SDK over stdio RPC.

Spawns the bundled sidecar (``node.exe`` + ``bundle.cjs``), speaks NDJSON
JSON-RPC 2.0 on stdio, bridges tool calls back to the ``AgentToolHost``, and
restarts the child on health failures within a bounded budget.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Set

from meeting.agent.base import find_provider_api_key
from meeting.agent.prompts import build_note_taker_system_prompt
from meeting.interfaces import (
    AgentConfig,
    AgentResult,
    AgentToolHost,
    CheckpointPayload,
    OpResult,
)
from meeting.agent.evidence import repair_evidence_ids
from meeting.state.patches import filter_notes_ops, live_note_ids

logger = logging.getLogger(__name__)

_JSONRPC = "2.0"
_PROTOCOL_VERSION = 1

_HELLO_TIMEOUT_S = 10.0
_CHECKPOINT_TIMEOUT_S = 60.0
_CONSOLIDATION_TIMEOUT_S = 180.0
_RPC_DEFAULT_TIMEOUT_S = 30.0
_PING_INTERVAL_S = 10.0
_PING_TIMEOUT_S = 5.0
_PING_MISS_LIMIT = 3
_RESTART_BACKOFFS_S = (1.0, 5.0, 30.0)
_MAX_RESTARTS = 3
_RESTART_WINDOW_S = 600.0  # 10 minutes
_SHUTDOWN_WAIT_S = 2.0
_TERMINATE_WAIT_S = 1.5

_BUNDLE_NAME = "bundle.cjs"
_NODE_EXE_NAME = "node.exe"


@dataclass
class _Pending:
    """A host-originated RPC awaiting a response from the sidecar."""
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[BaseException] = None


def _serialize_op_result(result: OpResult) -> Dict[str, Any]:
    """Serialize an ``OpResult`` for the tool-bridge RPC response."""
    return {
        "ok": result.ok,
        "reason": result.reason,
        "target_id": result.target_id,
        "seq": result.seq,
        "current_revision": result.current_revision,
    }


def _op_results_from_counts(applied: int, rejected: int) -> List[OpResult]:
    """Rebuild per-op results from the sidecar's applied/rejected tallies.

    Every op the sidecar forwarded was validated and applied by the tool bridge
    at the time, which returned the real ``OpResult`` over RPC; only the tallies
    survive in the checkpoint response. Reconstruct placeholders so callers see
    the true counts instead of an empty list.

    Args:
        applied: Number of ops the host applied during the checkpoint.
        rejected: Number of ops the host rejected during the checkpoint.

    Returns:
        Placeholder ``OpResult`` objects, applied ones first.
    """
    results = [
        OpResult(ok=True, op={"op": "sidecar_tool_call"})
        for _ in range(max(0, applied))
    ]
    results.extend(
        OpResult(ok=False, op={"op": "sidecar_tool_call"}, reason="rejected")
        for _ in range(max(0, rejected))
    )
    return results


def _coerce_count(value: Any) -> int:
    """Best-effort int conversion for a sidecar-reported tally."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


class PiSidecarAgent:
    """``AgentCore`` implementation that drives the Node Pi sidecar over stdio.

    Attributes:
        supports_notes_pass: This core runs the dedicated note-taker pass.
            Notes checkpoints carry the note-taker system prompt to the
            bundle, and their tool calls are filtered to ``live_notes`` ops
            both here (tool bridge) and in the bundle — so even a bundle
            that predates ``is_notes`` cannot mutate anything but the notes
            page during a notes pass.
    """

    supports_notes_pass = True

    def __init__(self, payload_dir: str) -> None:
        """Args:
            payload_dir: Directory holding ``bundle.cjs`` and optionally a
                portable ``node.exe``.
        """
        self._payload_dir = payload_dir
        self._cfg: Optional[AgentConfig] = None
        self._tools: Optional[AgentToolHost] = None

        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._token: Optional[str] = None
        self._next_rpc_id = 1
        self._pending: Dict[Any, _Pending] = {}
        #: Request ids currently in flight. A consolidation pass can overlap a
        #: rolling checkpoint, so a single slot would cancel the wrong run.
        self._active_request_ids: Set[str] = set()

        self._hello_event = threading.Event()
        self._hello_ok = False
        self._hello_seen = False
        self._pi_version: Optional[str] = None

        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._health_thread: Optional[threading.Thread] = None
        #: Single worker so tool-bridge work (SQLite writes under the store
        #: lock) never blocks the stdout reader, which must keep answering
        #: pings. One worker also preserves the sidecar's tool-call ordering.
        self._tool_executor: Optional[ThreadPoolExecutor] = None

        self._stop_health = threading.Event()
        self._shut_down = False
        self._fatal = False
        self._recovering = False
        self._ping_misses = 0
        self._restart_times: Deque[float] = deque()
        self._restart_generation = 0
        self._initialized = False
        #: Notes-pass state for the tool bridge: set while a notes checkpoint
        #: RPC is in flight (the bundle serializes checkpoints, so tool calls
        #: arriving now belong to it), read on the single tool worker.
        self._notes_mode = False
        self._notes_item_ids: frozenset = frozenset()
        #: Segment ids shown in the in-flight checkpoint's payload — the
        #: citable universe for evidence-id repair in the tool bridge.
        self._citable_ids: List[str] = []

    # ------------------------------------------------------------------
    # AgentCore lifecycle
    # ------------------------------------------------------------------

    def initialize(self, cfg: AgentConfig, tools: AgentToolHost) -> None:
        """Spawn the sidecar, complete the hello handshake, and initialize Pi.

        Args:
            cfg: Meeting-scoped agent configuration (provider, model, key).
            tools: The tool host (``MeetingEngine``) that applies ops.

        Raises:
            RuntimeError: If the sidecar cannot be started or initialized.
        """
        self._cfg = cfg
        self._tools = tools
        self._fatal = False
        self._shut_down = False
        self._ping_misses = 0
        self._initialized = False

        api_key = cfg.api_key or find_provider_api_key(cfg.provider)
        if not api_key:
            self._fatal = True
            raise RuntimeError(
                f"No {cfg.provider} API key found; Pi sidecar agent offline"
            )

        self._ensure_tool_executor()
        self._spawn_and_handshake(api_key)
        self._send_initialize()
        self._initialized = True
        self._ensure_health_thread()
        logger.info(
            "Pi sidecar agent initialized (provider=%s model=%s pi_version=%s)",
            cfg.provider, cfg.model, self._pi_version,
        )

    def checkpoint(self, payload: CheckpointPayload) -> AgentResult:
        """Run one rolling checkpoint. Blocking; called from a worker thread."""
        timeout = (
            _CONSOLIDATION_TIMEOUT_S if payload.is_consolidation
            else _CHECKPOINT_TIMEOUT_S
        )
        return self._run_checkpoint(payload, timeout)

    def consolidate(self, payload: CheckpointPayload) -> AgentResult:
        """Run the end-of-meeting full pass. Blocking."""
        return self._run_checkpoint(payload, _CONSOLIDATION_TIMEOUT_S)

    def cancel(self) -> None:
        """Cancel every in-flight checkpoint by its ``request_id``."""
        with self._lock:
            request_ids = sorted(self._active_request_ids)
        for request_id in request_ids:
            try:
                self._rpc("cancel", {"request_id": request_id}, timeout_s=5.0)
            except Exception:
                logger.debug(
                    "Sidecar cancel RPC failed for request_id=%s",
                    request_id, exc_info=True,
                )

    def is_healthy(self) -> bool:
        """True when the sidecar process is alive and not permanently offline."""
        if self._shut_down or self._fatal or not self._initialized:
            return False
        proc = self._proc
        return proc is not None and proc.poll() is None

    def shutdown(self) -> None:
        """Ask the sidecar to exit, then terminate and join helper threads."""
        self._shut_down = True
        self._stop_health.set()
        try:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self._rpc("shutdown", {}, timeout_s=_SHUTDOWN_WAIT_S)
                except Exception:
                    logger.debug("Sidecar shutdown RPC failed", exc_info=True)
        finally:
            self._teardown_process(reject_reason="sidecar shut down")
            health = self._health_thread
            if health is not None and health is not threading.current_thread():
                health.join(timeout=2.0)
            self._health_thread = None
            executor = self._tool_executor
            self._tool_executor = None
            if executor is not None:
                # Queued tool calls are dropped (they would be rejected by the
                # shutdown guard anyway); a running one finishes on its own.
                executor.shutdown(wait=False, cancel_futures=True)
            self._initialized = False

    # ------------------------------------------------------------------
    # Checkpoint execution
    # ------------------------------------------------------------------

    def _run_checkpoint(self, payload: CheckpointPayload,
                        timeout_s: float) -> AgentResult:
        if self._shut_down or self._fatal:
            return AgentResult(ok=False, error="agent_unavailable")
        if self._cfg is None or self._tools is None or not self._initialized:
            return AgentResult(ok=False, error="not_initialized")
        if not self.is_healthy():
            # Best-effort recover before failing the call.
            if not self._try_recover("unhealthy before checkpoint"):
                return AgentResult(ok=False, error="agent_unavailable")

        with self._lock:
            self._active_request_ids.add(payload.request_id)
            is_notes = bool(getattr(payload, "is_notes", False))
            self._notes_mode = is_notes
            self._notes_item_ids = (
                live_note_ids(payload.state_snapshot) if is_notes else frozenset()
            )
            self._citable_ids = [
                str(seg.get("id"))
                for seg in (payload.new_segments or [])
                if isinstance(seg, dict) and seg.get("id")
            ]
        params: Dict[str, Any] = {
            "request_id": payload.request_id,
            "state": payload.state_snapshot,
            "new_segments": payload.new_segments,
            "is_consolidation": payload.is_consolidation,
            "is_polish": bool(getattr(payload, "is_polish", False)),
            "is_notes": is_notes,
        }
        if is_notes:
            # The note-taker persona replaces the copilot charter for this
            # pass. Bundles that predate is_notes ignore the extra fields;
            # the tool-bridge filter below still keeps them notes-only.
            params["system_prompt"] = build_note_taker_system_prompt()
        try:
            result = self._rpc("checkpoint", params, timeout_s=timeout_s)
        except Exception as exc:
            logger.warning("Sidecar checkpoint RPC failed: %s", exc)
            if not self._shut_down and not self._fatal:
                self._try_recover(f"checkpoint failure: {exc}")
            return AgentResult(ok=False, error=str(exc))
        finally:
            with self._lock:
                self._active_request_ids.discard(payload.request_id)
                self._notes_mode = False
                self._notes_item_ids = frozenset()
                self._citable_ids = []

        if not isinstance(result, dict):
            return AgentResult(ok=False, error="invalid checkpoint response")

        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        op_results = _op_results_from_counts(
            _coerce_count(result.get("applied")),
            _coerce_count(result.get("rejected")),
        )
        if result.get("canceled"):
            return AgentResult(
                ok=False, op_results=op_results, error="canceled", usage=usage,
            )
        return AgentResult(ok=True, op_results=op_results, usage=usage)

    # ------------------------------------------------------------------
    # Process spawn / teardown
    # ------------------------------------------------------------------

    def _bundle_path(self) -> str:
        return os.path.join(self._payload_dir, _BUNDLE_NAME)

    def _resolve_node_cmd(self) -> List[str]:
        """Build the argv to launch the sidecar bundle."""
        bundle = self._bundle_path()
        if not os.path.isfile(bundle):
            raise RuntimeError(f"sidecar bundle not found: {bundle}")
        portable = os.path.join(self._payload_dir, _NODE_EXE_NAME)
        if os.path.isfile(portable):
            return [portable, bundle]
        return ["node", bundle]

    def _build_env(self, api_key: str) -> Dict[str, str]:
        """Environment for the sidecar child process."""
        assert self._cfg is not None
        env = os.environ.copy()
        env["OPENWHISPER_SIDECAR_TOKEN"] = self._token or ""
        # The sidecar currently reads OPENROUTER_API_KEY for the session key.
        env["OPENROUTER_API_KEY"] = api_key
        if self._cfg.provider == "openai":
            env["OPENAI_API_KEY"] = api_key
        if self._cfg.model:
            env["PI_MODEL"] = self._cfg.model
        return env

    def _spawn_and_handshake(self, api_key: str) -> None:
        """Start the child, reader threads, and wait for a valid hello."""
        self._teardown_process(reject_reason="sidecar respawn")
        self._token = secrets.token_urlsafe(32)
        self._hello_event.clear()
        self._hello_ok = False
        self._hello_seen = False
        self._pi_version = None

        cmd = self._resolve_node_cmd()
        env = self._build_env(api_key)
        creationflags = 0
        if sys.platform == "win32":
            creationflags = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]

        logger.info("Spawning Pi sidecar: %s", cmd)
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                creationflags=creationflags,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            self._fatal = True
            raise RuntimeError(f"failed to spawn Pi sidecar: {exc}") from exc

        with self._lock:
            self._proc = proc
            self._restart_generation += 1
            generation = self._restart_generation

        # Both loops take the process as an argument: a teardown landing between
        # the assignment above and thread entry would otherwise make the reader
        # return before its try/finally, and _on_stdout_closed would never fire.
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="pi-sidecar-stdout",
            args=(generation, proc),
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop,
            name="pi-sidecar-stderr",
            args=(generation, proc),
            daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread.start()

        if not self._hello_event.wait(_HELLO_TIMEOUT_S):
            self._teardown_process(reject_reason="hello timeout")
            raise RuntimeError(
                f"sidecar hello timed out after {_HELLO_TIMEOUT_S:.0f}s"
            )
        if not self._hello_ok:
            hello_seen = self._hello_seen
            # The child dying before saying anything — a bundle that cannot
            # load, a missing runtime, a bad argv — is a completely different
            # failure from a rejected handshake, and reporting it as the latter
            # sends whoever reads the log to the wrong place entirely.
            exit_code = None if hello_seen else self._await_exit_code(proc)
            self._teardown_process(reject_reason="hello rejected")
            if not hello_seen:
                raise RuntimeError(
                    "sidecar process exited before the hello handshake "
                    f"(exit code {exit_code}); see the sidecar stderr log above"
                )
            raise RuntimeError("sidecar hello token/protocol mismatch")

    @staticmethod
    def _await_exit_code(proc: subprocess.Popen) -> Any:
        """Exit code of ``proc``, waiting briefly for a child that is dying."""
        code = proc.poll()
        if code is None:
            try:
                code = proc.wait(timeout=0.5)
            except Exception:
                code = None
        return "unknown" if code is None else code

    def _send_initialize(self) -> None:
        """Send the ``initialize`` RPC after a successful hello."""
        assert self._cfg is not None
        try:
            result = self._rpc(
                "initialize",
                {
                    "meeting_id": self._cfg.meeting_id,
                    "provider": self._cfg.provider,
                    "model": self._cfg.model,
                    "system_prompt": self._cfg.system_prompt or "",
                },
                timeout_s=_RPC_DEFAULT_TIMEOUT_S,
            )
        except Exception as exc:
            self._teardown_process(reject_reason="initialize failed")
            raise RuntimeError(f"sidecar initialize failed: {exc}") from exc
        if isinstance(result, dict):
            version = result.get("pi_version")
            if isinstance(version, str):
                self._pi_version = version
            if result.get("ok") is False:
                self._teardown_process(reject_reason="initialize rejected")
                raise RuntimeError("sidecar initialize returned ok=false")

    def _teardown_process(self, reject_reason: str) -> None:
        """Terminate the child and reject any in-flight host RPCs."""
        with self._lock:
            proc = self._proc
            self._proc = None
            pending = list(self._pending.items())
            self._pending.clear()
            self._restart_generation += 1

        for _, entry in pending:
            if entry.error is None:
                entry.error = RuntimeError(reject_reason)
            entry.event.set()

        if proc is None:
            return
        try:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=_TERMINATE_WAIT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass
        except Exception:
            logger.debug("Sidecar process teardown error", exc_info=True)
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # RPC transport
    # ------------------------------------------------------------------

    def _alloc_id(self) -> int:
        with self._lock:
            req_id = self._next_rpc_id
            self._next_rpc_id += 1
            return req_id

    def _write_msg(self, msg: Dict[str, Any]) -> None:
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        with self._write_lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.poll() is not None:
                raise RuntimeError("sidecar process is not running")
            proc.stdin.write(line)
            proc.stdin.flush()

    def _rpc(self, method: str, params: Dict[str, Any],
             timeout_s: float) -> Any:
        """Send a JSON-RPC request and wait for the correlated response."""
        req_id = self._alloc_id()
        pending = _Pending()
        with self._lock:
            if self._proc is None or self._proc.poll() is not None:
                raise RuntimeError("sidecar process is not running")
            self._pending[req_id] = pending
        try:
            self._write_msg({
                "jsonrpc": _JSONRPC,
                "id": req_id,
                "method": method,
                "params": params,
            })
            if not pending.event.wait(timeout_s):
                with self._lock:
                    self._pending.pop(req_id, None)
                raise TimeoutError(
                    f"RPC '{method}' timed out after {timeout_s:.0f}s"
                )
            if pending.error is not None:
                raise pending.error
            return pending.result
        finally:
            with self._lock:
                self._pending.pop(req_id, None)

    def _reader_loop(self, generation: int, proc: subprocess.Popen) -> None:
        """Parse NDJSON lines from sidecar stdout until EOF or generation bump.

        Args:
            generation: Restart generation this reader belongs to.
            proc: The child process this reader owns, passed in so a teardown
                racing thread start cannot strand the handshake.
        """
        if proc.stdout is None:
            self._on_stdout_closed(generation, proc)
            return
        stdout = proc.stdout
        try:
            for raw in stdout:
                with self._lock:
                    stale = (
                        self._proc is not proc
                        or generation != self._restart_generation
                    )
                if stale:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Sidecar sent unparseable stdout line (%d bytes)",
                        len(line),
                    )
                    continue
                if not isinstance(msg, dict):
                    continue
                # Re-check under the lock after parse so a restart/teardown
                # that landed mid-read cannot apply stale tool-bridge calls.
                with self._lock:
                    if (self._proc is not proc
                            or generation != self._restart_generation):
                        break
                self._dispatch_inbound(msg, generation)
        except Exception:
            logger.debug("Sidecar stdout reader stopped", exc_info=True)
        finally:
            self._on_stdout_closed(generation, proc)

    def _stderr_loop(self, generation: int, proc: subprocess.Popen) -> None:
        """Drain sidecar stderr into the module logger.

        Args:
            generation: Restart generation this drainer belongs to.
            proc: The child process this drainer owns.
        """
        if proc.stderr is None:
            return
        try:
            for raw in proc.stderr:
                with self._lock:
                    if (self._proc is not proc
                            or generation != self._restart_generation):
                        break
                text = raw.rstrip()
                if text:
                    logger.warning("sidecar stderr: %s", text)
        except Exception:
            logger.debug("Sidecar stderr reader stopped", exc_info=True)

    def _on_stdout_closed(self, generation: int, proc: subprocess.Popen) -> None:
        """Reject pending RPCs when the protocol stream ends."""
        with self._lock:
            if self._proc is not proc:
                return
            # Process died unexpectedly.
            pending = list(self._pending.items())
            self._pending.clear()
        for _, entry in pending:
            if entry.error is None:
                entry.error = RuntimeError("sidecar process exited")
            entry.event.set()
        if not self._hello_event.is_set():
            self._hello_ok = False
            self._hello_event.set()
        if not self._shut_down and not self._fatal:
            logger.warning("Pi sidecar process exited unexpectedly")

    def _dispatch_inbound(self, msg: Dict[str, Any],
                          generation: Optional[int] = None) -> None:
        """Route one inbound JSON-RPC message (request, notification, or response)."""
        method = msg.get("method")
        if isinstance(method, str):
            if "id" in msg:
                self._submit_tool_request(msg, generation)
            else:
                self._handle_notification(
                    method, msg.get("params") or {}, generation
                )
            return
        if "id" in msg:
            self._handle_response(msg)

    def _ensure_tool_executor(self) -> None:
        """Create the single-worker tool-bridge executor if it is not running."""
        with self._lock:
            if self._tool_executor is None:
                self._tool_executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="pi-sidecar-tools",
                )

    def _submit_tool_request(self, msg: Dict[str, Any],
                             generation: Optional[int]) -> None:
        """Hand a tool-bridge request to the worker, off the reader thread.

        ``apply_agent_ops`` holds the state-store lock across a full SQLite
        write; running it inline would stop the reader from correlating any
        inbound line — including ping responses — long enough for the health
        loop to kill and respawn a perfectly healthy sidecar.
        """
        executor = self._tool_executor
        if executor is None:
            self._handle_tool_request(msg, generation)
            return
        try:
            executor.submit(self._handle_tool_request, msg, generation)
        except RuntimeError:
            # Executor already shut down: the agent is tearing down and the
            # call would be rejected by the shutdown guard anyway.
            logger.debug(
                "Tool bridge executor is shut down; dropping %r",
                msg.get("method"),
            )

    def _handle_notification(self, method: str, params: Any,
                             generation: Optional[int] = None) -> None:
        if not isinstance(params, dict):
            params = {}
        if method == "hello":
            with self._lock:
                if (generation is not None
                        and generation != self._restart_generation):
                    return
                expected = self._token
            token = params.get("token")
            protocol = params.get("protocol")
            token_ok = (
                isinstance(token, str)
                and isinstance(expected, str)
                and len(token) == len(expected)
                and hmac.compare_digest(token, expected)
            )
            ok = token_ok and protocol == _PROTOCOL_VERSION
            self._hello_seen = True
            self._hello_ok = bool(ok)
            version = params.get("pi_version")
            if isinstance(version, str):
                self._pi_version = version
            if not ok:
                logger.error(
                    "Sidecar hello rejected (token_match=%s protocol=%r)",
                    token_ok, protocol,
                )
            self._hello_event.set()
            return
        if method == "log":
            level = str(params.get("level") or "info").lower()
            text = str(params.get("msg") or "")
            if level == "warning":
                logger.warning("sidecar: %s", text)
            elif level == "error":
                logger.error("sidecar: %s", text)
            elif level == "debug":
                logger.debug("sidecar: %s", text)
            else:
                logger.info("sidecar: %s", text)
            return
        logger.debug("Ignoring unknown sidecar notification %r", method)

    def _handle_response(self, msg: Dict[str, Any]) -> None:
        req_id = msg.get("id")
        with self._lock:
            pending = self._pending.get(req_id)
            # JSON numbers may arrive as int; also try string form.
            if pending is None and req_id is not None:
                pending = self._pending.get(str(req_id))
                if pending is not None:
                    req_id = str(req_id)
            if pending is None and isinstance(req_id, str) and req_id.isdigit():
                pending = self._pending.get(int(req_id))
                if pending is not None:
                    req_id = int(req_id)
        if pending is None:
            logger.debug("Response for unknown RPC id %r; dropped", msg.get("id"))
            return
        if msg.get("error"):
            err = msg["error"]
            message = (
                err.get("message") if isinstance(err, dict) else None
            ) or "remote error"
            pending.error = RuntimeError(str(message))
        else:
            pending.result = msg.get("result")
        pending.event.set()

    @staticmethod
    def _repair_evidence(
        ops: List[Dict[str, Any]],
        tools: Any,
        citable_ids: List[str],
    ) -> List[Dict[str, Any]]:
        """Repair truncated/typo'd evidence ids before exact-match validation."""
        exists = getattr(tools, "segment_exists", None)
        if not callable(exists) or not citable_ids:
            return ops
        repaired, count = repair_evidence_ids(ops, citable_ids, exists)
        if count:
            logger.info("Repaired %d mistyped evidence id(s)", count)
        return repaired

    def _handle_tool_request(self, msg: Dict[str, Any],
                             generation: Optional[int] = None) -> None:
        """Serve an awaited tool-bridge request from the sidecar."""
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        with self._lock:
            # Drop tool calls from a torn-down generation or after shutdown so
            # a dying child's buffered stdout cannot mutate meeting state.
            if (self._shut_down or not self._initialized
                    or (generation is not None
                        and generation != self._restart_generation)):
                return
            tools = self._tools
            notes_mode = self._notes_mode
            notes_item_ids = self._notes_item_ids
            citable_ids = self._citable_ids
        if tools is None:
            self._write_error(req_id, -32603, "tool host not initialized")
            return
        try:
            if method == "tool.patch_state":
                ops = params.get("ops")
                if not isinstance(ops, list):
                    raise ValueError("'ops' must be a list")
                if notes_mode:
                    # Notes-pass backstop: regardless of what the bundle
                    # allowed, only live_notes item ops reach the host.
                    ops = filter_notes_ops(ops, notes_item_ids)
                ops = self._repair_evidence(ops, tools, citable_ids)
                results = tools.apply_agent_ops(ops)
                payload: Dict[str, Any] = {
                    "results": [_serialize_op_result(r) for r in results],
                }
            elif method in ("tool.ask_question", "tool.resolve_question"):
                if notes_mode:
                    payload = _serialize_op_result(
                        OpResult(ok=False, op={"op": method}, reason="notes_only")
                    )
                elif method == "tool.ask_question":
                    result = tools.ask_question(
                        str(params.get("text") or ""),
                        list(params.get("evidence") or []),
                    )
                    payload = _serialize_op_result(result)
                else:
                    result = tools.resolve_question(
                        str(params.get("question_id") or ""),
                        str(params.get("answer_text") or ""),
                        float(params.get("confidence") or 0.0),
                        list(params.get("evidence") or []),
                    )
                    payload = _serialize_op_result(result)
            else:
                self._write_error(
                    req_id, -32601, f"method not found: {method}",
                )
                return
            self._write_msg({
                "jsonrpc": _JSONRPC,
                "id": req_id,
                "result": payload,
            })
        except Exception as exc:
            logger.warning("Tool bridge %s failed: %s", method, exc)
            try:
                self._write_error(req_id, -32603, str(exc))
            except Exception:
                logger.debug("Failed to send tool-bridge error", exc_info=True)

    def _write_error(self, req_id: Any, code: int, message: str) -> None:
        self._write_msg({
            "jsonrpc": _JSONRPC,
            "id": req_id,
            "error": {"code": code, "message": message},
        })

    # ------------------------------------------------------------------
    # Health / restart
    # ------------------------------------------------------------------

    def _ensure_health_thread(self) -> None:
        if self._health_thread is not None and self._health_thread.is_alive():
            return
        self._stop_health.clear()
        self._health_thread = threading.Thread(
            target=self._health_loop,
            name="pi-sidecar-health",
            daemon=True,
        )
        self._health_thread.start()

    def _health_loop(self) -> None:
        """Ping the sidecar every 10s; restart after consecutive misses."""
        while not self._stop_health.wait(_PING_INTERVAL_S):
            if self._shut_down or self._fatal:
                return
            if not self._initialized:
                continue
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._ping_misses = _PING_MISS_LIMIT
                self._try_recover("process not running")
                continue
            try:
                result = self._rpc("ping", {}, timeout_s=_PING_TIMEOUT_S)
                if isinstance(result, dict) and result.get("ok"):
                    self._ping_misses = 0
                    continue
                self._ping_misses += 1
            except Exception:
                self._ping_misses += 1
                logger.debug(
                    "Sidecar ping miss %d/%d",
                    self._ping_misses, _PING_MISS_LIMIT, exc_info=True,
                )
            if self._ping_misses >= _PING_MISS_LIMIT:
                self._try_recover("ping miss budget exhausted")

    def _try_recover(self, reason: str) -> bool:
        """Restart the sidecar with backoff; return True when healthy again."""
        with self._lock:
            if self._shut_down or self._fatal or self._cfg is None:
                return False
            # Collapse concurrent recoveries.
            if self._recovering:
                return False
            self._recovering = True

        try:
            return self._recover_locked(reason)
        finally:
            with self._lock:
                self._recovering = False

    def _recover_locked(self, reason: str) -> bool:
        assert self._cfg is not None
        now = time.monotonic()
        while self._restart_times and (
            now - self._restart_times[0]
        ) > _RESTART_WINDOW_S:
            self._restart_times.popleft()

        if len(self._restart_times) >= _MAX_RESTARTS:
            self._fatal = True
            self._initialized = False
            self._teardown_process(reject_reason="intelligence offline")
            logger.error(
                "Pi sidecar restart budget exhausted (%d/%d in %.0fs); "
                "intelligence offline (%s)",
                _MAX_RESTARTS, _MAX_RESTARTS, _RESTART_WINDOW_S, reason,
            )
            return False

        attempt = len(self._restart_times)
        backoff = _RESTART_BACKOFFS_S[min(attempt, len(_RESTART_BACKOFFS_S) - 1)]
        logger.warning(
            "Restarting Pi sidecar in %.0fs (attempt %d/%d): %s",
            backoff, attempt + 1, _MAX_RESTARTS, reason,
        )
        self._teardown_process(reject_reason="sidecar restarting")
        if self._stop_health.wait(backoff):
            return False

        api_key = self._cfg.api_key or find_provider_api_key(self._cfg.provider)
        if not api_key:
            self._fatal = True
            self._initialized = False
            logger.error("No API key available during sidecar restart")
            return False

        try:
            self._spawn_and_handshake(api_key)
            self._send_initialize()
        except Exception:
            logger.exception("Pi sidecar restart failed")
            self._restart_times.append(time.monotonic())
            if len(self._restart_times) >= _MAX_RESTARTS:
                self._fatal = True
                self._initialized = False
                logger.error(
                    "Pi sidecar restart budget exhausted; intelligence offline"
                )
            return False

        # Shutdown may have won the race while we were respawning — do not
        # resurrect an agent the host already tore down.
        if self._shut_down:
            self._teardown_process(reject_reason="sidecar shut down")
            self._initialized = False
            return False

        self._restart_times.append(time.monotonic())
        self._ping_misses = 0
        self._initialized = True
        logger.info("Pi sidecar restarted successfully")
        return True
