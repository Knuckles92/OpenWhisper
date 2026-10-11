#!/usr/bin/env python3
"""Try MCP Events: subscribe to OpenWhisper's events and watch them arrive.

This script plays the part of an agent. It asks OpenWhisper's MCP server
which events it offers (``events/list``), subscribes to some of them
(``events/subscribe``) with a webhook receiver it runs on 127.0.0.1, answers
OpenWhisper's verification challenge, then checks the signature on every
event and prints it. Ctrl+C unsubscribes (``events/unsubscribe``) and exits.

Before running, in OpenWhisper's Settings > Automate > API & MCP:
  1. Turn on MCP, then MCP Events. Leave "Follow live events" on.
  2. Copy the access token.

Run it with any Python 3.9+ (standard library only):
  python3 scripts/mcp_events_demo.py
  python3 scripts/mcp_events_demo.py --events all
  python3 scripts/mcp_events_demo.py --events transcription.completed --raw

Paste the token when asked, or set OPENWHISPER_MCP_TOKEN. Then dictate.

The receiver runs on this computer, so run this where OpenWhisper runs:
an agent on another computer can't have events sent to an http:// address.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import itertools
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_SERVER = "http://127.0.0.1:8767/mcp"
DEFAULT_EVENTS = (
    "recording.started",
    "recording.stopped",
    "transcription.completed",
    "action.ran",
)
# The stateless MCP wire OpenWhisper's server speaks: every request carries
# its protocol version, so there is no initialize handshake or session.
PROTOCOL_VERSION = "2026-07-28"
# Standard Webhooks: reject deliveries signed more than this long ago.
TOLERANCE_S = 300


# -- Standard Webhooks signatures (https://www.standardwebhooks.com) ------------


def new_secret() -> str:
    """A ``whsec_`` secret: we choose it, OpenWhisper signs with it."""
    return "whsec_" + base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def signature_is_valid(secret: str, headers, body: bytes) -> bool:
    """True when a ``v1`` signature over ``id.timestamp.body`` matches and is fresh.

    During a secret change OpenWhisper sends several signatures separated by
    spaces; one matching is enough.
    """
    msg_id = headers.get("webhook-id") or ""
    try:
        timestamp = int(headers.get("webhook-timestamp") or "")
    except ValueError:
        return False
    if not msg_id or abs(time.time() - timestamp) > TOLERANCE_S:
        return False
    key = base64.b64decode(secret[len("whsec_"):])
    signed = f"{msg_id}.{timestamp}.".encode("utf-8") + body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")
    for part in (headers.get("webhook-signature") or "").split():
        version, _, value = part.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


# -- the webhook receiver ------------------------------------------------------


class Receiver(BaseHTTPRequestHandler):
    """Answers the verification challenge and prints each signed event."""

    secret = ""
    raw = False

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not signature_is_valid(self.secret, self.headers, body):
            say("!  Refused a request whose signature didn't match our secret.")
            self._reply(401)
            return
        message = json.loads(body)
        if message.get("type") == "verification":
            # OpenWhisper checks we asked for these events before sending any.
            self._reply(200, {"challenge": message["challenge"]})
            say("✓  Answered OpenWhisper's verification challenge.")
            return
        show_event(message, self.headers.get("X-MCP-Subscription-Id", ""), raw=self.raw)
        self._reply(200)

    def _reply(self, status: int, payload: dict | None = None) -> None:
        data = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:
        pass  # show_event prints what matters


_print_lock = threading.Lock()


def say(text: str) -> None:
    with _print_lock:
        print(text, flush=True)


def show_event(message: dict, subscription_id: str, *, raw: bool) -> None:
    clock = time.strftime("%H:%M:%S")
    name = message.get("name", "?")
    data = message.get("data") or {}
    if raw:
        say(f"{clock}  {name}  (subscription {subscription_id})\n"
            + json.dumps(message, indent=2, ensure_ascii=False))
        return
    if isinstance(data.get("text"), str):
        detail = json.dumps(data["text"], ensure_ascii=False)
    else:
        detail = " ".join(
            f"{key}={value}" for key, value in data.items()
            if isinstance(value, (str, int, float, bool)) and value != "" and key != "job_id"
        )
    if len(detail) > 160:
        detail = detail[:157] + "..."
    say(f"{clock}  {name:<24} {detail}")


# -- the MCP client ------------------------------------------------------------


class RpcError(Exception):
    def __init__(self, error: dict) -> None:
        super().__init__(error.get("message", "Unknown error"))
        self.code = error.get("code")
        self.data = error.get("data")


class McpClient:
    """JSON-RPC over Streamable HTTP, one POST per request."""

    def __init__(self, url: str, token: str) -> None:
        self.url = url
        self.token = token
        self._ids = itertools.count(1)
        # Talk to OpenWhisper directly, never through a proxy from the environment.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, params: dict | None = None) -> dict:
        request_id = next(self._ids)
        params = dict(params or {})
        params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            ).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "Mcp-Method": method,
            },
        )
        try:
            # A subscribe waits for OpenWhisper to verify our receiver (up to 10 s).
            with self._opener.open(request, timeout=30) as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise SystemExit("OpenWhisper refused the access token. Copy it again from "
                                 "Settings > Automate > API & MCP.") from None
            content_type = exc.headers.get("Content-Type", "")
            body = exc.read()
        except urllib.error.URLError as exc:
            raise SystemExit(f"Couldn't reach OpenWhisper at {self.url} ({exc.reason}). "
                             "Is it running with MCP turned on?") from None
        reply = _parse_reply(body, content_type, request_id)
        if "error" in reply:
            raise RpcError(reply["error"])
        return reply["result"]


def _parse_reply(body: bytes, content_type: str, request_id: int) -> dict:
    """The JSON-RPC reply, whether it came as JSON or as a Server-Sent Events stream."""
    text = body.decode("utf-8", "replace")
    if "text/event-stream" in content_type:
        for line in text.splitlines():
            if line.startswith("data:"):
                try:
                    message = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(message, dict) and message.get("id") == request_id:
                    return message
        raise SystemExit("OpenWhisper's MCP server sent an answer this script can't read.")
    try:
        return json.loads(text)
    except ValueError:
        raise SystemExit(f"OpenWhisper's MCP server answered: {text[:200]}") from None


# -- the demo ------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help=f"MCP server URL (default {DEFAULT_SERVER})")
    parser.add_argument("--events", default=",".join(DEFAULT_EVENTS),
                        help="comma-separated event names, or 'all' (default: %(default)s)")
    parser.add_argument("--port", type=int, default=0,
                        help="port for this script's webhook receiver (default: any free port)")
    parser.add_argument("--ttl-minutes", type=float, default=10.0,
                        help="how long each subscription lasts before this script refreshes it")
    parser.add_argument("--raw", action="store_true", help="print each delivery's full JSON")
    args = parser.parse_args()

    try:
        token = os.environ.get("OPENWHISPER_MCP_TOKEN") or getpass.getpass(
            "OpenWhisper MCP access token (Settings > Automate > API & MCP): "
        )
    except (KeyboardInterrupt, EOFError):
        print()
        return 130
    token = token.strip()
    if not token:
        print("An access token is needed.", file=sys.stderr)
        return 2
    client = McpClient(args.server, token)

    try:
        capabilities = client.call("server/discover").get("capabilities", {})
    except RpcError as exc:
        print(f"OpenWhisper didn't answer server/discover ({exc}). MCP Events needs "
              "OpenWhisper 2.7.2 or later.", file=sys.stderr)
        return 1
    if "events" not in capabilities:
        print("MCP Events is off. Turn it on in Settings > Automate > API & MCP, "
              "then run this again.", file=sys.stderr)
        return 1

    try:
        offered = client.call("events/list")["events"]
    except RpcError as exc:
        print(f"events/list was refused: {exc}", file=sys.stderr)
        return 1
    names = [event["name"] for event in offered]
    say(f"OpenWhisper offers {len(names)} events: {', '.join(names)}")
    wanted = names if args.events.strip() == "all" else [
        name.strip() for name in args.events.split(",") if name.strip()
    ]
    unknown = [name for name in wanted if name not in names]
    if unknown:
        print(f"Not an OpenWhisper event: {', '.join(unknown)}", file=sys.stderr)
        return 2

    Receiver.secret = new_secret()
    Receiver.raw = args.raw
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Receiver)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    callback = f"http://127.0.0.1:{server.server_address[1]}/openwhisper-events"
    say(f"Receiving webhooks at {callback}")

    delivery = {"mode": "webhook", "url": callback, "secret": Receiver.secret}
    ttl_ms = int(args.ttl_minutes * 60_000)

    def subscribe_all() -> None:
        for name in wanted:
            result = client.call("events/subscribe", {
                "name": name, "arguments": {}, "delivery": delivery, "ttlMs": ttl_ms,
            })
            status = result.get("deliveryStatus") or {}
            state = ("delivering" if status.get("active") else
                     "not delivering: check that MCP Events and Follow live events are on")
            say(f"   subscribed to {name:<24} until {result['refreshBefore']}  ({state})")

    try:
        say("Subscribing (OpenWhisper first checks that this receiver wants the events)...")
        subscribe_all()
    except RpcError as exc:
        reason = (exc.data or {}).get("reason") if isinstance(exc.data, dict) else None
        print(f"events/subscribe was refused: {exc}" + (f" ({reason})" if reason else ""),
              file=sys.stderr)
        server.shutdown()
        return 1

    say("\nListening. Dictate something in OpenWhisper. Ctrl+C to stop.\n")
    try:
        while True:
            # Subscribing again with the same name, URL and arguments refreshes it.
            time.sleep(max(30.0, ttl_ms / 2000))
            subscribe_all()
    except KeyboardInterrupt:
        say("\nUnsubscribing...")
    finally:
        for name in wanted:
            try:
                client.call("events/unsubscribe", {
                    "name": name, "arguments": {},
                    "delivery": {"mode": "webhook", "url": callback},
                })
            except (RpcError, SystemExit) as exc:
                say(f"   couldn't unsubscribe from {name}: {exc}")
        server.shutdown()
    say("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
