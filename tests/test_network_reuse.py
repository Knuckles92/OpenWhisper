"""Connection reuse and retry caps on the cleanup path, against local servers only."""
import functools
import http.client
import http.server
import json
import threading
import time
import urllib.error
from unittest.mock import Mock

import httpx
import pytest
from openai import OpenAI

from config import config
from services import typesafe
from services.typesafe import TypeSafeJudge

GOOD = {"answers": {"q": {"type": "noul", "noul": 0.9}}}


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        port = self.client_address[1]
        server = self.server
        with server.lock:
            server.requests.append({"port": port, "auth": self.headers.get("Authorization"),
                                    "body": body})
            nth = sum(1 for request in server.requests if request["port"] == port)
        server.behavior(self, nth)


def answer(handler, status=200, payload=GOOD):
    data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def drop(handler):
    """Close the connection without answering, as a server reaping it would."""
    handler.close_connection = True


@pytest.fixture
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.lock = threading.Lock()
    srv.requests = []
    srv.behavior = lambda handler, nth: answer(handler)
    srv.handle_error = lambda request, address: None  # client hung up first
    srv.url = f"http://127.0.0.1:{srv.server_port}/v1/systemone"
    srv.connections = lambda: len({request["port"] for request in srv.requests})
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def pool(monkeypatch):
    fresh = typesafe._KeepAlivePool()
    monkeypatch.setattr(typesafe, "_POOL", fresh)
    # Local servers only: never route these through a machine's proxy.
    monkeypatch.setattr(typesafe, "_proxied", lambda parts: False)
    yield fresh
    for origin in list(fresh._idle):
        fresh.discard(origin)


def post(url, timeout_s=5.0):
    payload = {"model": typesafe.MODEL, "state": {"text": "ok"}, "questions": {"q": {"type": "noul"}}}
    return typesafe._http_post(payload, timeout_s, api_key="k", endpoint=url)


