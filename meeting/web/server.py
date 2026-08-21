"""Threaded uvicorn server hosting the meeting dashboard.

Runs ``uvicorn.Server`` on a dedicated daemon thread with its own asyncio
event loop so the dashboard keeps serving regardless of what the host
application (Qt or otherwise) does on its main thread. Signal handlers are
never installed. Stop is cooperative via ``should_exit``.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple

try:
    import uvicorn
except ModuleNotFoundError as exc:  # pragma: no cover - exercised via runtime
    if exc.name != "uvicorn":
        raise
    raise ModuleNotFoundError(
        "Meeting Mode needs the dashboard dependencies. Reinstall this "
        "branch's environment with `python -m pip install -r requirements.txt` "
        "(missing module: uvicorn)."
    ) from exc

from meeting.web.api import create_app
from meeting.web.ws import WsHub

logger = logging.getLogger(__name__)

STARTUP_TIMEOUT_S = 15.0
STOP_JOIN_TIMEOUT_S = 10.0


class _ThreadedUvicornServer(uvicorn.Server):
    """A uvicorn server that never installs signal handlers.

    Signal handlers only work on the main thread; this server always runs on
    a worker thread and is stopped via ``should_exit`` instead.
    """

    def install_signal_handlers(self) -> None:  # pragma: no cover
        pass


class MeetingWebServer:
    """Implements ``meeting.interfaces.TransportServer`` over FastAPI/uvicorn."""

    def __init__(self, engine: Any, repository: Any, bind: str = "localhost",
                 port: int = 0) -> None:
        self._engine = engine
        self._repository = repository
        self._bind = bind if bind in ("localhost", "lan") else "localhost"
        self._port = int(port or 0)
        self._hub = WsHub(engine, repository)
        self._hub.get_guest_url = lambda: self.guest_url
        self._app = create_app(engine, repository, self._hub)
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._base_url = ""

    @property
    def app(self) -> Any:
        """The FastAPI application (exposed for tests)."""
        return self._app

    @property
    def base_url(self) -> str:
        """The base URL returned by ``start()`` (empty before start)."""
        return self._base_url

    @property
    def host_url(self) -> str:
        """Tokenized dashboard URL for the host, or '' when unavailable."""
        return self._tokenized_url("host_token")

    @property
    def guest_url(self) -> str:
        """Tokenized dashboard URL for guests, or '' when unavailable."""
        return self._tokenized_url("guest_token")

    def _tokenized_url(self, key: str) -> str:
        if not self._base_url:
            return ""
        meeting_id = getattr(self._engine, "meeting_id", None)
        if not meeting_id:
            return ""
        try:
            meeting = self._repository.get_meeting(meeting_id)
        except Exception:
            logger.exception("Failed to read meeting %s for %s", meeting_id, key)
            return ""
        token = (meeting or {}).get(key)
        return f"{self._base_url}/m/{token}" if token else ""

    def start(self) -> str:
        """Start serving on a daemon thread.

        Returns:
            The base URL (``http://host:port``) once the listener is up.

        Raises:
            RuntimeError: When the server thread dies before binding.
            TimeoutError: When startup exceeds ``STARTUP_TIMEOUT_S``.
        """
        if self._thread is not None and self._thread.is_alive():
            return self._base_url

        listen_host = "127.0.0.1" if self._bind == "localhost" else "0.0.0.0"
        config = uvicorn.Config(
            self._app,
            host=listen_host,
            port=self._port,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
        server = _ThreadedUvicornServer(config)
        self._server = server

        thread = threading.Thread(
            target=self._run, args=(server,),
            name="MeetingWebServer", daemon=True,
        )
        self._thread = thread
        thread.start()

        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while not server.started:
            if not thread.is_alive():
                self._server = None
                self._thread = None
                raise RuntimeError("Meeting web server failed to start")
            if time.monotonic() > deadline:
                server.should_exit = True
                raise TimeoutError("Meeting web server startup timed out")
            time.sleep(0.05)

        port = self._bound_port(server)
        self._base_url = f"http://{self._display_host()}:{port}"
        logger.info("Meeting web server listening at %s (bind=%s)",
                    self._base_url, self._bind)
        return self._base_url

    def _run(self, server: uvicorn.Server) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(server.serve())
        except Exception:
            logger.exception("Meeting web server crashed")
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                logger.debug("shutdown_asyncgens failed", exc_info=True)
            asyncio.set_event_loop(None)
            loop.close()
            self._loop = None

    def _bound_port(self, server: uvicorn.Server) -> int:
        try:
            return server.servers[0].sockets[0].getsockname()[1]
        except Exception:
            logger.exception("Could not read bound port; assuming %s",
                             self._port)
            return self._port

    def _display_host(self) -> str:
        """Host component for the returned URLs.

        Localhost bind always advertises 127.0.0.1. LAN bind makes a
        best-effort guess at the machine's LAN address and falls back to
        127.0.0.1 when resolution fails.
        """
        if self._bind != "lan":
            return "127.0.0.1"
        try:
            host = socket.gethostbyname(socket.gethostname())
            return host or "127.0.0.1"
        except OSError:
            logger.warning("Could not resolve LAN address; using 127.0.0.1")
            return "127.0.0.1"

    def stop(self) -> None:
        """Request shutdown and join the server thread (10s budget)."""
        server = self._server
        if server is not None:
            server.should_exit = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=STOP_JOIN_TIMEOUT_S)
            if thread.is_alive():
                logger.warning("Meeting web server did not stop within %.0fs",
                               STOP_JOIN_TIMEOUT_S)
        self._server = None
        self._thread = None
        self._base_url = ""

    def is_running(self) -> bool:
        """True while the server thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def broadcast(self, message: Dict[str, Any], *,
                  host_only: bool = False) -> None:
        """Push a JSON-serializable message to connected dashboard clients.

        Thread-safe: marshals into the server's event loop via the hub.

        Args:
            message: JSON-serializable payload.
            host_only: When True, only host-authenticated sockets receive it.
        """
        self._hub.schedule_broadcast(dict(message), host_only=host_only)

    def invalidate_connections(self) -> None:
        """Close sockets authenticated with the previous token pair."""
        self._hub.schedule_invalidate_connections()
