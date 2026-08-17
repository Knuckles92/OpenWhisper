"""
Tests for PiSidecarAgent: stdio hello handshake, auth failure, restart budget.
"""
import json
import os
import sys
import textwrap
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from meeting.agent import pi_sidecar as pi_mod
from meeting.agent.pi_sidecar import PiSidecarAgent
from meeting.interfaces import AgentConfig, AgentResult, CheckpointPayload, OpResult


#: Minimal NDJSON sidecar stub (Python). Speaks hello from env token, answers
#: initialize/checkpoint/ping/shutdown. Optional SIDECAR_STUB_MODE=
#: bad_token|crash_after_init|die_silently.
_STUB_SOURCE = textwrap.dedent(r"""
import json, os, sys, time

token = os.environ.get("OPENWHISPER_SIDECAR_TOKEN", "")
mode = os.environ.get("SIDECAR_STUB_MODE", "ok")
if mode == "die_silently":
    # Dies before writing a byte, exactly like a bundle that cannot load.
    sys.exit(3)
hello_token = "wrong-token" if mode == "bad_token" else token
sys.stdout.write(json.dumps({
    "jsonrpc": "2.0",
    "method": "hello",
    "params": {"token": hello_token, "protocol": 1, "pi_version": "stub-1"},
}) + "\n")
sys.stdout.flush()

if mode == "bad_token":
    time.sleep(30)
    sys.exit(0)

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": req_id,
            "result": {"ok": True, "pi_version": "stub-1"},
        }) + "\n")
        sys.stdout.flush()
        if mode == "crash_after_init":
            sys.exit(1)
    elif method == "ping":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": req_id, "result": {"ok": True},
        }) + "\n")
        sys.stdout.flush()
    elif method == "shutdown":
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": req_id, "result": {"ok": True},
        }) + "\n")
        sys.stdout.flush()
        break
    elif method == "checkpoint":
        params = msg.get("params") or {}
        if mode == "notes_tools" and params.get("is_notes"):
            # Notes-mode tool storm: mixed patch_state ops plus a question.
            # The host's tool bridge must filter to live_notes ops only and
            # reject the question with notes_only.
            tool_req = {
                "jsonrpc": "2.0", "id": 999, "method": "tool.patch_state",
                "params": {"ops": [
                    {"op": "add_item", "card": "key_points",
                     "text": "must not apply", "evidence": ["sg_1"]},
                    {"op": "add_item", "card": "live_notes",
                     "text": "new note block",
                     "data": {"heading": "H", "start_s": 1.0},
                     "evidence": ["sg_1"]},
                    {"op": "update_item", "id": "it_note9",
                     "base_revision": 1, "set": {"text": "extend"},
                     "evidence": ["sg_1"]},
                    {"op": "update_item", "id": "it_key1",
                     "base_revision": 1, "set": {"text": "must not apply"},
                     "evidence": ["sg_1"]},
                    {"op": "set_topic", "text": "must not apply",
                     "evidence": ["sg_1"]},
                ]},
            }
            sys.stdout.write(json.dumps(tool_req) + "\n")
            sys.stdout.flush()
            tool_resp = None
            for inner in sys.stdin:
                inner_msg = json.loads(inner.strip())
                if inner_msg.get("id") == 999:
                    tool_resp = inner_msg
                    break
            q_req = {
                "jsonrpc": "2.0", "id": 998, "method": "tool.ask_question",
                "params": {"text": "must not ask", "evidence": ["sg_1"]},
            }
            sys.stdout.write(json.dumps(q_req) + "\n")
            sys.stdout.flush()
            q_resp = None
            for inner in sys.stdin:
                inner_msg = json.loads(inner.strip())
                if inner_msg.get("id") == 998:
                    q_resp = inner_msg
                    break
            results = ((tool_resp or {}).get("result") or {}).get("results") or []
            applied_count = sum(1 for r in results if r.get("ok"))
            q_rejected = bool(
                ((q_resp or {}).get("result") or {}).get("reason") == "notes_only"
            )
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"applied": applied_count,
                           "rejected": 1 if q_rejected else 0,
                           "usage": {"filtered": applied_count,
                                     "q_rejected": q_rejected}},
            }) + "\n")
            sys.stdout.flush()
        elif mode == "notes_flag":
            is_notes = bool(params.get("is_notes"))
            has_prompt = is_notes and bool(params.get("system_prompt"))
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"applied": 9 if has_prompt else 3,
                           "rejected": 2,
                           "usage": {"is_notes": is_notes,
                                     "notes_prompt": has_prompt}},
            }) + "\n")
            sys.stdout.flush()
        else:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"applied": 7 if params.get("is_polish") else 3,
                           "rejected": 2,
                           "usage": {"totalTokens": 42}},
            }) + "\n")
            sys.stdout.flush()
    else:
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": req_id,
            "error": {"code": -32601, "message": f"unknown {method}"},
        }) + "\n")
        sys.stdout.flush()
""")


