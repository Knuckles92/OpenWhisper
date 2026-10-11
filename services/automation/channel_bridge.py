"""``openwhisper --channel``: a Claude Code channel fed by OpenWhisper.

Claude Code starts this as a stdio MCP server (a research preview:
https://code.claude.com/docs/en/channels-reference). It declares the
``claude/channel`` capability, follows the running app's live event stream
(``GET /v1/events?types=channel.message`` on the in-app listener) and turns
each dictated message into a ``notifications/claude/channel`` notification.
Its ``reply`` tool shows Claude's answer near the pointer (``POST /v1/notify``).

The MCP side is written by hand rather than with the mcp SDK. The SDK costs
half a second of imports before the first answer, and it speaks revision
2026-07-28: it would answer Claude Code's ``server/discover`` probe, and Claude
Code does not register a channel that connects on that revision because it
has no path for unsolicited notifications. Answering the probe with "method
not found" keeps Claude Code on the ``initialize`` handshake (2025-11-25 and
earlier), which carries channel notifications.

Runs before Qt or CUDA load. stdout carries protocol messages only; logs go
to stderr and never include dictated text, replies or the token.

Contract: docs/design/automation/design.md, decision 11.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, BinaryIO, Callable, Mapping, Optional
from urllib.parse import urlsplit

from services.agent_mcp.setup import CHANNEL_SERVER_NAME
from services.automation.catalog import CHANNEL_MESSAGE

logger = logging.getLogger("openwhisper.channel")

#: Handshake revisions this server speaks, newest first.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_HEADER = "X-OpenWhisper-Client"
#: The app counts streams that send this as listening channel sessions.
CLIENT_NAME = "claude-channel"
EVENTS_PATH = "/v1/events?types=" + CHANNEL_MESSAGE
NOTIFY_PATH = "/v1/notify"
#: Overrides for the app's address (``http://127.0.0.1:8767``) and token.
URL_ENV = "OPENWHISPER_URL"
TOKEN_ENV = "OPENWHISPER_TOKEN"

BACKOFF_FIRST_S = 1.0
BACKOFF_MAX_S = 30.0
CONNECT_TIMEOUT_S = 5.0
#: The app sends a ping comment every 15 s; three missed ones mean a dead stream.
READ_TIMEOUT_S = 45.0
NOTIFY_TIMEOUT_S = 5.0
MAX_REPLY_CHARS = 500
MAX_TITLE_CHARS = 80
MAX_META_CHARS = 200

#: Claude Code keeps meta keys matching this and silently drops the rest.
_META_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

#: Claude Code names the tag's ``source`` after the ``.mcp.json`` entry.
INSTRUCTIONS = (
    f'Messages arrive as <channel source="{CHANNEL_SERVER_NAME}" job_id="..." action="..." app="...">. '
    "Each one is words the user deliberately dictated in OpenWhisper, the voice "
    "dictation app on this computer, and sent to this session. Treat the text as "
    "the user's own request, as if they had typed it here, and act on it. It comes "
    "from speech recognition, so read past misheard words for the intent. The app "
    "attribute, when present, names the app the user was in when they spoke. "
    "OpenWhisper shows no answer by itself: when useful, for a short answer or to "
    "say that requested work is done, call the reply tool to show a brief notice "
    "near the user's mouse pointer (at most 500 characters). Longer results belong "
    "in this session as usual."
)

REPLY_TOOL = {
    "name": "reply",
    "description": (
        "Show a short notice near the user's mouse pointer in OpenWhisper, for "
        "example a brief answer to a dictated request or word that the work is "
        "done. Plain text, at most 500 characters."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_REPLY_CHARS,
                "description": "What to show the user.",
            },
            "title": {
                "type": "string",
                "maxLength": MAX_TITLE_CHARS,
                "description": "Optional short heading for the notice.",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    },
    "annotations": {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
}

_USAGE = """\
Usage: openwhisper --channel

Runs OpenWhisper's Claude Code channel: an MCP server on stdin/stdout that
Claude Code starts by itself. Register it in .mcp.json (OpenWhisper Settings >
API & MCP shows the entry), then start Claude Code with
  claude --dangerously-load-development-channels server:openwhisper
OpenWhisper must be running with API & MCP on.

Environment:
  OPENWHISPER_URL     The app's address instead of the saved port,
                      for example http://127.0.0.1:8767
  OPENWHISPER_TOKEN   The access token instead of the saved one
