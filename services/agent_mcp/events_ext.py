"""The MCP Events draft extension: events/list, subscribe, unsubscribe (stream D).

Contract: docs/design/automation/design.md, decision 10.
"""

from __future__ import annotations


def start_delivery() -> None:
    """Deliver events to saved subscriptions while MCP Events is on; idempotent."""


def stop_delivery() -> None:
    """Stop delivering."""


def reload() -> None:
    """The MCP Events switch changed."""


def subscription_count() -> int:
    """Active subscriptions agents made (Settings shows it)."""
    return 0


def clear_subscriptions() -> int:
    """Forget every subscription and its secret; returns how many there were."""
    return 0