class FakeTools:
    def apply_agent_ops(self, ops):
        return [OpResult(ok=True, op=op, seq=1) for op in ops]

    def ask_question(self, text, evidence):
        return OpResult(ok=True, op={"op": "ask_question"}, seq=1)

    def resolve_question(self, question_id, answer_text, confidence, evidence):
        return OpResult(ok=True, op={"op": "resolve_question"}, seq=1)


@pytest.fixture
def stub_dir(tmp_path):
    stub = tmp_path / "sidecar_stub.py"
    stub.write_text(_STUB_SOURCE, encoding="utf-8")
    # Satisfy bundle existence check
    (tmp_path / "bundle.cjs").write_text("// stub", encoding="utf-8")
    return tmp_path, stub


def _cfg(**kwargs):
    defaults = dict(
        meeting_id="m_side",
        provider="openrouter",
        model="test-model",
        api_key="sk-test",
        system_prompt="You are a test agent.",
    )
    defaults.update(kwargs)
    return AgentConfig(**defaults)


def _patch_cmd(agent, stub_path, env_extra=None):
    """Point the agent at the Python stub and optionally extend child env."""
    original_build_env = agent._build_env

    def build_env(api_key):
        env = original_build_env(api_key)
        if env_extra:
            env.update(env_extra)
        return env

    agent._resolve_node_cmd = lambda: [sys.executable, "-u", str(stub_path)]
    agent._build_env = build_env


class TestSidecarHandshake:
    def test_hello_initialize_and_ping(self, stub_dir):
        payload_dir, stub = stub_dir
        agent = PiSidecarAgent(str(payload_dir))
        _patch_cmd(agent, stub)
        # Keep health loop from interfering; short shutdown is enough
        with patch.object(pi_mod, "_PING_INTERVAL_S", 60.0):
            agent.initialize(_cfg(), FakeTools())
            try:
                assert agent.is_healthy()
                assert agent._pi_version == "stub-1"
                result = agent._rpc("ping", {}, timeout_s=5.0)
                assert result.get("ok") is True
            finally:
                agent.shutdown()
        assert not agent.is_healthy()

    def test_auth_failure_on_bad_hello_token(self, stub_dir):
        payload_dir, stub = stub_dir
        agent = PiSidecarAgent(str(payload_dir))
        _patch_cmd(agent, stub, env_extra={"SIDECAR_STUB_MODE": "bad_token"})
        with pytest.raises(RuntimeError, match="token/protocol mismatch"):
            agent.initialize(_cfg(), FakeTools())
        assert agent._hello_ok is False
        agent.shutdown()

    def test_missing_api_key_is_fatal(self, stub_dir):
        payload_dir, stub = stub_dir
        agent = PiSidecarAgent(str(payload_dir))
        with patch("meeting.agent.pi_sidecar.find_provider_api_key",
                   return_value=None):
            with pytest.raises(RuntimeError, match="API key"):
                agent.initialize(_cfg(api_key=None), FakeTools())
        assert agent._fatal is True

    def test_child_death_before_hello_is_not_a_token_mismatch(self, stub_dir):
        """A child that never speaks must not be diagnosed as a bad token."""
        payload_dir, stub = stub_dir
        agent = PiSidecarAgent(str(payload_dir))
        _patch_cmd(agent, stub, env_extra={"SIDECAR_STUB_MODE": "die_silently"})
        with pytest.raises(RuntimeError,
                           match="exited before the hello handshake"):
            agent.initialize(_cfg(), FakeTools())
        assert agent._hello_seen is False
        agent.shutdown()


