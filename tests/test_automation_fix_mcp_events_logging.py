"""MCP Events never lets httpx log a callback URL, whatever the app's logging setup.

httpx logs every request's URL, query included, at INFO; a callback URL can
carry a secret in its query. events_ext keeps its own requests out of those
logs the way the webhooks engine does, without relying on app-wide levels.
"""

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from services.agent_mcp import events_ext

SECRET_QUERY = "token=QUERY-SECRET"
HTTP_LOGGERS = ("httpx", "httpcore.connection", "httpcore.http11")


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/hook/PATH-SECRET?{SECRET_QUERY}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def verbose_http(caplog):
    """httpx and httpcore logging everything, as an app at DEBUG would."""
    previous = {name: logging.getLogger(name).level for name in HTTP_LOGGERS}
    for name in HTTP_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    yield caplog
    for name, level in previous.items():
        logging.getLogger(name).setLevel(level)


def test_deliveries_and_verifications_leave_no_url_in_the_logs(server, verbose_http):
    client = events_ext._new_client()
    try:
        reply = events_ext._post(server, b"{}", {"content-type": "application/json"}, 5.0, client=client)
    finally:
        client.close()
    assert reply.ok
    # A one-off request, as a verification challenge makes.
    assert events_ext._post(server, b"{}", {"content-type": "application/json"}, 5.0).ok
    assert "QUERY-SECRET" not in verbose_http.text
    assert "PATH-SECRET" not in verbose_http.text


def test_other_requests_keep_their_log_lines(server, verbose_http):
    events_ext._new_client().close()  # the filter is installed
    with httpx.Client(trust_env=False) as other:
        other.post(server, content=b"{}")
    assert "QUERY-SECRET" in verbose_http.text
