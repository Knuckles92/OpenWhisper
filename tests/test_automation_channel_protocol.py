"""The Claude Code channel's MCP handling, event parsing and setup snippets, in-process."""

from __future__ import annotations

import io
import json
import os
import queue
import sys
import threading
from pathlib import Path

import pytest

from services.agent_mcp import setup
from services.automation import channel_bridge as bridge
from services.automation.channel_bridge import (
    AppLink,
    ChannelServer,
    LinkError,
    MessageWriter,
    SseParser,
    channel_params,
    clean_meta,
)

TOKEN = "t0ken-for-the-channel-tests-0123456789abcdef"
REPO = Path(__file__).resolve().parents[1]


# -- server-sent events ---------------------------------------------------------


def _parse(lines: list[str], last_id: str = "") -> tuple[list, SseParser]:
    parser = SseParser(last_id)
    events = [event for line in lines if (event := parser.feed(line)) is not None]
    return events, parser


def test_sse_parser_reads_typed_events_with_ids():
    events, parser = _parse(
        [": ping", "", "id: 7", "event: channel.message", 'data: {"a":1}', "", "id: 8", "data: x", ""]
    )
    assert [(e.type, e.data, e.last_id) for e in events] == [
        ("channel.message", '{"a":1}', "7"),
        ("message", "x", "8"),
    ]
    assert parser.last_id == "8"


def test_sse_parser_joins_data_lines_and_strips_one_space():
    events, _ = _parse(["data:  two spaces", "data:none", "data", ""])
    assert events[0].data == " two spaces\nnone\n"


def test_sse_parser_moves_the_resume_point_only_when_a_block_ends():
    parser = SseParser("3")
    parser.feed("id: 4")
    parser.feed("data: half an event")
    assert parser.last_id == "3"  # broken here, the stream resumes before event 4
    parser.feed("")
    assert parser.last_id == "4"


def test_sse_parser_leaves_the_resume_point_alone_for_a_gap_and_retry():
    """The live API opens with ``retry:`` and sends ``events.gap`` without an ``id:`` line."""
    gap = json.dumps({"id": "", "seq": 0, "type": "events.gap", "data": {"missed_from_seq": 4}})
    events, parser = _parse(["retry: 2000", "", "event: events.gap", f"data: {gap}", ""], last_id="12")
    assert [(e.type, e.last_id) for e in events] == [("events.gap", "12")]
    assert parser.last_id == "12"


def test_sse_parser_keeps_ids_from_blocks_without_data_and_ignores_nul_ids():
    events, parser = _parse(["id: 9", "", "id: bad\0id", "data: y", ""])
    assert parser.last_id == "9"
    assert events[0].last_id == "9"


# -- what reaches Claude --------------------------------------------------------------


def test_clean_meta_keeps_only_what_claude_code_accepts():
    assert clean_meta(
        {
            "job_id": "abc",
            "app": "  Visual\nStudio   Code ",
            "with-hyphen": "dropped",
            "9lives": "dropped",
            "_ok": "kept",
            "empty": "   ",
            "number": 5,
            "none": None,
            "long": "x" * 500,
        }
    ) == {"job_id": "abc", "app": "Visual Studio Code", "_ok": "kept", "long": "x" * bridge.MAX_META_CHARS}


def test_channel_params_carry_the_text_and_routing_meta():
    event = {
        "type": "channel.message",
        "data": {
            "job_id": "0123456789abcdef",
            "action_id": "claude",
            "text": "  fix the flaky test\n",
            "app": {"id": "code", "name": "Visual Studio Code", "category": "editor"},
        },
    }
    assert channel_params(event) == {
        "content": "  fix the flaky test\n",
        "meta": {"job_id": "0123456789abcdef", "action": "claude", "app": "Visual Studio Code"},
    }


def test_channel_params_accept_an_app_name_and_leave_out_empty_meta():
    assert channel_params({"data": {"text": "hi", "app": "Terminal"}}) == {
        "content": "hi",
        "meta": {"app": "Terminal"},
    }
    assert channel_params({"data": {"text": "hi", "app": None, "action_id": None}}) == {"content": "hi"}


@pytest.mark.parametrize(
    "event",
    [None, [], {}, {"data": None}, {"data": {"text": ""}}, {"data": {"text": "  \n"}}, {"data": {"text": 5}}],
)
def test_channel_params_skip_events_without_text(event):
    assert channel_params(event) is None


# -- the MCP server -------------------------------------------------------------------


class _Pipe:
    """A blocking line reader fed by the test, like stdin."""

    def __init__(self) -> None:
        self._lines: queue.Queue = queue.Queue()

    def put(self, message) -> None:
        self._lines.put(message if isinstance(message, bytes) else json.dumps(message).encode() + b"\n")

    def close(self) -> None:
        self._lines.put(b"")

    def readline(self) -> bytes:
        return self._lines.get(timeout=10)