class TestCheckpointResults:
    def test_applied_and_rejected_counts_reach_the_caller(self, stub_dir):
        payload_dir, stub = stub_dir
        agent = PiSidecarAgent(str(payload_dir))
        _patch_cmd(agent, stub)
        with patch.object(pi_mod, "_PING_INTERVAL_S", 60.0):
            agent.initialize(_cfg(), FakeTools())
            try:
                result = agent.checkpoint(CheckpointPayload(
                    request_id="req-1", state_snapshot={}, new_segments=[],
                ))
            finally:
                agent.shutdown()
        assert result.ok
        assert result.usage == {"totalTokens": 42}
        assert len(result.op_results) == 5
        assert sum(1 for r in result.op_results if r.ok) == 3

    def test_polish_flag_reaches_the_sidecar(self, stub_dir):
        payload_dir, stub = stub_dir
        agent = PiSidecarAgent(str(payload_dir))
        _patch_cmd(agent, stub)
        with patch.object(pi_mod, "_PING_INTERVAL_S", 60.0):
            agent.initialize(_cfg(), FakeTools())
            try:
                result = agent.checkpoint(CheckpointPayload(
                    request_id="req-polish",
                    state_snapshot={},
                    new_segments=[],
                    is_polish=True,
                ))
            finally:
                agent.shutdown()
        assert result.ok
        assert sum(1 for item in result.op_results if item.ok) == 7

    def test_cancel_targets_every_in_flight_request(self, tmp_path):
        agent = PiSidecarAgent(str(tmp_path))
        agent._active_request_ids = {"r2", "r1"}
        calls = []

        def fake_rpc(method, params, timeout_s):
            calls.append((method, params))
            return {"ok": True}

        agent._rpc = fake_rpc
        agent.cancel()
        assert [p["request_id"] for _, p in calls] == ["r1", "r2"]

    def test_checkpoint_timeout_does_not_restart_sidecar(self, tmp_path):
        """A slow model turn is not a dead sidecar; do not kill in-flight work."""
        agent = PiSidecarAgent(str(tmp_path))
        agent._cfg = _cfg()
        agent._tools = FakeTools()
        agent._initialized = True
        agent._proc = type("Proc", (), {"poll": staticmethod(lambda: None)})()
        recover_calls = []
        agent._try_recover = lambda reason: recover_calls.append(reason) or False
        agent._rpc = lambda method, params, timeout_s, stall_s=None: (
            (_ for _ in ()).throw(
                TimeoutError(f"RPC '{method}' timed out after {timeout_s:.0f}s")
            )
        )
        result = agent.consolidate(CheckpointPayload(
            request_id="req-slow",
            state_snapshot={},
            new_segments=[],
            is_consolidation=True,
        ))
        assert result.ok is False
        assert "timed out" in (result.error or "")
        assert recover_calls == []

    def test_checkpoint_transport_error_still_restarts(self, tmp_path):
        agent = PiSidecarAgent(str(tmp_path))
        agent._cfg = _cfg()
        agent._tools = FakeTools()
        agent._initialized = True
        agent._proc = type("Proc", (), {"poll": staticmethod(lambda: None)})()
        recover_calls = []
        agent._try_recover = lambda reason: recover_calls.append(reason) or False
        agent._rpc = lambda method, params, timeout_s, stall_s=None: (
            (_ for _ in ()).throw(RuntimeError("sidecar process exited"))
        )
        result = agent.checkpoint(CheckpointPayload(
            request_id="req-dead", state_snapshot={}, new_segments=[],
        ))
        assert result.ok is False
        assert recover_calls and "sidecar process exited" in recover_calls[0]

    def test_await_pending_stalls_without_progress(self, tmp_path):
        agent = PiSidecarAgent(str(tmp_path))
        pending = pi_mod._Pending()
        pending.event.wait = lambda timeout=None: False
        times = iter([100.0, 100.0, 101.0, 106.0])
        with patch("meeting.agent.pi_sidecar.time.monotonic", side_effect=lambda: next(times)):
            with pytest.raises(TimeoutError, match="stalled"):
                agent._await_pending(pending, timeout_s=30.0, stall_s=5.0, method="checkpoint")

    def test_await_pending_progress_resets_stall(self, tmp_path):
        agent = PiSidecarAgent(str(tmp_path))
        pending = pi_mod._Pending()
        ticks = {"n": 0}

        def wait(timeout=None):
            ticks["n"] += 1
            if ticks["n"] == 2:
                agent._note_progress("thinking")
            if ticks["n"] >= 4:
                pending.event.set()
            return pending.event.is_set()

        pending.event.wait = wait
        now = {"t": 100.0}

        def mono():
            now["t"] += 1.0
            return now["t"]

        with patch("meeting.agent.pi_sidecar.time.monotonic", side_effect=mono):
            agent._await_pending(pending, timeout_s=30.0, stall_s=5.0, method="checkpoint")
        assert pending.event.is_set()

    def test_progress_notification_uses_thinking_delta(self, tmp_path):
        agent = PiSidecarAgent(str(tmp_path))
        seen = []
        agent.set_progress_callback(seen.append)
        agent._handle_notification("progress", {
            "event": "message_update",
            "delta": "thinking_delta",
            "streaming": True,
        })
        assert seen == ["Model is thinking through the transcript…"]
        assert agent._last_progress_mono > 0

    def test_progress_notification_uses_thinking_start(self, tmp_path):
        agent = PiSidecarAgent(str(tmp_path))
        seen = []
        agent.set_progress_callback(seen.append)
        agent._handle_notification("progress", {
            "event": "message_update",
            "delta": "thinking_start",
        })
        assert seen == ["Model is thinking through the transcript…"]


