"""OpenWhisper's meeting tools as a loopback MCP server (streamable HTTP).

An installed agent reaches its tools here and nowhere else. The server binds
127.0.0.1 on a random port and answers ``POST /mcp/<token>`` with a JSON-RPC
reply (no event stream, which every MCP client accepts). The token names one
*endpoint*: a meeting pass, or a router that picks a pass per call. It must
match the ``Authorization: Bearer`` header too. An endpoint that has closed
answers every call with an error, so a finished or cancelled pass has no
authority left.

Stdlib only; one thread per request, and tool calls within an endpoint run
one at a time so a batch lands in the order the agent sent it.
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SERVER_NAME = "openwhisper"
_SUPPORTED_PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_MAX_BODY_BYTES = 4 * 1024 * 1024

#: ``handler(tool_name, arguments, meta) -> (text, is_error)``.
ToolHandler = Callable[[str, Dict[str, Any], Dict[str, Any]], Tuple[str, bool]]


@dataclass
class McpEndpoint:
    """One token's view of the server: which tools it lists and who answers.

    Attributes:
        token: Path and bearer secret.
        tools: MCP tool definitions returned by ``tools/list``.
        handler: Answers ``tools/call``.
        active: False once closed; every later call is refused.
    """

    token: str
    tools: List[Dict[str, Any]]
    handler: ToolHandler
    active: bool = True
    lock: threading.Lock = field(default_factory=threading.Lock)
    listed: threading.Event = field(default_factory=threading.Event)

    @property
    def tool_names(self) -> List[str]:
        return [str(tool.get("name")) for tool in self.tools]


class LoopbackMcpServer:
    """The meeting-tool MCP server for one meeting (or one report).

    Call :meth:`start`, open an endpoint per pass with :meth:`open_endpoint`,
    close it when the pass ends, and :meth:`stop` at shutdown.
    """

    def __init__(self) -> None:
        self._endpoints: Dict[str, McpEndpoint] = {}
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # ---- lifecycle ----

    def start(self) -> None:
        if self._httpd is not None:
            return
        server = self

        class Handler(_McpRequestHandler):
            owner = server

        httpd = _QuietHTTPServer(("127.0.0.1", 0), Handler)
        httpd.daemon_threads = True
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="meeting-mcp-server", daemon=True,
        )
        self._thread.start()
        logger.info("Meeting tool server listening on 127.0.0.1:%d", self.port)

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        with self._lock:
            for endpoint in self._endpoints.values():
                endpoint.active = False
            self._endpoints.clear()
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> int:
        return self._httpd.server_address[1] if self._httpd is not None else 0

    # ---- endpoints ----

    def open_endpoint(self, tools: List[Dict[str, Any]],
                      handler: ToolHandler) -> McpEndpoint:
        """Register a new endpoint with a fresh token."""
        endpoint = McpEndpoint(
            token=secrets.token_urlsafe(24), tools=list(tools), handler=handler,
        )
        with self._lock:
            self._endpoints[endpoint.token] = endpoint
        return endpoint

    def close_endpoint(self, endpoint: Optional[McpEndpoint]) -> None:
        """Revoke an endpoint; a call already running finishes first."""
        if endpoint is None:
            return
        with endpoint.lock:
            endpoint.active = False
        with self._lock:
            self._endpoints.pop(endpoint.token, None)

    def url(self, endpoint: McpEndpoint) -> str:
        return f"http://127.0.0.1:{self.port}/mcp/{endpoint.token}"

    def _endpoint_for(self, token: str) -> Optional[McpEndpoint]:
        with self._lock:
            return self._endpoints.get(token)

    # ---- JSON-RPC ----

    def handle_message(self, endpoint: McpEndpoint,
                       msg: Any) -> Optional[Dict[str, Any]]:
        """Answer one JSON-RPC message; None for a notification."""
        if not isinstance(msg, dict):
            return _error(None, -32600, "invalid request")
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        if "id" not in msg:
            return None
        if method == "initialize":
            requested = str(params.get("protocolVersion") or "")
            version = requested if requested in _SUPPORTED_PROTOCOLS else _SUPPORTED_PROTOCOLS[1]
            return _result(msg_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": "OpenWhisper meeting tools",
                               "version": "1"},
            })
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            endpoint.listed.set()
            return _result(msg_id, {"tools": endpoint.tools})
        if method == "tools/call":
            return _result(msg_id, self._call_tool(endpoint, params))
        return _error(msg_id, -32601, f"method not found: {method}")

    def _call_tool(self, endpoint: McpEndpoint,
                   params: Dict[str, Any]) -> Dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        with endpoint.lock:
            if not endpoint.active:
                return _tool_text("This meeting pass has ended; its tools are closed.", True)
            if name not in endpoint.tool_names:
                return _tool_text(f"Unknown tool {name!r}. Use only: "
                                  + ", ".join(endpoint.tool_names), True)
            try:
                text, is_error = endpoint.handler(name, arguments, meta)
            except Exception as exc:
                logger.warning("Meeting tool %s failed: %s", name, exc)
                text, is_error = json.dumps({"error": str(exc)}), True
        return _tool_text(text, is_error)


def _result(msg_id: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tool_text(text: str, is_error: bool) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": bool(is_error)}


class _QuietHTTPServer(ThreadingHTTPServer):
    """An agent that exits mid-connection is routine, not an error."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys

        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return
        logger.warning("Meeting tool server request failed", exc_info=True)


class _McpRequestHandler(BaseHTTPRequestHandler):
    owner: LoopbackMcpServer
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        pass

    def _send(self, code: int, body: Any = None) -> None:
        data = b"" if body is None else json.dumps(body).encode("utf-8")
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _authorized_endpoint(self) -> Optional[McpEndpoint]:
        parts = self.path.split("?", 1)[0].strip("/").split("/")
        if len(parts) != 2 or parts[0] != "mcp":
            self._send(404, {"error": "not found"})
            return None
        # Browsers send Origin; agents do not. A web page must never reach
        # these tools, even from this computer.
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            self._send(403, {"error": "forbidden"})
            return None
        token = parts[1]
        auth = self.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        endpoint = self.owner._endpoint_for(token)
        if endpoint is None or not hmac.compare_digest(auth.encode(), expected.encode()):
            self._send(401, {"error": "unauthorized"})
            return None
        return endpoint

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        # No server-initiated stream: every reply rides its POST.
        if self._authorized_endpoint() is not None:
            self._send(405, {"error": "no event stream"})

    def do_DELETE(self) -> None:  # noqa: N802
        if self._authorized_endpoint() is not None:
            self._send(200, {})

    def do_POST(self) -> None:  # noqa: N802
        endpoint = self._authorized_endpoint()
        if endpoint is None:
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > _MAX_BODY_BYTES:
            self._send(413 if length > 0 else 400, {"error": "bad body"})
            return
        try:
            msg = json.loads(self.rfile.read(length))
        except ValueError:
            self._send(400, _error(None, -32700, "parse error"))
            return
        if isinstance(msg, list):
            replies = [r for r in (self.owner.handle_message(endpoint, m) for m in msg) if r]
            self._send(200, replies) if replies else self._send(202)
            return
        reply = self.owner.handle_message(endpoint, msg)
        if reply is None:
            self._send(202)
        else:
            self._send(200, reply)