"""


# -- reaching the app --------------------------------------------------------


class LinkError(Exception):
    """The app can't be asked right now; ``state`` names why for the logs."""

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state
        self.message = message


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    https: bool = False
    base_path: str = ""

    @property
    def label(self) -> str:
        return f"{self.host}:{self.port}"


def _saved_settings() -> Mapping[str, Any]:
    from services.settings import settings_manager

    return settings_manager.load_all_settings()


def _saved_token() -> Optional[str]:
    from services.agent_mcp.runtime import CREDENTIAL_NAME
    from services.credentials import CredentialStore

    # A fresh store on every read: the shared one remembers a missing key,
    # and the app creates the token on first use while this keeps running.
    return CredentialStore().get(CREDENTIAL_NAME)


def configured_port(settings: Mapping[str, Any]) -> int:
    """The in-app listener's port, validated the way ``McpRuntime.restore`` does."""
    from services.agent_mcp.runtime import DEFAULT_PORT
    from services.settings import SettingsKey

    port = settings.get(SettingsKey.MCP_PORT, DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        return DEFAULT_PORT
    return port


def parse_url(url: str) -> Target:
    """``OPENWHISPER_URL`` as a target; ``LinkError`` when it isn't usable."""
    parts = urlsplit(url.strip())
    try:
        port = parts.port
        valid = parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise LinkError("bad_url", f"{URL_ENV} must be an http:// address such as http://127.0.0.1:8767.")
    https = parts.scheme == "https"
    return Target(
        host=parts.hostname,
        port=port or (443 if https else 80),
        https=https,
        base_path=parts.path.rstrip("/"),
    )


class AppLink:
    """Where the running app listens, and the token it expects.

    ``OPENWHISPER_URL`` and ``OPENWHISPER_TOKEN`` override the app's own
    settings and credential store. Otherwise the port comes from the settings
    file on every connection, since the user may change it, and the token from
    the OS credential store: read once, and again after the app rejects it.
    """

    def __init__(
        self,
        *,
        url: str = "",
        token: str = "",
        read_settings: Callable[[], Mapping[str, Any]] = _saved_settings,
        read_token: Callable[[], Optional[str]] = _saved_token,
        version: str = "",
    ) -> None:
        self._url = url.strip()
        self._fixed_token = token.strip()
        self._read_settings = read_settings
        self._read_token = read_token
        self._user_agent = f"OpenWhisper-channel/{version or '0'}"
        self._lock = threading.Lock()
        self._token: Optional[str] = None

    @classmethod
    def from_environment(cls, *, version: str = "") -> "AppLink":
        return cls(
            url=os.environ.get(URL_ENV, ""),
            token=os.environ.get(TOKEN_ENV, ""),
            version=version,
        )

    def target(self) -> Target:
        if self._url:
            return parse_url(self._url)
        try:
            settings = self._read_settings()
        except Exception as exc:
            logger.debug("Reading settings failed: %s", type(exc).__name__)
            settings = {}
        return Target("127.0.0.1", configured_port(settings))

    def unreachable_message(self) -> str:
        if not self._url:
            from services.settings import SettingsKey

            try:
                enabled = self._read_settings().get(SettingsKey.MCP_ENABLED, False)
            except Exception:
                enabled = None
            if enabled is False:
                return "API & MCP is off in OpenWhisper Settings."
        return "OpenWhisper isn't running, or API & MCP is off in its Settings."

    def connect(self, read_timeout: float) -> tuple[http.client.HTTPConnection, Target]:
        """An open connection to the app; ``LinkError`` when nothing answers."""
        target = self.target()
        factory = http.client.HTTPSConnection if target.https else http.client.HTTPConnection
        connection = factory(target.host, target.port, timeout=CONNECT_TIMEOUT_S)
        try:
            connection.connect()
        except OSError:
            connection.close()
            raise LinkError("unreachable", self.unreachable_message()) from None
        connection.sock.settimeout(read_timeout)
        return connection, target

    def token(self) -> str:
        if self._fixed_token:
            return self._fixed_token
        with self._lock:
            if not self._token:
                try:
                    self._token = self._read_token() or None
                except Exception as exc:
                    logger.debug("Reading the token failed: %s", type(exc).__name__)
                    self._token = None
            if not self._token:
                raise LinkError(
                    "no_token",
                    "Couldn't read OpenWhisper's access token from the system "
                    "credential store. Turn on API & MCP in OpenWhisper Settings.",
                )
            return self._token

    def forget_token(self) -> None:
        """The app refused the token: read it again before the next request."""
        with self._lock:
            self._token = None

    def headers(self, token: str, **extra: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            CLIENT_HEADER: CLIENT_NAME,
            "User-Agent": self._user_agent,
            **extra,
        }

    def notify(self, text: str, title: str = "") -> None:
        """Show ``text`` near the pointer. Raises ``LinkError`` with copy for Claude."""
        body = {"text": text}
        if title:
            body["title"] = title
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        status, reply = self._post(NOTIFY_PATH, payload)
        if status == 401 and not self._fixed_token:
            self.forget_token()  # replaced since it was read; once more with the saved one
            status, reply = self._post(NOTIFY_PATH, payload)
        if not 200 <= status < 300:
            raise LinkError(f"http_{status}", _reply_refusal(status, reply))

    def _post(self, path: str, payload: bytes) -> tuple[int, bytes]:
        try:
            connection, target = self.connect(NOTIFY_TIMEOUT_S)
        except LinkError as exc:
            if exc.state == "unreachable":
                raise LinkError(exc.state, f"{exc.message} The reply wasn't shown; tell the user here instead.") from None
            raise
        try:
            connection.request(
                "POST",
                target.base_path + path,
                body=payload,
                headers=self.headers(self.token(), **{"Content-Type": "application/json"}),
            )
            response = connection.getresponse()
            return response.status, response.read()
        except socket.timeout:
            raise LinkError(
                "timeout", "OpenWhisper didn't answer in time, so the reply may not have been shown."
            ) from None
        except (OSError, http.client.HTTPException):
            raise LinkError(
                "unreachable", "The connection to OpenWhisper dropped, so the reply wasn't shown."
            ) from None
        finally:
            connection.close()


def _error_message(body: bytes) -> str:
    try:
        error = json.loads(body.decode("utf-8")).get("error")
        message = error.get("message") if isinstance(error, Mapping) else ""
    except (ValueError, AttributeError):
        return ""
    return message if isinstance(message, str) else ""


def _reply_refusal(status: int, body: bytes) -> str:
    if status == 401:
        return "OpenWhisper rejected the channel's access token, so the reply wasn't shown."
    if status == 403:
        return (
            "OpenWhisper didn't show the reply: allow running actions in "
            "OpenWhisper Settings > API & MCP."
        )
    if status == 404:
        return "This version of OpenWhisper can't show replies. Update OpenWhisper."
    detail = _error_message(body) or f"HTTP {status}"
    return f"OpenWhisper couldn't show the reply ({detail})."


def _stream_refusal(status: int, body: bytes) -> tuple[str, str]:
    if status == 200:
        return "unsupported", "OpenWhisper didn't answer with an event stream. Update OpenWhisper."
    if status == 401:
        return "unauthorized", "OpenWhisper rejected the access token; retrying."
    if status == 403:
        detail = _error_message(body) or "Allow live events in OpenWhisper Settings > API & MCP."
        return "forbidden", f"OpenWhisper refused the event stream. {detail}"
    if status == 404:
        return "unsupported", "This version of OpenWhisper has no live event stream. Update OpenWhisper."
    return f"http_{status}", f"OpenWhisper answered the event stream with HTTP {status}; retrying."


# -- the event stream ---------------------------------------------------------


@dataclass(frozen=True)
class SseEvent:
    type: str
    data: str
    last_id: str


class SseParser:
    """Server-sent events, one line at a time.

    Follows https://html.spec.whatwg.org/multipage/server-sent-events.html:
    ``last_id`` changes when an event block ends, so a stream that breaks
    halfway through an event resumes before it rather than after it.
    """

    def __init__(self, last_id: str = "") -> None:
        self.last_id = last_id
        self._reset()

    def _reset(self) -> None:
        self._type = ""
        self._data: list[str] = []
        self._id: Optional[str] = None

    def feed(self, line: str) -> Optional[SseEvent]:
        """One line without its line ending; returns the event a blank line ends."""
        if not line:
            if self._id is not None:
                self.last_id = self._id
            event = None
            if self._data:
                event = SseEvent(self._type or "message", "\n".join(self._data), self.last_id)
            self._reset()
            return event
        if line.startswith(":"):
            return None
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._type = value
        elif field == "data":
            self._data.append(value)
        elif field == "id" and "\0" not in value:
            self._id = value
        return None


def clean_meta(meta: Mapping[str, Any]) -> dict[str, str]:
    """The entries Claude Code keeps: identifier keys, non-empty one-line string values."""
    cleaned: dict[str, str] = {}
    for key, value in meta.items():
        if not isinstance(key, str) or not _META_KEY.match(key) or not isinstance(value, str):
            continue
        value = " ".join(value.split())[:MAX_META_CHARS]
        if value:
            cleaned[key] = value
    return cleaned


def channel_params(event: Any) -> Optional[dict]:
    """``notifications/claude/channel`` params for one ``channel.message`` event.

    None when the event carries no text. Claude Code sets the tag's
    ``source`` from the server name itself, so meta does not repeat it.
    """
    data = event.get("data") if isinstance(event, Mapping) else None
    if not isinstance(data, Mapping):
        return None
    text = data.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    app = data.get("app")
    if isinstance(app, Mapping):
        app = app.get("name")
    meta = clean_meta({"job_id": data.get("job_id"), "action": data.get("action_id"), "app": app})
    params: dict[str, Any] = {"content": text}
    if meta:
        params["meta"] = meta
    return params


def update_in_progress() -> bool:
    """Whether a Windows update is about to swap the folder this executable runs from.

    The frozen channel runs from the install folder, and Windows can't rename
    a folder while a program in it runs, so the channel steps aside.
    """
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    from services.app_update_apply import mutex_exists
    from services.update_contract import SETUP_MUTEX_NAME, UPDATE_MUTEX_NAMES

    return any(mutex_exists(name) for name in (*UPDATE_MUTEX_NAMES, SETUP_MUTEX_NAME))


class EventStream:
    """Follows the app's channel messages on a thread and hands each to ``deliver``.

    Reconnects with backoff (1 s doubling to 30 s, back to 1 s after a
    stream opens) and resumes with ``Last-Event-ID``. Logs one line per
    change of state, never per retry. While the app is unreachable and an
    update is replacing it, ``on_update`` is called and the thread ends.
    """

    def __init__(
        self,
        link: AppLink,
        deliver: Callable[[dict], None],
        *,
        on_update: Optional[Callable[[], None]] = None,
        update_check: Callable[[], bool] = update_in_progress,
        backoff_first_s: float = BACKOFF_FIRST_S,
        backoff_max_s: float = BACKOFF_MAX_S,
        read_timeout_s: float = READ_TIMEOUT_S,
    ) -> None:
        self._link = link
        self._deliver = deliver
        self._on_update = on_update
        self._update_check = update_check
        self._backoff_first = backoff_first_s
        self._backoff_max = backoff_max_s
        self._read_timeout = read_timeout_s
        self._stop = threading.Event()
        self._lock = threading.Lock()
        # Kept apart from the connection: http.client drops ``sock`` from a
        # connection whose response will close it, while the response still
        # reads from it.
        self._socket: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._state = ""
        self.last_id = ""

    @property
    def state(self) -> str:
        return self._state

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="openwhisper-channel-events", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._lock:
            sock = self._socket
        if sock is not None:
            try:
                # Unblocks the reader's recv; close() alone would not while
                # the response still holds the socket.
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _report(self, state: str, message: str, level: int = logging.INFO) -> None:
        if state != self._state:
            self._state = state
            logger.log(level, "%s", message)

    def _run(self) -> None:
        delay = self._backoff_first
        while not self._stop.is_set():
            opened, state = self._follow_once()
            if self._stop.is_set():
                return
            if state == "unreachable" and self._on_update is not None and self._update_check():
                logger.info(
                    "OpenWhisper is updating; closing the channel so the update can "
                    "replace its files. Reconnect the openwhisper server with /mcp afterwards."
                )
                self._on_update()
                return
            if opened:
                delay = self._backoff_first
            self._stop.wait(delay)
            if not opened:
                delay = min(delay * 2, self._backoff_max)

    def _follow_once(self) -> tuple[bool, str]:
        """One connection: (whether a stream opened, the state it ended in)."""
        try:
            connection, target = self._link.connect(self._read_timeout)
        except LinkError as exc:
            self._report(exc.state, exc.message, logging.WARNING if exc.state == "bad_url" else logging.INFO)
            return False, exc.state
        with self._lock:
            self._socket = connection.sock
        opened = False
        try:
            if self._stop.is_set():
                return False, "stopped"
            headers = self._link.headers(
                self._link.token(), Accept="text/event-stream", **{"Cache-Control": "no-cache"}
            )
            if self.last_id and self.last_id.isascii() and self.last_id.isprintable():
                headers["Last-Event-ID"] = self.last_id
            connection.request("GET", target.base_path + EVENTS_PATH, headers=headers)
            response = connection.getresponse()
            content_type = response.getheader("Content-Type", "")
            if response.status != 200 or not content_type.startswith("text/event-stream"):
                state, message = _stream_refusal(response.status, response.read(65536))
                if response.status == 401:
                    self._link.forget_token()
                self._report(state, message)
                return False, state
            opened = True
            self._report("connected", f"Connected to OpenWhisper at {target.label}; waiting for dictated messages.")
            self._read(response)
            if not self._stop.is_set():
                self._report("dropped", "OpenWhisper closed the event stream; reconnecting.")
            return True, "dropped"
        except LinkError as exc:
            self._report(exc.state, exc.message)
            return opened, exc.state
        except socket.timeout:
            if not self._stop.is_set():
                self._report("silent", "OpenWhisper stopped sending events; reconnecting.")
            return opened, "silent"
        except (OSError, http.client.HTTPException, ValueError):
            if self._stop.is_set():
                return opened, "stopped"
            if opened:
                self._report("dropped", "The connection to OpenWhisper dropped; reconnecting.")
                return True, "dropped"
            self._report("unreachable", self._link.unreachable_message())
            return False, "unreachable"
        finally:
            with self._lock:
                self._socket = None
            connection.close()

    def _read(self, response: http.client.HTTPResponse) -> None:
        parser = SseParser(self.last_id)
        while not self._stop.is_set():
            line = response.readline()
            if not line:
                return
            event = parser.feed(line.decode("utf-8", "replace").rstrip("\r\n"))
            if event is not None:
                self._handle(event)
            self.last_id = parser.last_id

    def _handle(self, event: SseEvent) -> None:
        if event.type != CHANNEL_MESSAGE:
            return
        try:
            payload = json.loads(event.data)
        except ValueError:
            logger.warning("Skipped a channel event that wasn't JSON.")
            return
        params = channel_params(payload)
        if params is None:
            logger.info("Skipped a channel event with no text.")
            return
        self._deliver(params)


# -- the MCP server -----------------------------------------------------------


class MessageWriter:
    """One JSON-RPC message per line on the protocol stream; the only writer.

    ASCII-only JSON: every parser reads it whatever the pipe's encoding, and
    a stray surrogate in dictated text can't fail the write.
    """

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.closed = False

    def send(self, message: Mapping[str, Any]) -> bool:
        line = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n"
        with self._lock:
            if self.closed:
                return False
            try:
                self._stream.write(line)
                self._stream.flush()
            except (OSError, ValueError):
                self.closed = True
                return False
        return True

    def close(self) -> None:
        with self._lock:
            self.closed = True
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass


class ChannelServer:
    """The MCP server Claude Code talks to over the protocol streams."""

    def __init__(
        self,
        reader: BinaryIO,
        writer: BinaryIO,
        link: AppLink,
        *,
        version: str = "",
        stream_factory: Optional[Callable[[], EventStream]] = None,
        exit_process: Callable[[int], None] = os._exit,
    ) -> None:
        self._reader = reader
        self._out = MessageWriter(writer)
        self._link = link
        self._version = version or "0"
        self._stream_factory = stream_factory or self._default_stream
        self._exit_process = exit_process
        self._stream: Optional[EventStream] = None
        self._stream_lock = threading.Lock()

    # -- lifecycle

    def serve(self) -> int:
        """Answer requests until stdin closes; returns the exit code."""
        try:
            while True:
                line = self._reader.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    self._handle_line(line)
        except (OSError, ValueError):
            logger.info("The connection to Claude Code closed.")
        finally:
            self.close()
        return 0

    def close(self) -> None:
        with self._stream_lock:
            stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()

    def _default_stream(self) -> EventStream:
        return EventStream(self._link, self.send_channel_message, on_update=self._exit_for_update)

    def _start_stream(self) -> None:
        with self._stream_lock:
            if self._stream is None:
                self._stream = self._stream_factory()
                self._stream.start()

    def _exit_for_update(self) -> None:
        # The main thread is waiting on stdin, which no other thread can
        # interrupt; the process ends here instead. Log records are already
        # flushed one by one.
        self._out.close()
        self._exit_process(0)

    # -- output

    def send_channel_message(self, params: dict) -> None:
        if self._out.send({"jsonrpc": "2.0", "method": "notifications/claude/channel", "params": params}):
            logger.info("Forwarded a dictated message to Claude Code.")

    def _result(self, request_id: Any, result: Mapping[str, Any]) -> None:
        self._out.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: Any, code: int, message: str) -> None:
        self._out.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    # -- input

    def _handle_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            self._error(None, PARSE_ERROR, "Parse error")
            return
        if isinstance(message, list):
            # JSON-RPC batches left MCP in 2025-06-18; no client of this server sends them.
            self._error(None, INVALID_REQUEST, "Batches are not supported")
            return
        if not isinstance(message, dict):
            self._error(None, INVALID_REQUEST, "Invalid Request")
            return
        method = message.get("method")
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                return  # a response; this server sends no requests
            self._error(message.get("id"), INVALID_REQUEST, "Invalid Request")
            return
        params = message.get("params", {})
        if params is None:
            params = {}
        if "id" not in message:
            self._notification(method, params)
            return
        request_id = message["id"]
        if not isinstance(params, dict):
            self._error(request_id, INVALID_PARAMS, "params must be an object")
            return
        if method == "initialize":
            self._result(request_id, self._initialize(params))
        elif method == "ping":
            self._result(request_id, {})
        elif method == "tools/list":
            self._result(request_id, {"tools": [REPLY_TOOL]})
        elif method == "tools/call":
            # A reply waits on the app; keep reading requests meanwhile.
            threading.Thread(
                target=self._call_tool, args=(request_id, params), name="openwhisper-channel-reply", daemon=True
            ).start()
        else:
            # Includes server/discover, Claude Code's probe for revision
            # 2026-07-28: this error sends it to the initialize handshake.
            self._error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    def _notification(self, method: str, params: Any) -> None:
        if method == "notifications/initialized":
            self._start_stream()
        # Everything else (cancelled, roots changes, …) needs nothing here.

    def _initialize(self, params: Mapping[str, Any]) -> dict:
        requested = params.get("protocolVersion")
        version = requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
        client = params.get("clientInfo")
        if isinstance(client, Mapping):
            logger.info(
                "%s %s connected (protocol %s).",
                str(client.get("name", "A client"))[:60], str(client.get("version", ""))[:30], version,
            )
        return {
            "protocolVersion": version,
            "capabilities": {"experimental": {"claude/channel": {}}, "tools": {}},
            "serverInfo": {"name": CHANNEL_SERVER_NAME, "title": "OpenWhisper", "version": self._version},
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, request_id: Any, params: Mapping[str, Any]) -> None:
        name = params.get("name")
        if name != REPLY_TOOL["name"]:
            self._error(request_id, INVALID_PARAMS, f"Unknown tool: {name}")
            return
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        try:
            text, title = _reply_arguments(arguments)
        except ValueError as exc:
            self._result(request_id, _tool_result(str(exc), error=True))
            return
        try:
            self._link.notify(text, title)
        except LinkError as exc:
            logger.info("A reply wasn't shown (%s).", exc.state)
            self._result(request_id, _tool_result(exc.message, error=True))
            return
        except Exception as exc:
            logger.warning("A reply failed: %s", type(exc).__name__)
            self._result(request_id, _tool_result("OpenWhisper couldn't show the reply.", error=True))
            return
        self._result(request_id, _tool_result("Shown to the user near their pointer."))


