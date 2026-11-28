"""Live tools on paired clients, through the host's MCP.

An assistant connected to a host's MCP can ask what each paired computer is
doing (``list_clients``) and run a live tool there by passing its
``device_id``. ``RoutedTools`` adds that parameter to every tool it registers,
and the host's ``live_call`` sends the call to the client instead of running it
here, once the host's own permission for the tool holds.

The client decides everything else. ``ClientTools`` is the same set of tools
without a listener: it runs a forwarded call against this computer's own
Settings > API & MCP permissions, and only while its user allows live access
through the paired host. Nothing here lets a host widen what a client allows.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError
from pydantic import Field, ValidationError
from pydantic_core import to_jsonable_python

from services.remote_history.channel import (
    LIVE_AVAILABLE,
    ClientToolError,
    ClientUnavailable,
)

logger = logging.getLogger(__name__)

DeviceId = Annotated[
    str | None,
    Field(
        min_length=1,
        max_length=200,
        pattern=r"^[^/\\?#\x00-\x1f]+$",
        description=(
            "Run this on a paired client instead of this computer: a device_id "
            "from list_clients whose live access is available."
        ),
    ),
]

_NO_ANSWER = "The paired client did not answer. It may be offline or busy; check list_clients."
#: The client call the running tool belongs to, when it was given a device_id.
_routed: ContextVar[_ClientCall | None] = ContextVar("openwhisper_client_call", default=None)


class _ClientCall:
    def __init__(self, clients, device_id, tool, arguments):
        self.clients = clients
        self.device_id = device_id
        self.tool = tool
        self.arguments = arguments
        self.sent = False

    def send(self):
        """The client's result for this call, or a ToolError saying why there is none."""
        self.sent = True
        if self.clients is None:
            raise ToolError(f"unsupported: {ClientUnavailable('unsupported')}")
        try:
            return self.clients.call_tool(self.device_id, self.tool, self.arguments)
        except ClientUnavailable as exc:
            message = _NO_ANSWER if exc.code == "unavailable" else str(exc)
            raise ToolError(f"{exc.code}: {message}") from None
        except ClientToolError as exc:
            code, _, message = str(exc).partition(": ")
            raise ToolError(f"{code}: {message} [paired client {self.device_id}]") from None


def routed_call():
    """The paired-client call this tool should be sent to, or None to run it here."""
    return _routed.get()


class RoutedTools:
    """``mcp.tool`` for tools that can also run on a paired client.

    Each tool gains an optional ``device_id``. With it, the tool's own body
    still runs here, so its checks and this computer's permission apply, and
    ``live_call`` forwards the call where it would have touched the app.
    """

    def __init__(self, mcp, clients):
        self._mcp = mcp
        self._clients = clients

    def __getattr__(self, name):
        return getattr(self._mcp, name)

    def tool(self, **options):
        register = self._mcp.tool(**options)

        def decorate(fn):
            register(_with_device(fn, self._clients))
            return fn

        return decorate


def _with_device(fn, clients):
    signature = inspect.signature(fn, eval_str=True)
    device = inspect.Parameter(
        "device_id", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=DeviceId
    )

    async def tool(**arguments):
        device_id = arguments.pop("device_id", None)
        if device_id is None:
            return await fn(**arguments)
        call = _ClientCall(clients, device_id, fn.__name__, to_jsonable_python(arguments))
        token = _routed.set(call)
        try:
            result = await fn(**arguments)
        finally:
            _routed.reset(token)
        if not call.sent:
            # A tool that answered without live_call ran here, not on the client.
            raise ToolError("unsupported: This tool cannot run on a paired client.")
        return result

    tool.__name__ = fn.__name__
    tool.__qualname__ = fn.__qualname__
    tool.__doc__ = fn.__doc__
    tool.__signature__ = signature.replace(
        parameters=[*signature.parameters.values(), device]
    )
    return tool


# ---- what paired clients are doing ----


def describe_clients(clients, presence=None, *, device_id=None, include_state=True):
    """Each paired client: its connection, live access and, when allowed, its live state.

    ``presence`` lists the speech connections open to this host. A client's
    ``state`` is its own ``get_live_state``, read only from clients that allow
    live access; the others say why they have none.
    """
    if clients is None:
        raise ToolError(f"unsupported: {ClientUnavailable('unsupported')}")
    history = {item["device_id"]: item["status"] for item in clients.clients()}
    connected = _connections(presence)
    found = [
        {
            "device_id": item["device_id"],
            "name": item["name"],
            **connected.get(item["device_id"], {"connected": False}),
            "history": history.get(item["device_id"], "unavailable"),
            "live": item["live"],
        }
        for item in clients.live_clients()
        if device_id is None or item["device_id"] == device_id
    ]
    if device_id is not None and not found:
        raise ToolError("not_found: No paired client has that device_id.")
    if include_state:
        asked = [item for item in found if item["live"] == LIVE_AVAILABLE]
        if asked:
            with ThreadPoolExecutor(max_workers=min(8, len(asked))) as pool:
                list(pool.map(lambda item: _add_state(clients, item), asked))
    return {"clients": found}


def _connections(presence):
    """Per device: whether it is connected, since when, and what the host sees it doing."""
    connected: dict = {}
    for item in presence() if presence is not None else ():
        entry = connected.setdefault(
            item["device_id"],
            {"connected": True, "connected_since": item["since"],
             "recording": False, "transcribing": False},
        )
        entry["connected_since"] = min(entry["connected_since"], item["since"])
        entry["recording"] = entry["recording"] or bool(item.get("recording"))
        entry["transcribing"] = entry["transcribing"] or bool(item.get("busy"))
    for entry in connected.values():
        entry["connected_since"] = datetime.fromtimestamp(
            entry["connected_since"], UTC
        ).isoformat()
    return connected


def _add_state(clients, item):
    try:
        item["state"] = clients.call_tool(item["device_id"], "get_live_state", {})
    except ClientUnavailable as exc:
        # It went offline or turned live access off since the list was read.
        item["live"] = "disabled" if exc.code == "live_disabled" else "offline"
    except ClientToolError as exc:
        item["state_error"] = str(exc)


# ---- the client's side ----


class ClientTools:
    """The live tools a paired host's MCP may run on this computer.

    The same tools, validation and permission checks as this computer's own
    MCP server, without a listener. Whether the host may ask at all is the
    user's opt-in, checked by the caller for every call.
    """

    def __init__(self, *, enabled=None, settings=None):
        from services.agent_mcp import app

        if settings is None:
            from services.settings import settings_manager as settings
        self._mcp = MCPServer("OpenWhisper")
        app.register_app_tools(
            self._mcp, app.tool_annotations(), app.live_caller(enabled, settings)
        )

    def call(self, tool, arguments):
        """``{"result": ...}`` from running ``tool`` here, or ``{"error": "code: message"}``."""
        try:
            result = asyncio.run(self._mcp.call_tool(tool, arguments))
        except UnexpectedToolError:
            return {"error": "unavailable: The client could not do that right now."}
        except ToolError as exc:
            return {"error": _refusal(tool, exc)}
        content = getattr(result, "structured_content", None)
        if not isinstance(content, dict):
            return {"error": "unavailable: The client could not do that right now."}
        return {"result": content}


def _refusal(tool, exc):
    text = str(exc).removeprefix(f"Error executing tool {tool}: ")
    if isinstance(exc.__cause__, ValidationError):
        # A host on another version asked with arguments this one doesn't take.
        return f"invalid_parameters: {text}"
    if text.startswith("Unknown tool"):
        return "unsupported: This client's OpenWhisper does not have that tool."
    return text
