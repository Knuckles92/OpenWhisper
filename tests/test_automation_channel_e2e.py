"""The Claude Code channel against a fake OpenWhisper on real sockets.

``FakeApp`` serves the two routes the channel uses, the way the live API
contract describes them (docs/design/automation/design.md): an SSE stream of
``channel.message`` events with ``Last-Event-ID`` replay, pings and chunked
transfer, and ``POST /v1/notify``. The subprocess tests run
``main.py --channel`` exactly as Claude Code would and speak JSON-RPC over
its pipes.
"""

from __future__ import annotations

import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from services.automation import channel_bridge
from services.automation.channel_bridge import AppLink, EventStream, LinkError

REPO = Path(__file__).resolve().parents[1]
MAIN = REPO / "main.py"
TOKEN = "t0ken-for-the-channel-tests-0123456789abcdef"
WAIT_S = 20.0


# -- the fake app ---------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:
        pass

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _headers(self) -> dict:
        return {key.lower(): value for key, value in self.headers.items()}

    def do_GET(self) -> None:
        app: FakeApp = self.server.app
        path, _, query = self.path.partition("?")
        with app.cond:
            app.stream_requests.append({"path": path, "query": query, "headers": self._headers()})
            app.cond.notify_all()
        if path != "/v1/events":
            self._json(404, {"error": {"code": "not_found", "message": "No such route."}})
            return
        if self.headers.get("Authorization") != f"Bearer {app.token}":
            self._json(401, {"error": {"code": "unauthorized", "message": "A valid bearer token is required."}})
            return
        if app.events_status != 200:
            self._json(app.events_status, {"error": {"code": "forbidden", "message": "Allow live events."}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        frames: queue.Queue = queue.Queue()
        frames.put("retry: 2000\r\n\r\n")  # the live API's first frame
        last = self.headers.get("Last-Event-ID")
        with app.cond:
            if last is not None:
                for seq, frame in app.buffer:
                    if seq > int(last):
                        frames.put(frame)
            app.streams.append(frames)
            app.cond.notify_all()
        try:
            while True:
                try:
                    frame = frames.get(timeout=0.2)
                except queue.Empty:
                    frame = ": ping\r\n\r\n"
                if frame is None:
                    break
                data = frame.encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with app.cond:
                app.streams.remove(frames)
                app.cond.notify_all()
        self.close_connection = True

    def do_POST(self) -> None:
        app: FakeApp = self.server.app
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with app.cond:
            app.notifications.append(
                {"path": self.path, "headers": self._headers(), "body": json.loads(body or b"{}")}
            )
            app.cond.notify_all()
        if self.path != "/v1/notify":
            self._json(404, {"error": {"code": "not_found", "message": "No such route."}})
        elif self.headers.get("Authorization") != f"Bearer {app.token}":
            self._json(401, {"error": {"code": "unauthorized", "message": "A valid bearer token is required."}})
        elif app.notify_status == 202:
            self._json(202, {"shown": True})
        else:
            self._json(app.notify_status, {"error": {"code": "refused", "message": app.notify_message}})


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        # A client that hangs up mid-answer is part of these tests.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class FakeApp:
    """OpenWhisper's live API in miniature, on a real listening socket."""

    def __init__(self, token: str = TOKEN) -> None:
        self.token = token
        self.port = 0
        self.events_status = 200
        self.notify_status = 202
        self.notify_message = "Refused."
        self.stream_requests: list[dict] = []
        self.notifications: list[dict] = []
        self.buffer: list[tuple[int, str]] = []
        self.streams: list[queue.Queue] = []
        self.cond = threading.Condition()
        self._seq = 0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FakeApp":
        server = _Server(("127.0.0.1", self.port), _Handler)
        server.app = self
        self.port = server.server_address[1]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """Close every stream and the listener, as the app does when it quits."""
        with self.cond:
            for frames in self.streams:
                frames.put(None)
            self.cond.wait_for(lambda: not self.streams, timeout=5)
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(5)

    def publish(self, data: dict, event_type: str = "channel.message") -> int:
        with self.cond:
            self._seq += 1
            event = {
                "id": f"evt_{self._seq:024x}",
                "seq": self._seq,
                "type": event_type,
                "time": "2026-10-10T12:00:00.000Z",
                "v": 1,
                "data": data,
            }
            frame = f"id: {self._seq}\r\nevent: {event_type}\r\ndata: {json.dumps(event, ensure_ascii=False)}\r\n\r\n"
            self.buffer.append((self._seq, frame))
            for frames in self.streams:
                frames.put(frame)
            return self._seq

    def send_gap(self, missed_from_seq: int, reason: str = "evicted") -> None:
        """``events.gap`` as the live API sends it: no ``id:`` line, seq 0."""
        event = {"id": "", "seq": 0, "type": "events.gap", "time": "2026-10-10T12:00:00.000Z", "v": 1,
                 "data": {"missed_from_seq": missed_from_seq, "reason": reason}}
        with self.cond:
            for frames in self.streams:
                frames.put(f"event: events.gap\r\ndata: {json.dumps(event)}\r\n\r\n")

    def wait_for(self, predicate, timeout: float = WAIT_S) -> None:
        with self.cond:
            assert self.cond.wait_for(predicate, timeout), "the fake app never saw the expected request"


@pytest.fixture
def app():
    fake = FakeApp().start()
    yield fake
    fake.stop()


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _message(text: str = "rename the helper to load_config", **extra) -> dict:
    return {
        "job_id": "0123456789abcdef",
        "source": "hotkey",
        "action_id": "claude_channel",
        "text": text,
        "app": {"id": "code", "name": "Visual Studio Code", "category": "editor"},
        **extra,
    }


# -- the channel as a subprocess --------------------------------------------------


class ChannelProcess:
    """``main.py --channel`` on pipes, as Claude Code starts it."""

    def __init__(self, env: dict, *, interpreter: str = sys.executable, argv: list[str] | None = None) -> None:
        self.proc = subprocess.Popen(
            argv or [interpreter, str(MAIN), "--channel"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO),
            env=env,
        )
        self.raw_lines: list[bytes] = []
        self._lines: queue.Queue = queue.Queue()
        self._stderr: list[bytes] = []
        self._unclaimed: list[dict] = []
        self._next_id = 100
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        for line in iter(self.proc.stdout.readline, b""):
            self.raw_lines.append(line)
            self._lines.put(json.loads(line))
        self._lines.put(None)

    def _pump_stderr(self) -> None:
        for line in iter(self.proc.stderr.readline, b""):
            self._stderr.append(line)

    @property
    def stderr(self) -> str:
        return b"".join(self._stderr).decode("utf-8", "replace")

    def send(self, message: dict) -> None:
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def _take(self, predicate, timeout: float) -> dict:
        for index, message in enumerate(self._unclaimed):
            if predicate(message):
                return self._unclaimed.pop(index)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"no matching message; stderr:\n{self.stderr}"
            try:
                message = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            assert message is not None, f"the channel closed stdout; stderr:\n{self.stderr}"
            if predicate(message):
                return message
            self._unclaimed.append(message)

    def request(self, method: str, params: dict | None = None, *, request_id=None, timeout: float = WAIT_S) -> dict:
        if request_id is None:
            self._next_id += 1
            request_id = self._next_id
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        return self._take(lambda m: m.get("id") == request_id and "method" not in m, timeout)

    def notification(self, method: str, timeout: float = WAIT_S) -> dict:
        return self._take(lambda m: m.get("method") == method and "id" not in m, timeout)

    def handshake(self) -> dict:
        response = self.request(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": "2.1.296"},
            },
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return response

    def close_stdin(self, timeout: float = WAIT_S) -> int:
        self.proc.stdin.close()
        return self.proc.wait(timeout)

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)


def _env(**values: str) -> dict:
    env = dict(os.environ)
    for name in ("OPENWHISPER_URL", "OPENWHISPER_TOKEN", "OPENWHISPER_LOG_LEVEL"):
        env.pop(name, None)
    env.update(values)
    return env


@pytest.fixture
def spawn():
    started: list[ChannelProcess] = []

    def start(env: dict, **kwargs) -> ChannelProcess:
        process = ChannelProcess(env, **kwargs)
        started.append(process)
        return process

    yield start
    for process in started:
        process.kill()


def test_channel_end_to_end(app, spawn):
    channel = spawn(_env(OPENWHISPER_URL=app.url, OPENWHISPER_TOKEN=TOKEN))

    # Claude Code's v2 runtime probes for revision 2026-07-28 first; a
    # "method not found" keeps it on the handshake that carries channels.
    probe = channel.request("server/discover", {}, request_id="probe-1")
    assert probe["error"]["code"] == -32601

    initialized = channel.handshake()["result"]
    assert initialized["protocolVersion"] == "2025-11-25"
    assert initialized["capabilities"]["experimental"] == {"claude/channel": {}}
    assert initialized["capabilities"]["tools"] == {}
    assert initialized["serverInfo"]["name"] == "openwhisper"
    assert "reply tool" in initialized["instructions"]

    tools = channel.request("tools/list")["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["reply"]
    assert tools[0]["inputSchema"]["required"] == ["text"]

    app.wait_for(lambda: app.streams)
    first = app.stream_requests[0]
    assert first["path"] == "/v1/events"
    assert first["query"] == "types=channel.message"
    assert first["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert first["headers"]["x-openwhisper-client"] == "claude-channel"
    assert first["headers"]["accept"] == "text/event-stream"
    assert "last-event-id" not in first["headers"]

    app.publish({"job_id": "aa11", "text": "ignored: another type"}, event_type="recording.started")
    app.publish(_message("rename the helper to load_config, café ✓"))
    pushed = channel.notification("notifications/claude/channel")
    assert pushed["params"] == {
        "content": "rename the helper to load_config, café ✓",
        "meta": {
            "origin": "dictated", "job_id": "0123456789abcdef", "action": "claude_channel",
            "app": "Visual Studio Code",
        },
    }

    reply = channel.request(
        "tools/call", {"name": "reply", "arguments": {"text": "Renamed in 3 files.", "title": "Claude"}}
    )
    assert reply["result"]["isError"] is False
    app.wait_for(lambda: app.notifications)
    posted = app.notifications[0]
    assert posted["path"] == "/v1/notify"
    assert posted["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert posted["headers"]["x-openwhisper-client"] == "claude-channel"
    assert posted["body"] == {"text": "Renamed in 3 files.", "title": "Claude"}

    # The app restarts its listener; a message sent meanwhile is replayed.
    app.stop()
    missed = app.publish(_message("run the tests again"))
    app.start()
    app.wait_for(lambda: len(app.stream_requests) >= 2 and app.streams)
    assert app.stream_requests[-1]["headers"]["last-event-id"] == str(missed - 1)
    replayed = channel.notification("notifications/claude/channel")
    assert replayed["params"]["content"] == "run the tests again"

    assert channel.close_stdin() == 0
    assert all(not line.endswith(b"\r\n") for line in channel.raw_lines)
    log = channel.stderr
    assert "Connected to OpenWhisper" in log
    assert TOKEN not in log
    assert "rename the helper" not in log and "Renamed in 3 files" not in log


def test_reply_needs_the_run_actions_permission(app, spawn):
    app.notify_status = 403
    channel = spawn(_env(OPENWHISPER_URL=app.url, OPENWHISPER_TOKEN=TOKEN))
    channel.handshake()
    result = channel.request("tools/call", {"name": "reply", "arguments": {"text": "Done."}})["result"]
    assert result["isError"] is True
    assert "allow running actions in OpenWhisper Settings > API & MCP" in result["content"][0]["text"]
    assert channel.close_stdin() == 0


def test_reply_says_when_openwhisper_is_not_running(spawn):
    channel = spawn(_env(OPENWHISPER_URL=f"http://127.0.0.1:{_closed_port()}", OPENWHISPER_TOKEN=TOKEN))
    channel.handshake()
    result = channel.request("tools/call", {"name": "reply", "arguments": {"text": "Done."}})["result"]
    assert result["isError"] is True
    assert "OpenWhisper isn't running" in result["content"][0]["text"]
    assert channel.close_stdin() == 0
    # Retries while the app is away log one line, not one per attempt.
    assert channel.stderr.count("OpenWhisper isn't running") == 1


def test_channel_reads_the_saved_port_and_never_loads_qt(app, spawn, tmp_path):
    (tmp_path / "openwhisper_settings.json").write_text(
        json.dumps({"mcp_enabled": True, "mcp_port": app.port}), encoding="utf-8"
    )
    probe = (
        "import json, runpy, sys\n"
        f"sys.argv = [{str(MAIN)!r}, '--channel']\n"
        "try:\n"
        f"    runpy.run_path({str(MAIN)!r}, run_name='__main__')\n"
        "except SystemExit:\n"
        "    pass\n"
        "heavy = ('PyQt6', 'torch', 'ctranslate2', 'onnxruntime', 'mcp', 'fastapi', 'uvicorn', 'httpx', 'pydantic')\n"
        "sys.stderr.write('LOADED ' + json.dumps([m for m in heavy if m in sys.modules]) + '\\n')\n"
    )
    channel = spawn(
        _env(OPENWHISPER_TOKEN=TOKEN, OPENWHISPER_DATA_DIR=str(tmp_path)),
        argv=[sys.executable, "-c", probe],
    )
    channel.handshake()
    app.wait_for(lambda: app.streams)
    assert channel.close_stdin() == 0
    loaded = [line for line in channel.stderr.splitlines() if line.startswith("LOADED ")]
    assert loaded == ["LOADED []"], channel.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="the windowed interpreter is a Windows question")
def test_windowed_interpreter_speaks_over_inherited_pipes(app, spawn):
    """The frozen app is a windowed executable; pythonw.exe starts the same way."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pytest.skip("no pythonw.exe next to this interpreter")
    channel = spawn(_env(OPENWHISPER_URL=app.url, OPENWHISPER_TOKEN=TOKEN), interpreter=str(pythonw))
    assert channel.handshake()["result"]["capabilities"]["experimental"] == {"claude/channel": {}}
    app.wait_for(lambda: app.streams)
    app.publish(_message("from a windowed process"))
    assert channel.notification("notifications/claude/channel")["params"]["content"] == "from a windowed process"
    assert channel.close_stdin() == 0
    assert all(line.endswith(b"\n") and not line.endswith(b"\r\n") for line in channel.raw_lines)
    assert "Connected to OpenWhisper" in channel.stderr


def test_an_empty_stdin_ends_the_channel_at_once():
    done = subprocess.run(
        [sys.executable, str(MAIN), "--channel"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        cwd=str(REPO),
        env=_env(OPENWHISPER_URL=f"http://127.0.0.1:{_closed_port()}", OPENWHISPER_TOKEN=TOKEN),
        timeout=60,
    )
    assert done.returncode == 0
    assert done.stdout == b""


@pytest.mark.skipif(sys.platform != "win32", reason="the windowed interpreter is a Windows question")
def test_a_windowed_start_without_pipes_exits_quietly():
    """A double-clicked shortcut gives the windowed app no stdio; no error dialog, just exit 2."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        pytest.skip("no pythonw.exe next to this interpreter")
    process = subprocess.Popen(
        [str(pythonw), str(MAIN), "--channel"],
        cwd=str(REPO),
        env=_env(OPENWHISPER_URL=f"http://127.0.0.1:{_closed_port()}", OPENWHISPER_TOKEN=TOKEN),
        creationflags=subprocess.DETACHED_PROCESS,
    )
    assert process.wait(60) == 2


# -- the event stream in-process ----------------------------------------------------


class _Collector:
    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.cond = threading.Condition()

    def __call__(self, params: dict) -> None:
        with self.cond:
            self.messages.append(params)
            self.cond.notify_all()

    def wait(self, count: int, timeout: float = WAIT_S) -> list[dict]:
        with self.cond:
            assert self.cond.wait_for(lambda: len(self.messages) >= count, timeout)
            return list(self.messages)


def _eventually(predicate, timeout: float = WAIT_S) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition never held"
        time.sleep(0.01)


def _stream(link: AppLink, deliver, **kwargs) -> EventStream:
    kwargs.setdefault("backoff_first_s", 0.02)
    kwargs.setdefault("backoff_max_s", 0.08)
    kwargs.setdefault("update_check", lambda: False)
    return EventStream(link, deliver, **kwargs)


def test_stream_rereads_a_rejected_token(app, caplog):
    tokens = iter(["stale-token-0123456789abcdef0123456789", TOKEN])
    reads: list[str] = []

    def read_token():
        reads.append("read")
        return next(tokens)

    collected = _Collector()
    stream = _stream(AppLink(url=app.url, read_token=read_token), collected)
    caplog.set_level("INFO", logger="openwhisper.channel")
    stream.start()
    try:
        app.wait_for(lambda: app.streams)
        app.publish(_message("after the token changed"))
        assert collected.wait(1)[0]["content"] == "after the token changed"
    finally:
        stream.stop()
    assert len(reads) == 2
    assert "rejected the access token" in caplog.text
    assert TOKEN not in caplog.text and "after the token changed" not in caplog.text


def test_stream_waits_quietly_while_live_events_are_refused(app, caplog):
    app.events_status = 403
    stream = _stream(AppLink(url=app.url, token=TOKEN), _Collector())
    caplog.set_level("INFO", logger="openwhisper.channel")
    stream.start()
    try:
        app.wait_for(lambda: len(app.stream_requests) >= 3)
    finally:
        stream.stop()
    assert stream.state == "forbidden"
    assert caplog.text.count("refused the event stream") == 1


def test_stream_steps_aside_for_an_update_once_the_app_is_gone():
    stepped_aside = threading.Event()
    stream = _stream(
        AppLink(url=f"http://127.0.0.1:{_closed_port()}", token=TOKEN),
        _Collector(),
        update_check=lambda: True,
        on_update=stepped_aside.set,
    )
    stream.start()
    assert stepped_aside.wait(WAIT_S)
    stream._thread.join(WAIT_S)
    assert not stream._thread.is_alive()


def test_stream_ignores_an_update_while_the_app_answers(app):
    stepped_aside = threading.Event()
    collected = _Collector()
    stream = _stream(AppLink(url=app.url, token=TOKEN), collected, update_check=lambda: True, on_update=stepped_aside.set)
    stream.start()
    try:
        app.wait_for(lambda: app.streams)
        app.publish(_message("still here"))
        collected.wait(1)
    finally:
        stream.stop()
    assert not stepped_aside.is_set()


def test_stop_ends_a_quiet_stream_promptly(app):
    stream = _stream(AppLink(url=app.url, token=TOKEN), _Collector())
    stream.start()
    app.wait_for(lambda: app.streams)
    started = time.monotonic()
    stream.stop()
    assert time.monotonic() - started < 2.0
    assert not stream._thread.is_alive()


def test_a_gap_is_announced_and_keeps_the_resume_point(app):
    collected = _Collector()
    stream = _stream(AppLink(url=app.url, token=TOKEN), collected)
    stream.start()
    try:
        app.wait_for(lambda: app.streams)
        first = app.publish(_message("before the gap"))
        collected.wait(1)
        app.send_gap(missed_from_seq=first + 1)
        _eventually(lambda: len(collected.messages) == 2)
        assert stream.last_id == str(first)
        second = app.publish(_message("after the gap"))
        messages = collected.wait(3)
        _eventually(lambda: stream.last_id == str(second))
    finally:
        stream.stop()
    assert [m["content"] for m in messages] == [
        "before the gap", channel_bridge.GAP_NOTICE, "after the gap",
    ]
    assert messages[1]["meta"] == {"origin": "openwhisper"}


def test_stream_skips_events_without_text(app):
    collected = _Collector()
    stream = _stream(AppLink(url=app.url, token=TOKEN), collected)
    stream.start()
    try:
        app.wait_for(lambda: app.streams)
        app.publish(_message("   "))
        app.publish({"job_id": "x"})
        app.publish(_message("the real one"))
        messages = collected.wait(1)
    finally:
        stream.stop()
    assert [m["content"] for m in messages] == ["the real one"]


# -- replies in-process -------------------------------------------------------------


def test_notify_rereads_a_rejected_token_once(app):
    tokens = iter(["stale-token-0123456789abcdef0123456789", TOKEN])
    link = AppLink(url=app.url, read_token=lambda: next(tokens))
    link.token()  # the stale one, as if read at startup
    link.notify("Done.")
    assert [n["headers"]["authorization"] for n in app.notifications] == [
        "Bearer stale-token-0123456789abcdef0123456789",
        f"Bearer {TOKEN}",
    ]


@pytest.mark.parametrize(
    ("status", "message", "expected"),
    [
        (403, "", "allow running actions in OpenWhisper Settings > API & MCP"),
        (404, "", "can't show replies. Update OpenWhisper"),
        (503, "OpenWhisper isn't ready for this yet.", "(OpenWhisper isn't ready for this yet.)"),
    ],
)
def test_notify_refusals_tell_claude_what_to_do(app, status, message, expected):
    app.notify_status = status
    app.notify_message = message
    with pytest.raises(LinkError) as caught:
        AppLink(url=app.url, token=TOKEN).notify("Done.")
    assert expected in caught.value.message


def test_notify_sends_no_title_when_none_is_given(app):
    AppLink(url=app.url, token=TOKEN).notify("Done.")
    assert app.notifications[0]["body"] == {"text": "Done."}


def test_the_bridge_module_stays_light():
    """Imported at ``--channel`` before anything else; no app, Qt or SDK modules."""
    probe = (
        "import sys\n"
        "import services.automation.channel_bridge\n"
        "heavy = ('PyQt6', 'numpy', 'mcp', 'httpx', 'fastapi', 'services.settings', 'keyring')\n"
        "print([m for m in heavy if m in sys.modules])\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe], cwd=str(REPO), capture_output=True, text=True, timeout=60, check=True
    )
    assert out.stdout.strip() == "[]"
    assert channel_bridge.CHANNEL_MESSAGE == "channel.message"