def _reply_arguments(arguments: Any) -> tuple[str, str]:
    if not isinstance(arguments, Mapping):
        raise ValueError("Pass the reply as {\"text\": \"...\"}.")
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text is required: the words to show the user.")
    text = text.strip()
    if len(text) > MAX_REPLY_CHARS:
        raise ValueError(f"Replies are limited to {MAX_REPLY_CHARS} characters; shorten it.")
    title = arguments.get("title", "")
    if title is None:
        title = ""
    if not isinstance(title, str):
        raise ValueError("title must be text.")
    title = " ".join(title.split())
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError(f"Titles are limited to {MAX_TITLE_CHARS} characters.")
    return text, title


def _tool_result(text: str, *, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": error}


# -- process setup ------------------------------------------------------------


class PipeLineReader:
    """Lines from a Windows pipe, without ever leaving a read waiting on it.

    Windows serializes synchronous I/O on a pipe: while a ReadFile waits for
    data, another thread's GetFileType on the same pipe waits with it. A DLL
    whose C runtime sets up its own stdio calls exactly that while it loads
    (numpy's does, through config), so a blocking read on stdin froze the
    first such import on the event thread until Claude Code sent something.
    Peeking, then reading only the bytes that have arrived, never leaves a
    read pending.
    """

    POLL_MIN_S = 0.005
    POLL_MAX_S = 0.05

    def __init__(self, fd: int) -> None:
        import msvcrt

        self._fd = fd
        self._handle = msvcrt.get_osfhandle(fd)
        self._buffer = bytearray()
        self._eof = False

    def readline(self) -> bytes:
        import _winapi

        delay = self.POLL_MIN_S
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                line = bytes(self._buffer[: end + 1])
                del self._buffer[: end + 1]
                return line
            if self._eof:
                line = bytes(self._buffer)
                self._buffer.clear()
                return line
            try:
                available = _winapi.PeekNamedPipe(self._handle)[0]
            except OSError:
                self._eof = True  # the writer closed its end
                continue
            if available:
                chunk = os.read(self._fd, available)
                self._eof = not chunk
                self._buffer += chunk
                delay = self.POLL_MIN_S
            else:
                time.sleep(delay)
                delay = min(delay * 2, self.POLL_MAX_S)

    def close(self) -> None:
        os.close(self._fd)


def _is_pipe(fd: int) -> bool:
    import _winapi
    import msvcrt

    try:
        return _winapi.GetFileType(msvcrt.get_osfhandle(fd)) == _winapi.FILE_TYPE_PIPE
    except OSError:
        return False


def _binary_mode(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        # An embedded interpreter (the frozen app) may leave the inherited
        # descriptors in text mode, which would turn "\n" into "\r\n".
        msvcrt.setmode(fd, os.O_BINARY)


def protocol_streams() -> Optional[tuple[Any, BinaryIO]]:
    """Line reader on the inherited stdin and binary stdout, or None without them.

    Anything else that writes to stdout (a print, a native library) is sent to
    stderr instead, so it can never corrupt the protocol.
    """
    # The interpreter leaves these None when it found no usable descriptor,
    # as in a windowed executable started without pipes (a double-click).
    if sys.stdin is None or sys.stdout is None:
        return None
    try:
        in_fd = os.dup(0)
        out_fd = os.dup(1)
        for fd in (in_fd, out_fd):
            _binary_mode(fd)
        reader = PipeLineReader(in_fd) if sys.platform == "win32" and _is_pipe(in_fd) else os.fdopen(in_fd, "rb")
        writer = os.fdopen(out_fd, "wb")
    except OSError:
        return None
    try:
        os.dup2(2, 1)
    except OSError:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.close(devnull)
    sys.stdout = sys.stderr
    return reader, writer


def _configure_logging() -> None:
    level = getattr(logging, os.environ.get("OPENWHISPER_LOG_LEVEL", "INFO").upper(), logging.INFO)
    if not isinstance(level, int):
        level = logging.INFO
    handler: logging.Handler
    if sys.stderr is None:
        handler = logging.NullHandler()
    else:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("openwhisper channel: %(message)s"))
    logging.basicConfig(level=level, handlers=[handler], force=True)


def _app_version() -> str:
    try:
        from _version import __version__
    except ImportError:
        return ""
    return __version__


def main(argv: list[str]) -> int:
    """The ``--channel`` entry point; runs until Claude Code closes stdin."""
    if argv:
        help_asked = argv[:1] in (["-h"], ["--help"])
        if sys.stderr is not None:
            sys.stderr.write(_USAGE if help_asked else "Unexpected arguments. Run --channel --help for usage.\n")
        return 0 if help_asked else 2
    # Nothing may escape: the windowed app would show an error dialog.
    try:
        _configure_logging()
        streams = protocol_streams()
        if streams is None:
            logger.error("Claude Code starts the channel with pipes on stdin and stdout; it can't run without them.")
            return 2
        reader, writer = streams
        version = _app_version()
        server = ChannelServer(reader, writer, AppLink.from_environment(version=version), version=version)
        return server.serve()
    except Exception as exc:
        logger.error("The channel stopped: %s", type(exc).__name__)
        return 1