class TestTypeSafeKeepAlive:
    def test_sequential_judgments_share_one_connection(self, server, pool):
        judge = TypeSafeJudge("secret-key", endpoint=server.url)
        assert [judge.noul({"text": "hi"}, "Is it?") for _ in range(5)] == [0.9] * 5
        assert len(server.requests) == 5
        assert server.connections() == 1
        assert server.requests[0]["auth"] == "Bearer secret-key"
        assert json.loads(server.requests[0]["body"])["model"] == typesafe.MODEL

    def test_concurrent_judges_never_share_a_socket(self, server, pool):
        judge = TypeSafeJudge("k", endpoint=server.url)
        barrier = threading.Barrier(4)
        results = []

        def worker():
            barrier.wait()
            for _ in range(5):
                results.append(judge.noul({"text": "hi"}, "Is it?"))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        # Interleaved writes on one socket would corrupt answers.
        assert results == [0.9] * 20
        assert judge.usage.failures == 0
        assert server.connections() <= 4

    def test_http_error_keeps_only_the_status_and_the_connection(self, server, pool):
        server.behavior = lambda handler, nth: answer(handler, 503, "secret-sentinel")
        assert post(server.url) == (503, "")
        server.behavior = lambda handler, nth: answer(handler)
        status, text = post(server.url)
        assert status == 200 and json.loads(text) == GOOD
        assert server.connections() == 1

    def test_connection_the_server_closed_while_idle_is_replaced(self, server, pool, monkeypatch):
        def answer_then_close(handler, nth):
            answer(handler)
            handler.close_connection = True  # without saying so in a header

        server.behavior = answer_then_close
        discards = Mock(side_effect=pool.discard)
        monkeypatch.setattr(pool, "discard", discards)
        assert post(server.url)[0] == 200
        time.sleep(0.2)  # let the FIN arrive
        assert post(server.url)[0] == 200
        assert server.connections() == 2
        # Caught before sending, so nothing needed a second attempt.
        assert len(server.requests) == 2
        discards.assert_not_called()

    def test_request_dropped_on_a_reused_connection_is_sent_once_more(self, server, pool):
        server.behavior = lambda handler, nth: drop(handler) if nth == 2 else answer(handler)
        assert post(server.url)[0] == 200
        status, text = post(server.url)
        assert status == 200 and json.loads(text) == GOOD
        assert len(server.requests) == 3  # two on the first connection, one on the next
        assert server.connections() == 2

    def test_failure_on_a_fresh_connection_is_not_retried(self, server, pool):
        server.behavior = lambda handler, nth: drop(handler)
        judge = TypeSafeJudge("k", endpoint=server.url)
        assert judge.noul("x", "Is it?") is None
        assert judge.usage.failures == 1
        assert len(server.requests) == 1

    def test_connection_idle_past_the_window_is_not_reused(self, server, monkeypatch):
        clock = [0.0]
        pool = typesafe._KeepAlivePool(idle_s=10.0, monotonic=lambda: clock[0])
        monkeypatch.setattr(typesafe, "_POOL", pool)
        monkeypatch.setattr(typesafe, "_proxied", lambda parts: False)
        assert post(server.url)[0] == 200
        clock[0] = 9.0
        assert post(server.url)[0] == 200
        assert server.connections() == 1
        clock[0] = 20.0
        assert post(server.url)[0] == 200
        assert server.connections() == 2
        for origin in list(pool._idle):
            pool.discard(origin)

    def test_timeout_never_outlasts_the_requests_budget(self, server, pool):
        assert post(server.url, timeout_s=5.0)[0] == 200

        def slow(handler, nth):
            time.sleep(1.0)
            answer(handler)

        server.behavior = slow
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            post(server.url, timeout_s=0.2)
        assert time.monotonic() - started < 0.9
        # The reused socket used the whole budget, so nothing was left for a
        # fresh attempt, and the silent socket was not handed back.
        assert len(server.requests) == 2 and server.connections() == 1
        assert not any(pool._idle.values())

    def test_silent_reused_connection_is_retried_fresh_within_the_budget(self, server, pool):
        # A NAT that forgot an idle connection: the request goes out and
        # nothing ever comes back on that socket.
        assert post(server.url, timeout_s=5.0)[0] == 200
        first_port = server.requests[0]["port"]

        def forgotten(handler, nth):
            if handler.client_address[1] == first_port:
                time.sleep(2.0)
            answer(handler)

        server.behavior = forgotten
        started = time.monotonic()
        status, text = post(server.url, timeout_s=2.2)
        elapsed = time.monotonic() - started
        assert status == 200 and json.loads(text) == GOOD
        # Gave the reused socket half the budget (1.1 s), then a fresh one.
        assert 1.0 < elapsed < 2.0
        assert server.connections() == 2

    def test_https_connections_use_the_shared_verified_context(self, monkeypatch, pool):
        context = object()
        monkeypatch.setattr(typesafe, "verified_context", lambda: context)
        response = Mock(status=200, will_close=False)
        response.read.return_value = json.dumps(GOOD).encode()
        connection = Mock()
        connection.getresponse.return_value = response
        https = Mock(return_value=connection)
        monkeypatch.setattr(typesafe.http.client, "HTTPSConnection", https)
        monkeypatch.setattr(typesafe, "_still_open", lambda conn: True)
        assert post(typesafe.ENDPOINT, timeout_s=3.0)[0] == 200
        assert post(typesafe.ENDPOINT, timeout_s=2.0)[0] == 200
        https.assert_called_once_with("api.typesafe.ai", 443, timeout=3.0, context=context)
        assert connection.request.call_args.args == ("POST", "/v1/systemone")
        # A reused socket may stay silent for half the 2 s budget, at least 1 s.
        connection.sock.settimeout.assert_called_with(1.0)