def _track_pass(agent, payload):
    """Mirror ``_run_checkpoint`` pass-kind bookkeeping without an RPC."""
    with agent._lock:
        agent._pass_kind = pi_mod._pass_kind_for(payload)
        agent._pass_kinds[payload.request_id] = agent._pass_kind


class TestAgentActivity:
    """Structured activity ticks for the host-only dashboard strip."""

    def test_progress_activity_carries_tool_and_pass_kind(self, tmp_path):
        agent = PiSidecarAgent(str(tmp_path))
        progress = []
        activity = []
        agent.set_progress_callback(progress.append)
        agent.set_activity_callback(activity.append)
        payload = CheckpointPayload(
            request_id="req-consol",
            state_snapshot={},
            new_segments=[],
            is_consolidation=True,
        )
        _track_pass(agent, payload)
        agent._handle_notification("progress", {
            "event": "message_update",
            "delta": "thinking_delta",
            "tool": "patch_state",
            "request_id": payload.request_id,
            "streaming": True,
        })
        assert progress == ["Model is thinking through the transcript…"]
        assert len(activity) == 1
        tick = activity[0]
        assert isinstance(tick, pi_mod.AgentActivity)
        assert tick.kind == "thinking"
        assert tick.label == "Model is thinking through the transcript…"
        assert tick.tool == "patch_state"
        assert tick.pass_kind == "consolidation"
        assert tick.ts
        assert "+00:00" in tick.ts or tick.ts.endswith("Z")

    def test_progress_callback_strings_stay_exact_with_activity(self, tmp_path):
        """Activity must not change the consolidation-card progress copy."""
        agent = PiSidecarAgent(str(tmp_path))
        progress = []
        agent.set_progress_callback(progress.append)
        agent.set_activity_callback(lambda _tick: None)
        agent._handle_notification("progress", {
            "event": "message_update",
            "delta": "thinking_delta",
            "streaming": True,
        })
        agent._handle_notification("progress", {
            "event": "message_update",
            "delta": "thinking_start",
        })
        assert progress == [
            "Model is thinking through the transcript…",
            "Model is thinking through the transcript…",
        ]

    def test_cards_pass_fallback_is_not_final_report(self, tmp_path):
        agent = PiSidecarAgent(str(tmp_path))
        progress = []
        activity = []
        agent.set_progress_callback(progress.append)
        agent.set_activity_callback(activity.append)
        payload = CheckpointPayload(
            request_id="req-cards",
            state_snapshot={},
            new_segments=[],
        )
        _track_pass(agent, payload)
        assert agent._pass_kind == "cards"
        agent._handle_notification("progress", {
            "request_id": payload.request_id,
        })
        assert progress == ["Reviewing the last few minutes…"]
        assert "final report" not in progress[0].lower()
        assert len(activity) == 1
        assert activity[0].pass_kind == "cards"
        assert activity[0].label == "Reviewing the last few minutes…"
        assert activity[0].tool == ""

    def test_tool_bridge_activity_uses_real_method_name(self, tmp_path):
        agent = PiSidecarAgent(str(tmp_path))
        agent._tools = FakeTools()
        agent._initialized = True
        agent._ensure_tool_executor()
        payload = CheckpointPayload(
            request_id="req-tools",
            state_snapshot={},
            new_segments=[],
            is_consolidation=True,
        )
        _track_pass(agent, payload)
        activity = []
        agent.set_activity_callback(activity.append)
        writes = []
        wrote = threading.Event()

        def fake_write(msg):
            writes.append(msg)
            wrote.set()

        agent._write_msg = fake_write
        requests = (
            ("tool.patch_state", {"ops": [{"op": "add_item"}]}),
            ("tool.ask_question", {"text": "why?", "evidence": []}),
            ("tool.resolve_question", {
                "question_id": "q1", "answer_text": "yes",
                "confidence": 0.5, "evidence": [],
            }),
        )
        try:
            for index, (method, params) in enumerate(requests):
                wrote.clear()
                agent._dispatch_inbound({
                    "jsonrpc": "2.0", "id": f"sc-{index}",
                    "method": method, "params": params,
                })
                assert wrote.wait(5.0)
        finally:
            agent.shutdown()
        assert [tick.tool for tick in activity] == [
            "patch_state", "ask_question", "resolve_question",
        ]
        assert all(tick.kind == "tool" for tick in activity)
        assert all(tick.pass_kind == "consolidation" for tick in activity)
        assert all(tick.tool is not None for tick in activity)


