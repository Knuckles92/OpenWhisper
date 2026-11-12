"""Stopping the dashboard server must not wait on a browser's open response.

A dashboard tab keeps responses open (the recording player streams audio),
and uvicorn's graceful shutdown waited for them without a limit, so quitting
the app after a meeting sat out the whole stop budget.
"""
import asyncio
import socket
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

from fastapi.responses import StreamingResponse
from starlette.routing import Route

from meeting.web.server import MeetingWebServer


async def _endless_stream(_request):
    async def chunks():
        while True:
            yield b"x" * 1024
            await asyncio.sleep(0.2)

    return StreamingResponse(chunks(), media_type="application/octet-stream")


def test_stop_does_not_wait_for_an_open_response(repo):
    server = MeetingWebServer(SimpleNamespace(meeting_id=None, store=None), repo, port=0)
    server.app.router.routes.insert(0, Route("/test-endless-stream", _endless_stream))
    url = urlsplit(server.start())
    client = socket.create_connection((url.hostname, url.port), timeout=5)
    try:
        client.sendall(
            f"GET /test-endless-stream HTTP/1.1\r\nHost: {url.netloc}\r\n\r\n".encode()
        )
        assert client.recv(4096)  # the response is streaming

        started = time.monotonic()
        server.stop()
        took = time.monotonic() - started
    finally:
        client.close()
        server.stop()

    assert not server.is_running()
    assert took < 5.0, f"stop() took {took:.1f}s with a response open"
