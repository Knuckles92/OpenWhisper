"""``AgentCore`` for meetings run by the user's installed coding agent.

Each checkpoint becomes one agent run: the same host-written prompts Pi
uses, plus a tool contract, with OpenWhisper's meeting tools as the only
tools. Every tool call is answered by :func:`meeting.agent.tool_policy.run_tool`
under the pass's own :class:`ToolScope`, exactly as for Pi and the direct
agent, so a notes pass can only touch notes and a polish pass only
transcript text, and every op is validated by the state-patch layer.

Runs are slower than the built-in engines (10-20 s) and bill the user's own
plan, so the scheduler gives these cores a wider :attr:`cadence`.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from meeting.agent.base import CONSOLIDATION_STALL_S, CONSOLIDATION_TIMEOUT_CAP_S
from meeting.agent.installed.drivers import (
    AgentUnavailable,
    PassOutcome,
    PassRequest,
    make_driver,
    tool_contract,
)
from meeting.agent.installed.mcp_server import LoopbackMcpServer
from meeting.agent.prompts import (
    build_checkpoint_user_prompt,
    build_note_taker_system_prompt,
    build_notes_user_prompt,
)
from meeting.agent.scheduler import INSTALLED_AGENT_CADENCE
from meeting.agent.tool_policy import ToolScope, run_tool, tool_result_text
from meeting.agent.tool_specs import mcp_tool_definitions
from meeting.finalization import POLISH_TIMEOUT_S
from meeting.interfaces import (
    AgentConfig,
    AgentResult,
    AgentToolHost,
    CheckpointPayload,
    OpResult,
)
from services.installed_agents import (
    AGENT_SPECS,
    InstalledAgent,
    resolve_agent,
    sign_in_hint,
)

logger = logging.getLogger(__name__)

#: Live passes: a hard wall and a silence limit per run.
_LIVE_TIMEOUT_S = 120.0
_LIVE_STALL_S = 75.0
#: Live passes favour speed; the final report may think harder.
LIVE_EFFORT = "low"
FINAL_EFFORT = "medium"
#: Repeat the same progress line at most this often.
_PROGRESS_MIN_INTERVAL_S = 1.0

#: Driver event kind -> the Pi session event whose copy and activity kind
#: :mod:`meeting.agent.sidecar` already defines.
_PI_EVENTS = {
    "start": ("agent_start", ""),
    "thinking": ("message_update", "thinking_delta"),
    "writing": ("message_update", "text_delta"),
    "tool": ("tool_execution_start", ""),
    "turn": ("turn_start", ""),
    "retry": ("auto_retry_start", ""),
    "settled": ("agent_end", ""),
}


def installed_agent(agent_id: str) -> InstalledAgent:
    """The runnable install of ``agent_id``, or :class:`AgentUnavailable`."""
    spec = AGENT_SPECS.get(agent_id)
    if spec is None:
        raise AgentUnavailable(f"Unknown meeting agent {agent_id!r}.")
    agent = resolve_agent(agent_id)
    if agent is None:
        raise AgentUnavailable(
            f"{spec.name} is not installed on this computer. Choose another agent "
            "in Settings → Meeting Mode → Intelligence."
        )
    if agent.problem:
        raise AgentUnavailable(agent.problem)
    if agent.signed_in is False:
        raise AgentUnavailable(f"{spec.name} is not signed in. {sign_in_hint(agent_id)}")
    return agent


class InstalledAgentCore:
    """Meeting intelligence through Claude Code, Codex, or OpenCode.

    Args:
        agent_id: ``MeetingAgentCore`` value of an installed agent.
    """

    #: Read by :class:`CheckpointScheduler`.
    cadence = INSTALLED_AGENT_CADENCE

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self._cfg: Optional[AgentConfig] = None
        self._tools: Optional[AgentToolHost] = None
        self._agent: Optional[InstalledAgent] = None
        self._server: Optional[LoopbackMcpServer] = None
        self._driver: Any = None
        self._lock = threading.Lock()
        self._active: Set[threading.Event] = set()
        self._initialized = False
        self._shut_down = False
        self._progress_cb: Optional[Any] = None
        self._activity_cb: Optional[Any] = None
        self._last_progress = ("", 0.0)

    @property
    def name(self) -> str:
        return AGENT_SPECS[self.agent_id].name

    # ---- AgentCore ----

    def initialize(self, cfg: AgentConfig, tools: AgentToolHost) -> None:
        """Find the agent, start the tool server, and ready its driver.

        Raises:
            RuntimeError: When the agent is missing, too old, or signed out;
                the message says what to do.
        """
        self._cfg = cfg
        self._tools = tools
        self._shut_down = False
        self._agent = installed_agent(self.agent_id)
        server = LoopbackMcpServer()
        server.start()
        driver = make_driver(self._agent)
        contract = tool_contract(mcp_tool_definitions(), self.name,
                                 getattr(driver, "tool_prefix", ""))
        #: ``build`` writes cards, polish, and the final report under the
        #: copilot charter; ``plan`` is the note taker.
        self._personas = {
            "build": f"{cfg.system_prompt or ''}\n\n{contract}",
            "plan": f"{build_note_taker_system_prompt()}\n\n{contract}",
        }
        try:
            driver.start(server, self._personas)
        except Exception:
            server.stop()
            raise
        self._server = server
        self._driver = driver
        self._initialized = True
        logger.info(
            "Meeting agent ready: %s %s at %s (model=%s)",
            self.name, self._agent.version, self._agent.path, cfg.model or "default",
        )

    def checkpoint(self, payload: CheckpointPayload) -> AgentResult:
        if payload.is_consolidation:
            return self.consolidate(payload)
        timeout_s = max(POLISH_TIMEOUT_S, _LIVE_TIMEOUT_S) if payload.is_polish else _LIVE_TIMEOUT_S
        return self._run(payload, timeout_s, _LIVE_STALL_S, LIVE_EFFORT)

    def consolidate(self, payload: CheckpointPayload) -> AgentResult:
        return self._run(payload, CONSOLIDATION_TIMEOUT_CAP_S, CONSOLIDATION_STALL_S,
                         FINAL_EFFORT)

    def cancel(self) -> None:
        with self._lock:
            events = list(self._active)
        for event in events:
            event.set()

    def is_healthy(self) -> bool:
        driver = self._driver
        return (self._initialized and not self._shut_down
                and driver is not None and bool(driver.healthy()))

    def shutdown(self) -> None:
        self._shut_down = True
        self.cancel()
        driver, self._driver = self._driver, None
        server, self._server = self._server, None
        if driver is not None:
            try:
                driver.close()
            except Exception:
                logger.debug("Agent driver close failed", exc_info=True)
        if server is not None:
            server.stop()
        self._initialized = False

    def set_progress_callback(self, callback: Optional[Any]) -> None:
        """Receive human-readable progress while a consolidation runs."""
        self._progress_cb = callback

    def set_activity_callback(self, callback: Optional[Any]) -> None:
        """Receive an ``AgentActivity`` for the dashboard's activity strip."""
        self._activity_cb = callback

    # ---- one pass ----

    def _run(self, payload: CheckpointPayload, timeout_s: float,
             stall_s: float, effort: str) -> AgentResult:
        if self._shut_down or not self.is_healthy():
            return AgentResult(ok=False, error="agent_unavailable")
        assert self._cfg is not None and self._tools is not None
        scope = ToolScope.for_payload(payload)
        tools_host = self._tools
        results: List[OpResult] = []
        cancel = threading.Event()

        def handle(name: str, args: Dict[str, Any], _meta: Dict[str, Any]):
            if cancel.is_set():
                return "This meeting pass was canceled.", True
            out, op_results = run_tool(tools_host, name, args, scope)
            results.extend(op_results)
            return tool_result_text(name, out), False

        tools = mcp_tool_definitions()
        persona = "plan" if payload.is_notes else "build"
        system_prompt = self._personas[persona]
        user_prompt = (
            build_notes_user_prompt(payload.state_snapshot, payload.new_segments)
            if payload.is_notes else build_checkpoint_user_prompt(
                payload.state_snapshot, payload.new_segments,
                payload.is_consolidation, payload.is_polish,
            )
        )
        request = PassRequest(
            system_prompt=system_prompt, user_prompt=user_prompt, tools=tools,
            handler=handle, model=self._cfg.model or "", effort=effort, persona=persona,
            timeout_s=timeout_s, stall_s=stall_s, cancel_event=cancel,
            on_event=lambda kind, tool: self._on_event(kind, tool, scope.pass_kind),
        )
        with self._lock:
            self._active.add(cancel)
        logger.info("Dispatching %s pass request_id=%s to %s (%d segments)",
                    scope.pass_kind, payload.request_id, self.name,
                    len(payload.new_segments or []))
        try:
            outcome: PassOutcome = self._driver.run_pass(request)
        except Exception as exc:
            logger.exception("%s pass failed", self.name)
            outcome = PassOutcome(ok=False, error=str(exc))
        finally:
            cancel.set()  # no authority after the run, whatever the agent does
            with self._lock:
                self._active.discard(cancel)
        applied = sum(1 for r in results if r.ok)
        logger.info("%s pass request_id=%s %s: %d/%d ops applied%s", self.name,
                    payload.request_id, "ok" if outcome.ok else "failed",
                    applied, len(results),
                    f" ({outcome.error})" if outcome.error else "")
        if outcome.canceled:
            return AgentResult(ok=False, op_results=results, error="canceled",
                               usage=outcome.usage)
        if not outcome.ok:
            return AgentResult(ok=False, op_results=results, error=outcome.error,
                               usage=outcome.usage)
        return AgentResult(ok=True, op_results=results, usage=outcome.usage)

    def _on_event(self, kind: str, tool: str, pass_kind: str) -> None:
        from meeting.agent.sidecar import AgentActivity, _activity_kind, _progress_detail

        event, delta = _PI_EVENTS.get(kind, ("update", ""))
        detail = _progress_detail(event, delta, pass_kind)
        progress = self._progress_cb
        # Streaming agents report every token; the finalization card needs
        # a change of wording, or a heartbeat.
        last_detail, last_at = self._last_progress
        now = time.monotonic()
        if callable(progress) and (detail != last_detail
                                   or now - last_at >= _PROGRESS_MIN_INTERVAL_S):
            self._last_progress = (detail, now)
            try:
                progress(detail)
            except Exception:
                logger.debug("Progress callback failed", exc_info=True)
        activity_cb = self._activity_cb
        if callable(activity_cb):
            try:
                activity_cb(AgentActivity(
                    kind=_activity_kind(event, delta), label=detail, tool=tool,
                    pass_kind=pass_kind, ts=datetime.now(timezone.utc).isoformat(),
                ))
            except Exception:
                logger.debug("Activity callback failed", exc_info=True)


