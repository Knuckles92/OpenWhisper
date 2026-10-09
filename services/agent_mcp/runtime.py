"""App-owned MCP listeners: loopback and optional private Tailscale access."""

import asyncio
import ipaddress
import secrets
import socket
import threading
from dataclasses import dataclass

from services.asyncio_utils import quiet_connection_lost
from services.settings import SettingsKey

DEFAULT_PORT = 8767
CREDENTIAL_NAME = "OPENWHISPER_MCP_TOKEN"


@dataclass(frozen=True)
class ServerStatus:
    state: str
    message: str
    port: int
    remote_url: str = ""

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/mcp"


class TailscaleUnavailable(RuntimeError):
    pass


def _tailscale_address():
    from services.remote_asr import tailscale

    status = tailscale.status()
    try:
        address = str(ipaddress.IPv4Address(status.address))
    except ipaddress.AddressValueError:
        address = ""
    if not status.running or not address or not tailscale.is_tailscale_address(address):
        raise TailscaleUnavailable
    return address, status.dns_name.lower().rstrip(".")


class McpRuntime:
    def __init__(self, database=None, *, credentials=None, settings=None):
        self._database = database
        self._credentials = credentials
        self._settings = settings
        self._on_change = None
        self._meeting_renamer = None
        self._client_history = None
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
                    "running",
                    "Ready for agent connections.",
                    self._status.port,
                    self._status.remote_url,
                )
            return self._status

    def token(self):
        with self._lock:
            return self._token if self.status().state == "running" else ""

    def start(self, port=DEFAULT_PORT, *, tailscale=False):
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
                target=self._serve,
                args=(port, tailscale is True),
                daemon=True,
                name="OpenWhisper-MCP",
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
        self._settings = settings
        if settings.get(SettingsKey.MCP_ENABLED, False) is True:
            port = settings.get(SettingsKey.MCP_PORT, DEFAULT_PORT)
            if (
                isinstance(port, bool)
                or not isinstance(port, int)
                or not 1 <= port <= 65535
            ):
                port = DEFAULT_PORT
            if settings.get(SettingsKey.MCP_TAILSCALE_ENABLED, False) is True:
                self.start(port, tailscale=True)
            else:
                self.start(port)

    def configure_controls(self, *, on_change=None, meeting_renamer=None, client_history=None):
        """Bind thread-safe desktop handlers before starting the listener."""
        self._on_change = on_change
        self._meeting_renamer = meeting_renamer
        self._client_history = client_history

    def bind_settings(self, settings):
        self._settings = settings

    def _serve(self, port, tailscale=False):
        from services.credentials import CredentialStoreError, store

        sockets = []
        error = ""
        try:
            addresses = ["127.0.0.1"]
            allowed_hosts = []
            remote_url = ""
            if tailscale:
                address, dns_name = _tailscale_address()
                addresses.append(address)
                allowed_hosts.append(address)
                if dns_name:
                    allowed_hosts.append(dns_name)
                remote_url = f"http://{address}:{port}/mcp"
            credentials = (
                self._credentials if self._credentials is not None else store()
            )
            token = credentials.get(CREDENTIAL_NAME)
            if not token:
                token = secrets.token_urlsafe(32)
                credentials.set(CREDENTIAL_NAME, token)
            if self._stop.is_set():
                return

            import uvicorn

            from config import config
            from services.agent_mcp.app import create_app
            from services.settings import settings_manager

            # Bind ourselves so an occupied port is recoverable, not uvicorn's
            # SystemExit. Windows' exclusive bind prevents a second listener.
            for address in addresses:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(sock)
                if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((address, port))
                sock.listen(128)
                sock.setblocking(False)
            app = create_app(
                self._database or config.DATABASE_FILE,
                token,
                enabled=lambda: not self._stop.is_set(),
                settings=self._settings
                if self._settings is not None
                else settings_manager,
                on_change=self._on_change,
                meeting_renamer=self._meeting_renamer,
                allowed_hosts=allowed_hosts,
                client_history=self._client_history,
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
                    self._status = ServerStatus(
                        "starting", "Starting MCP…", port, remote_url
                    )
            async def serve() -> None:
                quiet_connection_lost(asyncio.get_running_loop())
                await server.serve(sockets=sockets)

            asyncio.run(serve())
            if not self._stop.is_set():
                error = "MCP stopped unexpectedly. Turn it off and on to retry."
        except CredentialStoreError:
            error = "Could not save the MCP token. Unlock your system credential store, then retry."
        except TailscaleUnavailable:
            error = "Tailscale is not connected. Connect Tailscale, or turn off Tailscale access, then retry."
        except OSError:
            error = f"Could not listen on port {port}. Choose another port or close the app using it."
        except ImportError:
            error = "MCP dependencies are missing. Update OpenWhisper or install its requirements."
        except (Exception, SystemExit):
            # Never display/log credentials, SQL, transcript data, or DB paths.
            error = "Could not start MCP. Verify that OpenWhisper's history database is available, then retry."
        finally:
            for sock in sockets:
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
