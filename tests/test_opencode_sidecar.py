"""OpenCode-specific authority, lifecycle, packaging, and actual SDK integration."""
import json
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock

import pytest

from meeting.agent.base import create_agent_core
from meeting.agent.opencode_sidecar import OpenCodeSidecarAgent
from meeting.agent.sidecar import PASS_NOTES, PASS_POLISH
from meeting.interfaces import AgentConfig, CheckpointPayload, OpResult
from services import components
from services.component_leases import acquire_component, component_mutation
from services.opencode_catalog import SDK_VERSION, BUN_VERSION
from services.opencode_component import isolated_environment, runnable


def test_factory_does_not_fall_back_when_opencode_missing():
    with pytest.raises(RuntimeError, match="Downloads"):
        create_agent_core("opencode", None)
    assert isinstance(create_agent_core("opencode", "payload"), OpenCodeSidecarAgent)


def test_setting_round_trip_keeps_pi_default():
    from services.settings import MeetingAgentCore, SettingsKey, resolve_meeting_agent_core
    assert MeetingAgentCore.OPENCODE in MeetingAgentCore.ALL
    assert resolve_meeting_agent_core({SettingsKey.MEETING_AGENT_CORE: "opencode"}) == "opencode"
    assert resolve_meeting_agent_core({}) == "pi"


def test_handshake_rejects_runtime_sdk_and_protocol_drift():
    agent = OpenCodeSidecarAgent("payload")
    hello = dict(harness="opencode", harness_version=SDK_VERSION, runtime_version=BUN_VERSION,
                 request_scoped_tools=1, host_prompt=1,
                 text_protocols=["chat", "responses", "anthropic", "google"])
    assert agent._validate_hello(hello)
    for key in hello:
        changed = {**hello, key: None}
        assert not agent._validate_hello(changed)
    agent.shutdown()


def test_environment_has_no_ambient_cli_config_preloads_or_provider_keys(tmp_path):
    env = isolated_environment({
        "PATH": "system-path", "SYSTEMROOT": "windows",
        "BUN_OPTIONS": "--preload evil.js", "NODE_OPTIONS": "--require evil.js",
        "OPENCODE_CONFIG_CONTENT": "ambient", "ANTHROPIC_API_KEY": "unrelated",
        "OPENWHISPER_LLM_API_KEY": "selected", "OPENWHISPER_SIDECAR_TOKEN": "token",
        "HTTPS_PROXY": "proxy",
    }, str(tmp_path))
    assert env["OPENWHISPER_LLM_API_KEY"] == "selected"
    assert env["HTTPS_PROXY"] == "proxy"
    assert not ({"BUN_OPTIONS", "NODE_OPTIONS", "OPENCODE_CONFIG_CONTENT", "ANTHROPIC_API_KEY"} & env.keys())
    assert env["HOME"] == env["USERPROFILE"] == str(tmp_path)
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "TMP", "TEMP"):
        assert Path(env[name]).is_relative_to(tmp_path)
        assert Path(env[name]).is_dir()


def test_component_lease_blocks_update_and_remove_before_any_mutation(monkeypatch):
    component = components.ComponentId.MEETING_AGENT_OPENCODE
    install, uninstall = Mock(), Mock()
    monkeypatch.setattr(components, "_install_component", install)
    monkeypatch.setattr(components, "_uninstall_component", uninstall)
    release = acquire_component(component)
    try:
        with pytest.raises(components.ComponentError, match="in use"):
            components.install_component(component, {}, Mock(), threading.Event())
        with pytest.raises(components.ComponentError, match="in use"):
            components.uninstall_component(component)
        install.assert_not_called()
        uninstall.assert_not_called()
    finally:
        release()
        release()
    with component_mutation(component):
        with pytest.raises(components.ComponentError):
            acquire_component(component)
    components.uninstall_component(component)
    uninstall.assert_called_once()


def test_failed_initialize_releases_root_and_component(monkeypatch, tmp_path):
    from meeting.agent.sidecar import SidecarAgent
    monkeypatch.setattr(components, "current_platform_tag", lambda: "win_amd64")
    roots = []
    def fail(agent, *_):
        roots.append(agent._runtime_root)
        raise RuntimeError("expected failure")
    monkeypatch.setattr(SidecarAgent, "initialize", fail)
    agent = OpenCodeSidecarAgent(str(tmp_path))
    with pytest.raises(RuntimeError, match="expected"):
        agent.initialize(AgentConfig("m", "openrouter", "test", "key", "charter"), Mock())
    assert roots and not Path(roots[0]).exists()
    with component_mutation(components.ComponentId.MEETING_AGENT_OPENCODE):
        pass