def run_agent_task(agent_id: str, *, model: str, system_prompt: str,
                   user_prompt: str, tools: List[Dict[str, Any]], handler: Any,
                   timeout_s: float, closing: str, effort: str = FINAL_EFFORT,
                   cancel_event: Optional[threading.Event] = None) -> PassOutcome:
    """Run one self-contained task (a custom report) through an installed agent.

    Starts a tool server and the agent's driver, runs one pass, and stops
    both. ``tools`` and ``handler`` (``handler(name, args) -> (text,
    is_error)``) are the task's own read-only tools; ``closing`` says what
    the final reply must be, since that reply is the task's result.

    Raises:
        AgentUnavailable: When the agent cannot run.
    """
    agent = installed_agent(agent_id)
    name = AGENT_SPECS[agent_id].name
    server = LoopbackMcpServer()
    server.start()
    driver = make_driver(agent)
    try:
        charter = f"{system_prompt}\n\n" + tool_contract(
            tools, name, getattr(driver, "tool_prefix", ""), closing)
        driver.start(server, {"build": charter}, tools)
        request = PassRequest(
            system_prompt=charter,
            user_prompt=user_prompt, tools=tools,
            handler=lambda n, a, _m: handler(n, a), model=model, effort=effort,
            timeout_s=timeout_s, stall_s=CONSOLIDATION_STALL_S,
            cancel_event=cancel_event or threading.Event(),
        )
        return driver.run_pass(request)
    finally:
        try:
            driver.close()
        finally:
            server.stop()
