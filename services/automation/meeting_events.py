"""Meeting engine events as public ``meeting.*`` events (stream A fills this in)."""

from __future__ import annotations

from typing import Any, Mapping


def forward(kind: str, payload: Mapping[str, Any], *, meeting_id: str = "") -> None:
    """Publish the public event for one routed engine event; Qt thread, never raises.

    Args:
        kind: The engine listener kind (status, segments, ended, voice_command…).
        payload: The engine's payload; may carry private ``_``-prefixed keys.
        meeting_id: The engine's meeting, for payloads that don't name it.
    """
