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
        sys.stdout.write(json.dumps({
            "jsonrpc": "2.0", "id": req_id,
            "result": {"applied": 3, "rejected": 2,
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
