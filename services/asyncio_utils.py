"""Helpers for the app's embedded asyncio servers."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def quiet_connection_lost(loop: asyncio.AbstractEventLoop) -> None:
    """Keep a peer's connection reset from being logged as an ERROR.

    On Windows, asyncio's proactor transport calls ``socket.shutdown`` while
    closing a connection and raises ``ConnectionResetError`` when the other
    side (a browser tab, an MCP client) has already dropped it. The socket is
    closed either way, so the traceback is noise; every other loop error keeps
    the default handling.
    """

    def handler(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
        exc = context.get("exception")
        if (
            isinstance(exc, (ConnectionResetError, ConnectionAbortedError))
            and "_call_connection_lost" in repr(context.get("handle"))
        ):
            logger.debug("Peer reset the connection while closing: %s", exc)
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(handler)
