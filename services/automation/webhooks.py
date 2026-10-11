"""Signed webhook delivery for saved webhooks (stream B fills this in).

Contract: docs/design/automation/design.md, "services/automation/webhooks.py".
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DeliveryRecord:
    event_id: str
    event_type: str
    at: str
    ok: bool
    status: Optional[int]
    elapsed_ms: int
    error: str = ""
    attempt: int = 1


def start() -> None:
    """Begin delivering events to enabled webhooks; idempotent."""


def stop() -> None:
    """Stop delivering; queued deliveries are dropped."""


def reload() -> None:
    """Saved webhooks changed."""


def send_test(hook_id: str) -> Future:
    """Send a test event to one webhook; Future[DeliveryRecord]."""
    future: Future = Future()
    future.set_exception(RuntimeError("Webhooks aren't available yet."))
    return future


def deliveries(hook_id: str) -> list[DeliveryRecord]:
    """Recent deliveries to one webhook, newest first (memory only)."""
    return []


def ensure_secret(hook_id: str) -> str:
    """The webhook's signing secret, created on first use."""
    raise RuntimeError("Webhooks aren't available yet.")


def rotate_secret(hook_id: str) -> str:
    raise RuntimeError("Webhooks aren't available yet.")


def delete_secret(hook_id: str) -> None:
    """Forget a deleted webhook's secret."""


def post_signed(url: str, payload: dict, secret: str, *, timeout_s: float = 5.0) -> DeliveryRecord:
    """POST ``payload`` as JSON signed with ``secret``; one attempt, never raises."""
    return DeliveryRecord("", "", "", False, None, 0, "Webhooks aren't available yet.")
