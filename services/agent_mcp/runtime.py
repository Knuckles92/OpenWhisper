"""One app-owned MCP listener. No Qt imports, subprocess, or model loading."""

import asyncio
import secrets
import socket
import threading
from dataclasses import dataclass

from services.settings import SettingsKey

DEFAULT_PORT = 8767
CREDENTIAL_NAME = "OPENWHISPER_MCP_TOKEN"


@dataclass(frozen=True)
class ServerStatus:
    state: str
    message: str
    port: int

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/mcp"


class McpRuntime:
    def __init__(self, database=None, *, credentials=None):
        self._database = database
        self._credentials = credentials
        self._lock = threading.RLock()
        self._thread = None
        self._server = None
        self._stop = threading.Event()
        self._token = ""
        self._status = ServerStatus("stopped", "MCP is off.", DEFAULT_PORT)

    def status(self):
        with self._lock:
            if (
                self._status.state == "starting"
                and self._server
                and self._server.started
            ):
                self._status = ServerStatus(
                    "running", "Ready for agent connections.", self._status.port
                )
            return self._status

    def token(self):
        with self._lock:
            return self._token if self.status().state == "running" else ""

    def start(self, port=DEFAULT_PORT):
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
        ):
            raise ValueError("MCP port must be between 1 and 65535.")
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._token = ""
            self._server = None
            self._status = ServerStatus("starting", "Starting MCP…", port)
            self._thread = threading.Thread(
                target=self._serve, args=(port,), daemon=True, name="OpenWhisper-MCP"
            )
            self._thread.start()

    def stop(self, *, wait=False):
        with self._lock:
            self._stop.set()
            self._token = ""
            if self._server is not None:
                self._server.should_exit = True
            alive = self._thread and self._thread.is_alive()
            self._status = ServerStatus(
                "stopping" if alive else "stopped",
                "Stopping MCP…" if alive else "MCP is off.",
                self._status.port,
            )
            thread = self._thread
        if wait and thread:
            thread.join(timeout=7)

    def restore(self, settings):
        if settings.get(SettingsKey.MCP_ENABLED, False) is True:
            port = settings.get(SettingsKey.MCP_PORT, DEFAULT_PORT)
            if (
                isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= 65535
            ):
                port = DEFAULT_PORT
            self.start(port)

    def _serve(self, port):
        from services.credentials import CredentialStoreError, store

        sock = None
        error = ""
        try:
            credentials = self._credentials if self._credentials is not None else store
            token = credentials.get(CREDENTIAL_NAME)
            if not token:
                token = secrets.token_urlsafe(32)
                credentials.set(CREDENTIAL_NAME, token)
            if self._stop.is_set():
                return

            import uvicorn

            from config import config
            from services.agent_mcp.app import create_app

            # Bind ourselves so an occupied port is recoverable, not uvicorn's
            # SystemExit. Windows' exclusive bind prevents a second listener.
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
            sock.listen(128)
            sock.setblocking(False)
            app = create_app(
                self._database or config.DATABASE_FILE,
                token,
                enabled=lambda: not self._stop.is_set(),
            )
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="127.0.0.1",
                    port=port,
                    access_log=False,
                    proxy_headers=False,
                    log_config=None,
                    log_level="warning",
                    ws="none",
                    lifespan="on",
                    timeout_graceful_shutdown=3,
                )
            )
            with self._lock:
                self._server = server
                if self._stop.is_set():
                    server.should_exit = True
                else:
                    self._token = token
            asyncio.run(server.serve(sockets=[sock]))
            if not self._stop.is_set():
                error = "MCP stopped unexpectedly. Turn it off and on to retry."
        except CredentialStoreError:
            error = "Could not save the MCP token. Unlock your system credential store, then retry."
        except OSError:
            error = f"Could not listen on port {port}. Choose another port or close the app using it."
        except ImportError:
            error = "MCP dependencies are missing. Update OpenWhisper or install its requirements."
        except (Exception, SystemExit):
            # Never display/log credentials, SQL, transcript data, or DB paths.
            error = "Could not start MCP. Verify that OpenWhisper's history database is available, then retry."
        finally:
            if sock is not None:
                sock.close()
            with self._lock:
                self._token = ""
                self._server = None
                self._status = ServerStatus(
                    "error" if error and not self._stop.is_set() else "stopped",
                    error if error and not self._stop.is_set() else "MCP is off.",
                    port,
                )


runtime = McpRuntime()