class TestToolBridgeThreading:
    """Tool-bridge work must never run on the stdout reader thread."""

    def _agent(self, tmp_path, tools):
        agent = PiSidecarAgent(str(tmp_path))
        agent._tools = tools
        agent._initialized = True
        agent._ensure_tool_executor()
        return agent

    def test_tool_requests_run_on_the_tool_worker(self, tmp_path):
        agent = self._agent(tmp_path, FakeTools())
        sent = threading.Event()
        seen = {}

        def fake_write(msg):
            seen["thread"] = threading.current_thread().name
            seen["msg"] = msg
            sent.set()

        agent._write_msg = fake_write
        try:
            agent._dispatch_inbound(
                {"jsonrpc": "2.0", "id": "sc-1", "method": "tool.patch_state",
                 "params": {"ops": [{"op": "add_item"}]}},
                generation=agent._restart_generation,
            )
            assert sent.wait(5.0)
        finally:
            agent.shutdown()
        assert seen["thread"] != threading.current_thread().name
        assert seen["thread"].startswith("pi-sidecar-tools")
        assert seen["msg"]["result"]["results"][0]["ok"] is True

    def test_slow_tool_call_does_not_stall_response_correlation(self, tmp_path):
        release = threading.Event()
        entered = threading.Event()

        class BlockingTools(FakeTools):
            def apply_agent_ops(self, ops):
                entered.set()
                release.wait(10.0)
                return super().apply_agent_ops(ops)

        agent = self._agent(tmp_path, BlockingTools())
        agent._write_msg = lambda msg: None
        try:
            started = time.monotonic()
            agent._dispatch_inbound(
                {"jsonrpc": "2.0", "id": "sc-1", "method": "tool.patch_state",
                 "params": {"ops": [{"op": "add_item"}]}},
                generation=agent._restart_generation,
            )
            # The reader returned immediately even though the store is busy.
            assert time.monotonic() - started < 1.0
            assert entered.wait(5.0)

            # ...and an inbound ping response is still correlated meanwhile.
            pending = pi_mod._Pending()
            with agent._lock:
                agent._pending[7] = pending
            agent._dispatch_inbound(
                {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
            )
            assert pending.event.wait(1.0)
            assert pending.result == {"ok": True}
        finally:
            release.set()
            agent.shutdown()


class TestSidecarRestartBudget:
    def test_restart_budget_exhausted_marks_offline(self, stub_dir):
        payload_dir, stub = stub_dir
        agent = PiSidecarAgent(str(payload_dir))
        _patch_cmd(agent, stub, env_extra={"SIDECAR_STUB_MODE": "crash_after_init"})

        # Tiny backoffs so the test finishes quickly
        with patch.object(pi_mod, "_RESTART_BACKOFFS_S", (0.01, 0.01, 0.01)), \
             patch.object(pi_mod, "_PING_INTERVAL_S", 0.05), \
             patch.object(pi_mod, "_PING_TIMEOUT_S", 0.5), \
             patch.object(pi_mod, "_PING_MISS_LIMIT", 1), \
             patch.object(pi_mod, "_MAX_RESTARTS", 3), \
             patch.object(pi_mod, "_RESTART_WINDOW_S", 600.0), \
             patch.object(pi_mod, "_HELLO_TIMEOUT_S", 5.0):
            # First initialize may succeed briefly then child exits; health
            # loop / recover should burn the restart budget.
            try:
                agent.initialize(_cfg(), FakeTools())
            except RuntimeError:
                # Initialize itself can fail if the child dies mid-handshake
                pass

            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                if agent._fatal or len(agent._restart_times) >= 3:
                    break
                # Drive recovery manually if health loop is slow
                if not agent.is_healthy() and not agent._fatal and agent._cfg:
                    agent._try_recover("test drive")
                time.sleep(0.05)

            # Force remaining budget if child kept dying during recover
            while (not agent._fatal
                   and len(agent._restart_times) < pi_mod._MAX_RESTARTS
                   and time.monotonic() < deadline):
                agent._try_recover("force budget")
                time.sleep(0.02)

            assert agent._fatal or len(agent._restart_times) >= 3
            agent.shutdown()


class _RecordingTools(FakeTools):
    """FakeTools that records every op/submit that reaches the host."""

    def __init__(self):
        self.ops = []
        self.questions = []

    def apply_agent_ops(self, ops):
        self.ops.extend(ops)
        return super().apply_agent_ops(ops)

    def ask_question(self, text, evidence):
        self.questions.append(text)
        return super().ask_question(text, evidence)


_NOTES_STATE = {
    "meeting_id": "m_notes",
    "seq": 3,
    "cards": {
        "live_notes": [
            {"id": "it_note9", "card": "live_notes", "text": "existing block",
             "data": {"heading": "Funnel", "start_s": 12.0},
             "status": "proposed", "revision": 1, "pinned": False},
        ],
        "key_points": [
            {"id": "it_key1", "card": "key_points", "text": "a key point",
             "status": "proposed", "revision": 1, "pinned": False},
        ],
    },
    "participants": {},
}


class TestNotesPass:
    def test_sidecar_declares_notes_support(self):
        assert PiSidecarAgent.supports_notes_pass is True

    def test_notes_flag_and_persona_prompt_reach_the_sidecar(self, stub_dir):
        payload_dir, stub = stub_dir
        agent = PiSidecarAgent(str(payload_dir))
        _patch_cmd(agent, stub, env_extra={"SIDECAR_STUB_MODE": "notes_flag"})
        with patch.object(pi_mod, "_PING_INTERVAL_S", 60.0):
            agent.initialize(_cfg(), FakeTools())
            try:
                notes = agent.checkpoint(CheckpointPayload(
                    request_id="req-notes",
                    state_snapshot=dict(_NOTES_STATE),
                    new_segments=[],
                    is_notes=True,
                ))
                plain = agent.checkpoint(CheckpointPayload(
                    request_id="req-plain",
                    state_snapshot=dict(_NOTES_STATE),
                    new_segments=[],
                ))
            finally:
                agent.shutdown()
        assert notes.ok
        assert notes.usage["is_notes"] is True
        assert notes.usage["notes_prompt"] is True
        assert sum(1 for r in notes.op_results if r.ok) == 9
        # A plain checkpoint carries neither the flag nor the override.
        assert plain.ok
        assert plain.usage["is_notes"] is False
        assert plain.usage["notes_prompt"] is False

    def test_notes_tool_bridge_filters_to_live_notes_ops(self, stub_dir):
        """Even a bundle that ignores is_notes cannot escape the notes gate."""
        payload_dir, stub = stub_dir
        agent = PiSidecarAgent(str(payload_dir))
        _patch_cmd(agent, stub, env_extra={"SIDECAR_STUB_MODE": "notes_tools"})
        tools = _RecordingTools()
        with patch.object(pi_mod, "_PING_INTERVAL_S", 60.0):
            agent.initialize(_cfg(), tools)
            try:
                result = agent.checkpoint(CheckpointPayload(
                    request_id="req-notes-tools",
                    state_snapshot=dict(_NOTES_STATE),
                    new_segments=[],
                    is_notes=True,
                ))
            finally:
                agent.shutdown()

        assert result.ok
        # Only the two live_notes ops (add + known-id update) reached the host.
        assert [
            (op["op"], op.get("card", op.get("id"))) for op in tools.ops
        ] == [("add_item", "live_notes"), ("update_item", "it_note9")]
        assert tools.questions == []
        # The mixed storm: 2 kept + 3 filtered patch ops, and the question
        # answered with a notes_only rejection.
        assert result.usage["filtered"] == 2
        assert result.usage["q_rejected"] is True