class TestProxyFallback:
    def test_proxied_endpoint_goes_through_urllib(self, monkeypatch):
        monkeypatch.setattr(typesafe.urllib.request, "getproxies",
                            lambda: {"https": "http://proxy.invalid:3128"})
        monkeypatch.setattr(typesafe.urllib.request, "proxy_bypass", lambda host: False)
        context = object()
        monkeypatch.setattr(typesafe, "verified_context", lambda: context)
        https = Mock()
        monkeypatch.setattr(typesafe.http.client, "HTTPSConnection", https)
        response = Mock(status=200)
        response.read.return_value = json.dumps(GOOD).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=None)
        urlopen = Mock(return_value=response)
        monkeypatch.setattr(typesafe.urllib.request, "urlopen", urlopen)
        assert post(typesafe.ENDPOINT, timeout_s=3.0) == (200, json.dumps(GOOD))
        assert urlopen.call_args.kwargs == {"timeout": 3.0, "context": context}
        https.assert_not_called()
        urlopen.side_effect = urllib.error.HTTPError(
            typesafe.ENDPOINT, 401, "secret-sentinel", {}, None)
        assert post(typesafe.ENDPOINT) == (401, "")

    def test_proxy_detection(self, monkeypatch):
        parts = typesafe.urllib.parse.urlsplit(typesafe.ENDPOINT)
        monkeypatch.setattr(typesafe.urllib.request, "getproxies", lambda: {})
        assert typesafe._proxied(parts) is False
        monkeypatch.setattr(typesafe.urllib.request, "getproxies",
                            lambda: {"https": "http://proxy.invalid:3128"})
        monkeypatch.setattr(typesafe.urllib.request, "proxy_bypass", lambda host: True)
        assert typesafe._proxied(parts) is False
        monkeypatch.setattr(typesafe.urllib.request, "proxy_bypass", lambda host: False)
        assert typesafe._proxied(parts) is True

        def broken():
            raise OSError("registry unreadable")

        # When in doubt, let urllib apply its own proxy rules.
        monkeypatch.setattr(typesafe.urllib.request, "getproxies", broken)
        assert typesafe._proxied(parts) is True


class TestCleanupRetryCap:
    """Dictation cleanup retries once; combined batch cleanup keeps two retries."""

    @staticmethod
    def _cleaner(monkeypatch, status=500):
        from services.transcript_cleanup import TranscriptCleanup

        attempts = []

        def handler(request):
            attempts.append(request)
            # Keep the SDK's backoff sleep to a millisecond.
            return httpx.Response(status, headers={"retry-after-ms": "1"},
                                  json={"error": {"message": "synthetic"}})

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr("services.text_llm.OpenAI", functools.partial(
            OpenAI, http_client=httpx.Client(transport=transport)))
        return TranscriptCleanup(provider="openai", model="gpt-test", api_key="test-key"), attempts

    def test_worst_case_dictation_wait_is_two_timeouts(self):
        assert config.TRANSCRIPT_CLEANUP_MAX_RETRIES == 1
        attempts = config.TRANSCRIPT_CLEANUP_MAX_RETRIES + 1
        assert attempts * config.TRANSCRIPT_CLEANUP_TIMEOUT_S <= 16.0
        assert config.TRANSCRIPT_BATCH_CLEANUP_MAX_RETRIES == 2

    def test_dictation_cleanup_makes_at_most_two_attempts(self, monkeypatch):
        cleaner, attempts = self._cleaner(monkeypatch)
        assert cleaner.client.max_retries == config.TRANSCRIPT_CLEANUP_MAX_RETRIES
        assert cleaner.cleanup("raw text") == "raw text"
        assert cleaner.last_error is not None
        assert len(attempts) == 2
        assert attempts[0].extensions["timeout"]["read"] == config.TRANSCRIPT_CLEANUP_TIMEOUT_S

    def test_batch_cleanup_keeps_two_retries_and_its_timeout(self, monkeypatch):
        cleaner, attempts = self._cleaner(monkeypatch)
        assert cleaner.cleanup(
            "raw text", timeout_s=config.TRANSCRIPT_BATCH_CLEANUP_TIMEOUT_S) == "raw text"
        assert len(attempts) == 3
        assert attempts[0].extensions["timeout"]["read"] == config.TRANSCRIPT_BATCH_CLEANUP_TIMEOUT_S
        # The override leaves the dictation client alone.
        assert cleaner.client.max_retries == config.TRANSCRIPT_CLEANUP_MAX_RETRIES

    def test_batch_override_shares_the_connection_pool(self, monkeypatch):
        cleaner, _ = self._cleaner(monkeypatch)
        batch = cleaner.client.with_options(max_retries=config.TRANSCRIPT_BATCH_CLEANUP_MAX_RETRIES)
        assert batch._client is cleaner.client._client

    def test_other_clients_keep_the_sdk_default(self):
        from openai import DEFAULT_MAX_RETRIES
        from services.text_llm import builtin_profile, create_openai_client

        client = create_openai_client(builtin_profile("openai"), api_key="test-key")
        try:
            assert client.max_retries == DEFAULT_MAX_RETRIES
        finally:
            client.close()
