"""Brings installed-agent lookups to the Qt thread.

Widgets read ``text_rewrite.provider_readiness``, which never starts an
agent; a lookup it starts lands later on a worker thread, and this relay
tells whichever widgets exist then to read again.
"""
from typing import Optional

from PyQt6.QtCore import QObject, pyqtSignal


class _LookupRelay(QObject):
    """Lives for the session, so the lookup thread never holds or emits on a widget."""

    looked_up = pyqtSignal(str)


_relay: Optional[_LookupRelay] = None


def lookup_relay() -> _LookupRelay:
    """The session relay; its ``looked_up(agent_id)`` arrives on the Qt thread."""
    global _relay
    if _relay is None:
        from services import installed_agents

        _relay = _LookupRelay()
        installed_agents.add_lookup_listener(_relay.looked_up.emit)
    return _relay