@pytest.fixture
def authorized_agent():
    agent = OpenCodeSidecarAgent("unused")
    agent._initialized = True
    agent._tools = Mock()
    agent._tools.apply_agent_ops.side_effect = lambda ops: [OpResult(ok=True, op=op) for op in ops]
    agent._tools.segment_exists.return_value = True
    agent._tools.ask_question.return_value = OpResult(ok=True, op={"op": "ask_question"})
    agent._write_msg = Mock()
    agent._active_request_ids.add("active")
    agent._request_contexts["active"] = {"pass": "cards", "notes": False, "evidence": ["sg_1"]}
    agent._checkpoint_op_results["active"] = []
    yield agent
    agent.shutdown()


@pytest.mark.parametrize("request_id", [None, "", "expired", "revoked"])
def test_unscoped_expired_and_canceled_tools_cannot_write(authorized_agent, request_id):
    agent = authorized_agent
    if request_id == "revoked":
        agent._active_request_ids.add(request_id)
        agent._request_contexts[request_id] = {}
        agent._revoked_requests.add(request_id)
    agent._handle_tool_request({"id": 1, "method": "tool.patch_state",
                               "params": {"request_id": request_id, "ops": [{"op": "set_topic", "text": "bad"}]}})
    agent._tools.apply_agent_ops.assert_not_called()
    assert "error" in agent._write_msg.call_args.args[0]


def test_stale_generation_cannot_write_or_reset_progress(authorized_agent):
    agent = authorized_agent
    agent._handle_tool_request({"id": 1, "method": "tool.patch_state", "params": {
        "request_id": "active", "ops": [{"op": "set_topic", "text": "bad"}]}}, generation=-1)
    agent._tools.apply_agent_ops.assert_not_called()
    before = agent._last_progress_mono
    agent._handle_notification("progress", {
        "request_id": "active", "event": "message_update", "delta": "text_delta"}, generation=-1)
    assert agent._last_progress_mono == before


@pytest.mark.parametrize("kind", [PASS_POLISH, PASS_NOTES])
def test_pass_authority_uses_request_snapshot_and_preserves_actual_results(authorized_agent, kind):
    agent = authorized_agent
    agent._pass_kind = "cards"  # An overlapping newer pass must not change this one's authority.
    agent._request_contexts["active"].update({"pass": kind, "notes": kind == PASS_NOTES, "notes_ids": frozenset()})
    allowed = ({"op": "revise_segment_text", "segment_id": "sg_1", "text": "corrected", "evidence": ["sg_1"]}
               if kind == PASS_POLISH else {"op": "add_item", "card": "live_notes", "text": "note", "evidence": ["sg_1"]})
    denied = {"op": "set_topic", "text": "bad", "evidence": ["sg_1"]}
    agent._handle_tool_request({"id": 1, "method": "tool.patch_state",
                               "params": {"request_id": "active", "ops": [denied, allowed]}})
    agent._tools.apply_agent_ops.assert_called_once_with([allowed])
    results = agent._checkpoint_op_results["active"]
    assert [r.ok for r in results] == [False, True]
    assert results[1].op == allowed


def test_question_operations_are_reported_to_scheduler(authorized_agent):
    agent = authorized_agent
    agent._handle_tool_request({"id": 1, "method": "tool.ask_question", "params": {
        "request_id": "active", "text": "Who owns this?", "evidence": ["sg_1"]}})
    assert agent._checkpoint_op_results["active"][0].op["op"] == "ask_question"


def test_payload_resolution_is_platform_gated_and_does_not_hide_broken_install(monkeypatch, tmp_path):
    monkeypatch.setattr(components, "current_platform_tag", lambda: "linux_x86_64")
    assert components.meeting_agent_payload_dir("opencode") is None
    monkeypatch.setattr(components, "current_platform_tag", lambda: "win_amd64")
    monkeypatch.setattr(components, "is_installed", lambda _: True)
    monkeypatch.setattr(components, "read_manifest", lambda _: None)
    monkeypatch.setattr(components, "component_dir", lambda _: str(tmp_path))
    assert components.meeting_agent_payload_dir("opencode") is None


def test_payload_validation_rejects_incomplete_tree(tmp_path):
    from services.opencode_component import validate_payload
    assert not runnable(str(tmp_path))
    with pytest.raises(components.ComponentError):
        validate_payload(str(tmp_path))


