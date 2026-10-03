"""A total pass budget bounds trickling HTTP and excludes late tool writes."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openai import OpenAI

from meeting.agent import openrouter_direct as direct
from services.text_generation import ToolCall


OP = {"op": "set_topic", "text": "Late budget", "evidence": ["sg_1"]}


def _response(json_mode):
    text = json.dumps({"ops": [OP]}) if json_mode else "Done."
    calls = [] if json_mode else [ToolCall("late", SimpleNamespace(
        name="patch_state", arguments=json.dumps({"ops": [OP]})))]
    return SimpleNamespace(text=text, tool_calls=calls, usage=None,
                           assistant_message={"role": "assistant", "content": text})


@pytest.mark.parametrize("json_mode", [False, True])
def test_cancel_drops_late_writes_and_prevents_overlapping_requests(monkeypatch, json_mode):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def generate(*args, **kwargs):
        calls.append(args)
        entered.set()
        release.wait(5)
        return _response(json_mode)

    monkeypatch.setattr(direct, "generate", generate)
    agent = direct.DirectOpenRouterAgent()
    agent._tools = MagicMock()
    run = agent._run_json_mode if json_mode else agent._run_tool_mode
    results = []
    caller = threading.Thread(target=lambda: results.append(run(MagicMock(), "sys", "user", 5)))
    caller.start()
    try:
        assert entered.wait(2)
        agent.cancel()
        caller.join(1)
        assert not caller.is_alive()
        assert not results[0].ok and results[0].error == "canceled"
        agent._cancel_event.clear()
        blocked = run(MagicMock(), "sys", "user", 1)
        assert not blocked.ok and "still ending" in blocked.error
        assert len(calls) == 1
    finally:
        release.set()
        caller.join(2)
        agent._generation_thread.join(2)
    agent._tools.apply_agent_ops.assert_not_called()


def test_capability_probe_has_a_wall_deadline(monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(direct, "_PROBE_TIMEOUT_S", .1)
    monkeypatch.setattr(direct, "generate", lambda *a, **k: release.wait(5))
    agent = direct.DirectOpenRouterAgent()
    agent._profile = SimpleNamespace(kind="openrouter")
    started = time.monotonic()
    try:
        agent._probe_tool_support(MagicMock())
        assert time.monotonic() - started < .7
        assert agent._json_mode
    finally:
        release.set()
        agent._generation_thread.join(2)


def test_trickling_http_ends_before_late_patch_can_run():
    release = threading.Event()
    body = json.dumps({"id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None, "tool_calls": [{"id": "late", "type": "function",
            "function": {"name": "patch_state", "arguments": json.dumps({"ops": [OP]})}}]}}]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for _ in range(100):
                    if release.wait(.04):
                        break
                    self.wfile.write(b"1\r\n \r\n")
                    self.wfile.flush()
                self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body))
                self.wfile.flush()
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    agent = direct.DirectOpenRouterAgent()
    agent._tools = MagicMock()
    agent._model = "m"
    client = OpenAI(api_key="synthetic", base_url=f"http://127.0.0.1:{server.server_port}/v1", max_retries=0)
    try:
        started = time.monotonic()
        agent._pass_deadline = started + .2
        result = agent._run_tool_mode(client, "sys", "user", .2)
        assert not result.ok and "deadline exceeded" in result.error
        assert time.monotonic() - started < .9
        release.set()
        agent._generation_thread.join(2)
        assert not agent._generation_thread.is_alive()
        agent._tools.apply_agent_ops.assert_not_called()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        client.close()