class _Output(io.RawIOBase):
    """Collects what the server writes, a message per line."""

    def __init__(self) -> None:
        super().__init__()
        self.raw = b""
        self.cond = threading.Condition()

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        with self.cond:
            self.raw += bytes(data)
            self.cond.notify_all()
        return len(data)

    def messages(self, count: int, timeout: float = 10) -> list[dict]:
        with self.cond:
            assert self.cond.wait_for(lambda: self.raw.count(b"\n") >= count, timeout)
            return [json.loads(line) for line in self.raw.splitlines()]


class _FakeLink:
    def __init__(self, error: Exception | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.error = error

    def notify(self, text: str, title: str = "") -> None:
        if self.error is not None:
            raise self.error
        self.sent.append((text, title))


class _FakeStream:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class _Harness:
    def __init__(self, link=None) -> None:
        self.stdin = _Pipe()
        self.stdout = _Output()
        self.link = link or _FakeLink()
        self.streams: list[_FakeStream] = []

        def make_stream():
            stream = _FakeStream()
            self.streams.append(stream)
            return stream

        self.server = ChannelServer(self.stdin, self.stdout, self.link, version="9.9.9", stream_factory=make_stream)
        self.code: list[int] = []
        self.thread = threading.Thread(target=lambda: self.code.append(self.server.serve()), daemon=True)
        self.thread.start()

    def finish(self) -> int:
        self.stdin.close()
        self.thread.join(10)
        return self.code[0]


@pytest.fixture
def harness():
    created: list[_Harness] = []

    def make(**kwargs) -> _Harness:
        created.append(_Harness(**kwargs))
        return created[-1]

    yield make
    for item in created:
        if item.thread.is_alive():
            item.finish()


@pytest.mark.parametrize(
    ("requested", "answered"),
    [
        ("2025-11-25", "2025-11-25"),
        ("2025-06-18", "2025-06-18"),
        ("2024-11-05", "2024-11-05"),
        ("2026-07-28", "2025-11-25"),
        ("1999-01-01", "2025-11-25"),
        (None, "2025-11-25"),
    ],
)
def test_initialize_negotiates_a_handshake_revision(harness, requested, answered):
    h = harness()
    h.stdin.put({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": requested}})
    result = h.stdout.messages(1)[0]["result"]
    assert result["protocolVersion"] == answered
    assert result["capabilities"] == {"experimental": {"claude/channel": {}}, "tools": {}}
    assert result["serverInfo"] == {"name": "openwhisper", "title": "OpenWhisper", "version": "9.9.9"}
    assert len(result["instructions"]) < 2048
    assert '<channel source="openwhisper"' in result["instructions"]
    assert h.finish() == 0


def test_discover_probe_and_unknown_methods_are_method_not_found(harness):
    h = harness()
    h.stdin.put({"jsonrpc": "2.0", "id": "probe", "method": "server/discover", "params": {}})
    h.stdin.put({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
    h.stdin.put({"jsonrpc": "2.0", "method": "notifications/something_new"})
    h.stdin.put({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    messages = h.stdout.messages(3)
    assert messages[0] == {
        "jsonrpc": "2.0",
        "id": "probe",
        "error": {"code": -32601, "message": "Method not found: server/discover"},
    }
    assert messages[1]["id"] == 2 and messages[1]["error"]["code"] == -32601
    assert messages[2] == {"jsonrpc": "2.0", "id": 3, "result": {}}
    assert h.finish() == 0
    assert len(h.stdout.messages(3)) == 3  # the notification got no answer


def test_malformed_input_gets_json_rpc_errors(harness):
    h = harness()
    h.stdin.put(b"{not json\n")
    h.stdin.put(b'[{"jsonrpc": "2.0", "id": 1, "method": "ping"}]\n')
    h.stdin.put(b'"just a string"\n')
    h.stdin.put({"jsonrpc": "2.0", "id": 4})
    h.stdin.put({"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": [1]})
    h.stdin.put({"jsonrpc": "2.0", "id": 6, "result": {}})  # a response: ignored
    h.stdin.put(b"\n")
    h.stdin.put({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    codes = [(m["id"], m.get("error", {}).get("code")) for m in h.stdout.messages(6)]
    assert codes == [(None, -32700), (None, -32600), (None, -32600), (4, -32600), (5, -32602), (7, None)]
    assert h.finish() == 0


def test_tools_list_offers_the_reply_tool(harness):
    h = harness()
    h.stdin.put({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = h.stdout.messages(1)[0]["result"]["tools"]
    assert tools == [bridge.REPLY_TOOL]
    schema = tools[0]["inputSchema"]
    assert schema["required"] == ["text"]
    assert schema["properties"]["text"]["maxLength"] == 500


def test_reply_posts_through_the_link(harness):
    h = harness()
    h.stdin.put(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "reply", "arguments": {"text": "  Done.  ", "title": " Tests \n passed "}},
        }
    )
    result = h.stdout.messages(1)[0]["result"]
    assert result == {"content": [{"type": "text", "text": "Shown to the user near their pointer."}], "isError": False}
    assert h.link.sent == [("Done.", "Tests passed")]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (None, "text is required"),
        ({}, "text is required"),
        ({"text": "   "}, "text is required"),
        ({"text": 5}, "text is required"),
        ({"text": "x" * 501}, "limited to 500 characters"),
        ({"text": "ok", "title": 5}, "title must be text"),
        ({"text": "ok", "title": "t" * 81}, "limited to 80 characters"),
        ("text", "Pass the reply as"),
    ],
)
def test_reply_rejects_bad_arguments_as_tool_errors(harness, arguments, expected):
    h = harness()
    params = {"name": "reply"}
    if arguments is not None:
        params["arguments"] = arguments
    h.stdin.put({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params})
    result = h.stdout.messages(1)[0]["result"]
    assert result["isError"] is True
    assert expected in result["content"][0]["text"]
    assert h.link.sent == []


def test_reply_failures_reach_claude_as_tool_errors(harness):
    h = harness(link=_FakeLink(LinkError("http_403", "allow running actions")))
    h.stdin.put({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "reply", "arguments": {"text": "x"}}})
    result = h.stdout.messages(1)[0]["result"]
    assert result == {"content": [{"type": "text", "text": "allow running actions"}], "isError": True}


def test_unknown_tools_are_invalid_params(harness):
    h = harness()
    h.stdin.put({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "rm_rf", "arguments": {}}})
    assert h.stdout.messages(1)[0]["error"] == {"code": -32602, "message": "Unknown tool: rm_rf"}


def test_the_stream_starts_once_after_initialized_and_stops_at_eof(harness):
    h = harness()
    h.stdin.put({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    h.stdout.messages(1)
    assert h.streams == []
    h.stdin.put({"jsonrpc": "2.0", "method": "notifications/initialized"})
    h.stdin.put({"jsonrpc": "2.0", "method": "notifications/initialized"})
    h.stdin.put({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    h.stdout.messages(2)
    assert len(h.streams) == 1 and h.streams[0].started == 1
    assert h.finish() == 0
    assert h.streams[0].stopped == 1


def test_channel_messages_are_notifications_on_their_own_line(harness):
    h = harness()
    h.server.send_channel_message({"content": "a\nb", "meta": {"job_id": "1"}})
    raw = h.stdout.messages(1) and h.stdout.raw
    assert raw.count(b"\n") == 1
    assert json.loads(raw) == {
        "jsonrpc": "2.0",
        "method": "notifications/claude/channel",
        "params": {"content": "a\nb", "meta": {"job_id": "1"}},
    }


def test_stepping_aside_for_an_update_closes_stdout_and_ends_the_process():
    exits: list[int] = []
    out = io.BytesIO()
    server = ChannelServer(_Pipe(), out, _FakeLink(), exit_process=exits.append)
    stream = server._default_stream()
    assert stream._on_update is not None
    stream._on_update()
    assert exits == [0]
    assert out.closed


def test_writer_sends_ascii_json_and_survives_a_lone_surrogate():
    out = io.BytesIO()
    writer = MessageWriter(out)
    assert writer.send({"text": "café ✓ \ud800"})
    line = out.getvalue()
    assert line.isascii() and line.endswith(b"\n")
    assert json.loads(line)["text"] == "café ✓ \ud800"


def test_writer_reports_a_closed_pipe():
    class Broken(io.RawIOBase):
        def writable(self):
            return True

        def write(self, data):
            raise BrokenPipeError

    writer = MessageWriter(Broken())
    assert writer.send({"a": 1}) is False
    assert writer.closed


# -- reaching the app -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("saved", "port"),
    [({"mcp_port": 9123}, 9123), ({}, 8767), ({"mcp_port": True}, 8767), ({"mcp_port": 70000}, 8767), ({"mcp_port": "9"}, 8767)],
)
def test_the_port_comes_from_settings_like_the_app_reads_it(saved, port):
    link = AppLink(read_settings=lambda: saved, read_token=lambda: TOKEN)
    assert link.target() == bridge.Target("127.0.0.1", port)


def test_unreadable_settings_fall_back_to_the_default_port():
    def broken():
        raise OSError("locked")

    assert AppLink(read_settings=broken).target().port == 8767


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("http://127.0.0.1:9000", bridge.Target("127.0.0.1", 9000)),
        ("http://localhost:9000/", bridge.Target("localhost", 9000)),
        ("https://host.example/base/", bridge.Target("host.example", 443, True, "/base")),
        ("http://[::1]:8767", bridge.Target("::1", 8767)),
    ],
)
def test_the_url_override_wins(url, target):
    link = AppLink(url=url, read_settings=lambda: pytest.fail("settings read"))
    assert link.target() == target


@pytest.mark.parametrize("url", ["127.0.0.1:8767", "ftp://host", "http://", "http://host:99999"])
def test_a_bad_url_override_is_reported(url):
    with pytest.raises(LinkError) as caught:
        AppLink(url=url).target()
    assert caught.value.state == "bad_url"


def test_the_token_is_read_once_and_again_after_a_refusal():
    reads = iter(["first-token", "second-token"])
    link = AppLink(read_token=lambda: next(reads))
    assert link.token() == "first-token"
    assert link.token() == "first-token"
    link.forget_token()
    assert link.token() == "second-token"


def test_a_missing_token_is_read_again_on_the_next_try():
    reads = iter([None, "late-token"])
    link = AppLink(read_token=lambda: next(reads))
    with pytest.raises(LinkError) as caught:
        link.token()
    assert caught.value.state == "no_token"
    assert link.token() == "late-token"


def test_the_token_override_is_never_reread():
    link = AppLink(token=" fixed ", read_token=lambda: pytest.fail("store read"))
    link.forget_token()
    assert link.token() == "fixed"


def test_unreachable_copy_names_mcp_when_it_is_off():
    assert "API & MCP is off" in AppLink(read_settings=lambda: {"mcp_enabled": False}).unreachable_message()
    assert "isn't running" in AppLink(read_settings=lambda: {"mcp_enabled": True}).unreachable_message()
    assert "isn't running" in AppLink(url="http://127.0.0.1:1").unreachable_message()


def test_headers_identify_the_channel_client():
    headers = AppLink(version="2.7.0").headers(TOKEN, Accept="text/event-stream")
    assert headers == {
        "Authorization": f"Bearer {TOKEN}",
        "X-OpenWhisper-Client": "claude-channel",
        "User-Agent": "OpenWhisper-channel/2.7.0",
        "Accept": "text/event-stream",
    }


def test_no_update_check_outside_the_frozen_windows_app():
    assert bridge.update_in_progress() is False


# -- process entry --------------------------------------------------------------------


def test_help_and_unexpected_arguments(capsys):
    assert bridge.main(["--help"]) == 0
    assert "claude --dangerously-load-development-channels server:openwhisper" in capsys.readouterr().err
    assert bridge.main(["--port", "1"]) == 2
    assert "Unexpected arguments" in capsys.readouterr().err


# -- setup snippets -------------------------------------------------------------------


def test_source_runs_start_this_interpreter_with_main_py(monkeypatch):
    import config

    monkeypatch.setattr(config, "is_frozen", lambda: False)
    command = setup.channel_command_line()
    assert command == [os.path.abspath(sys.executable), str(REPO / "main.py"), "--channel"]
    assert all(os.path.isabs(part) for part in command[:2])


def test_frozen_builds_start_their_own_executable(monkeypatch):
    import config

    monkeypatch.setattr(config, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", "OpenWhisper.exe")
    command = setup.channel_command_line()
    assert command == [os.path.abspath("OpenWhisper.exe"), "--channel"]


def test_mcp_json_entry_names_the_data_folder_only_when_it_is_unusual(monkeypatch, tmp_path):
    import config

    monkeypatch.setattr(config, "is_frozen", lambda: True)
    monkeypatch.setattr(config, "data_root", lambda: str(tmp_path))
    monkeypatch.setattr(config, "local_app_dir", lambda: str(tmp_path / "standard"))
    entry = json.loads(setup.claude_channel_config())["mcpServers"]["openwhisper"]
    assert entry["args"] == ["--channel"]
    assert entry["env"] == {"OPENWHISPER_DATA_DIR": str(tmp_path)}

    monkeypatch.setattr(config, "local_app_dir", lambda: str(tmp_path))
    entry = json.loads(setup.claude_channel_config())["mcpServers"]["openwhisper"]
    assert "env" not in entry
    assert set(entry) == {"command", "args"}


def test_snippets_carry_no_token(monkeypatch):
    from services import credentials

    credentials.store().set("OPENWHISPER_MCP_TOKEN", TOKEN)
    text = setup.claude_channel_config() + setup.claude_channel_command()
    assert TOKEN not in text and "Bearer" not in text
    assert setup.claude_channel_command() == "claude --dangerously-load-development-channels server:openwhisper"
