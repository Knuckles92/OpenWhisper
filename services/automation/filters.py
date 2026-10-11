"""Text filters: outside programs that change dictated text (stream B fills this in).

Contract: docs/design/automation/design.md, "services/automation/filters.py".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

LEASE_TTL_S = 60.0


@dataclass(frozen=True)
class FilterOutcome:
    text: str
    canceled: bool = False
    changed: bool = False


@dataclass(frozen=True)
class Lease:
    id: str
    url: str
    stage: str
    timeout_ms: int
    modes: tuple[str, ...]
    expires_at: float


def apply(stage: str, text: str, job, settings: Optional[Mapping] = None) -> FilterOutcome:
    """Run the filters for ``stage``; worker thread. Unchanged when none apply."""
    return FilterOutcome(text)


def register_lease(url: str, stage: str, *, timeout_ms: int = 1500,
                   modes: Sequence[str] = ("dictation", "action")) -> Lease:
    raise ValueError("Text filters aren't available yet.")


def renew_lease(lease_id: str) -> Lease:
    raise KeyError(lease_id)


def drop_lease(lease_id: str) -> bool:
    return False


def leases() -> list[Lease]:
    return []
