"""Spoken routing: which action the words name, asked of Fast judgments (stream F).

Contract: docs/design/automation/design.md, decision 4 and
"services/actions/routing.py".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

ROUTE_TIMEOUT_S = 1.5


@dataclass(frozen=True)
class RouteDecision:
    action_id: str
    payload: str
    confidence: float


@dataclass(frozen=True)
class RoutingStatus:
    enabled: bool
    ready: bool
    message: str


def decide(text: str, job, settings: Mapping, *, judge=None) -> Optional[RouteDecision]:
    """The action ``text`` names, or None to paste it; worker thread, never raises."""
    return None


def status(settings: Mapping) -> RoutingStatus:
    return RoutingStatus(False, False, "Spoken routing isn't available yet.")