def test_python_supervisor_with_packaged_sdk_all_meeting_passes():
    """Real Python RPC -> Bun -> embedded SDK -> mock provider -> real state store."""
    payload = Path(os.environ.get("OPENWHISPER_TEST_OPENCODE_PAYLOAD", "sidecar-opencode/dist")).resolve()
    if components.current_platform_tag() != "win_amd64" or not runnable(str(payload)):
        pytest.skip("Build the Windows OpenCode payload to run SDK integration")
    from benchmarks.meeting_mode.product_eval import ProductEvalHost
    from meeting.agent.prompts import build_system_prompt
    segments = [dict(id="sg_1", start_s=1, end_s=4, text="Budget is five hundred.", channel="mic")]
    host = ProductEvalHost("synthetic_opencode", segments)
    host.allow_agent_writes()
    calls = []
    desired_ops = []
    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            done = any(m["role"] == "tool" for m in body["messages"])
            delta = {"content": "Done"} if done else {"tool_calls": [{
                "index": 0, "id": "call_test", "type": "function",
                "function": {"name": "patch_state", "arguments": json.dumps({"ops": desired_ops})},
            }]}
            chunks = [
                {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop" if done else "tool_calls"}],
                 "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}},
            ]
            data = "".join("data: " + json.dumps({"id": "chatcmpl-test", "object": "chat.completion.chunk", **c}) + "\n\n" for c in chunks).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    agent = OpenCodeSidecarAgent(str(payload))
    try:
        agent.initialize(AgentConfig("synthetic_opencode", "custom_test", "test", None, build_system_prompt(), endpoint={
            "profile_id": "custom_test", "name": "Synthetic", "kind": "custom",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key_env": "",
        }), host)
        root = agent._runtime_root
        modes = [
            ({}, {"op": "set_topic", "text": "Budget", "evidence": ["sg_1"]}),
            ({"is_notes": True}, {"op": "add_item", "card": "live_notes", "text": "Budget cap is $500.", "data": {"heading": "Budget", "start_s": 1}, "evidence": ["sg_1"]}),
            ({"is_polish": True}, {"op": "revise_segment_text", "segment_id": "sg_1", "text": "Budget is $500.", "evidence": ["sg_1"]}),
            ({"is_consolidation": True}, {"op": "set_rolling_summary", "text": "The budget cap is $500.", "evidence": ["sg_1"]}),
        ]
        for i, (flags, op) in enumerate(modes):
            desired_ops[:] = [op]
            result = agent.checkpoint(CheckpointPayload("req_" + str(i), host.store.snapshot(), host.get_transcript(), **flags))
            assert result.ok, result.error
            assert len(result.op_results) == 1
            assert result.op_results[0].ok, result.op_results[0].reason
            assert result.op_results[0].op["op"] == op["op"]
        assert len(calls) == 8
        assert all(not any(m["role"] == "tool" for m in call["messages"]) for call in calls[::2])
        assert host.store.snapshot()["cards"]["live_notes"]
        assert host.get_transcript()[0]["text"] == "Budget is $500."
        assert agent.is_healthy()
    finally:
        agent.shutdown()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
    assert not Path(root).exists()


def test_stale_runtime_reaper_keeps_active_unmarked_and_linked_roots(monkeypatch, tmp_path):
    from services.opencode_component import clean_stale_runtime_dirs
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("meeting.recovery._pid_alive", lambda pid: pid == 111)
    for pid in (111, 222, 333):
        root = tmp_path / f"openwhisper-opencode-{pid}-test"
        root.mkdir()
        (root / "private.txt").write_text("synthetic")
        if pid != 333:
            (root / ".openwhisper-owner.json").write_text(json.dumps({"application": "OpenWhisper", "pid": pid}))
    clean_stale_runtime_dirs()
    assert (tmp_path / "openwhisper-opencode-111-test").exists()
    assert not (tmp_path / "openwhisper-opencode-222-test").exists()
    assert (tmp_path / "openwhisper-opencode-333-test").exists()


def test_timeout_preserves_real_operations_and_revokes_late_tools(authorized_agent):
    agent = authorized_agent
    agent._cfg = AgentConfig("m", "openrouter", "test", "key", "charter")
    agent.is_healthy = lambda: True
    op = {"op": "revise_segment_text", "segment_id": "sg_1", "text": "corrected", "evidence": ["sg_1"]}
    def rpc(method, params, **kwargs):
        if method == "cancel":
            return {"ok": True}
        agent._handle_tool_request({"id": 7, "method": "tool.patch_state", "params": {
            "request_id": params["request_id"], "ops": [op]}})
        raise TimeoutError("synthetic stall")
    agent._rpc = rpc
    result = agent.checkpoint(CheckpointPayload("timeout", {}, [{"id": "sg_1", "text": "draft"}]))
    assert not result.ok
    assert result.op_results[0].op == op
    before = agent._tools.apply_agent_ops.call_count
    agent._handle_tool_request({"id": 8, "method": "tool.patch_state", "params": {
        "request_id": "timeout", "ops": [op]}})
    assert agent._tools.apply_agent_ops.call_count == before
