"""Agent actions: hand words to an installed coding agent, or ask it (stream C2).

Contract: docs/design/automation/design.md, "services/actions/agents.py".
Both calls block; run them on a worker.
"""

from __future__ import annotations

import threading
from typing import Optional

from services.actions.runtime import ActionResult


def available() -> dict[str, bool]:
    """Which installed agents were found by the last scan; never blocks."""
    return {}


def handoff(agent_id: str, text: str, *, folder: str = "", surface: str = "auto") -> ActionResult:
    """Open a visible session of ``agent_id`` with ``text`` ready."""
    return ActionResult(False, "Agent actions aren't available yet.")


def ask(agent_id: str, text: str, *, model: str = "", timeout_s: float = 120.0,
        cancel: Optional[threading.Event] = None) -> ActionResult:
    """Ask ``agent_id`` with no tools; ``output`` is the answer."""
    return ActionResult(False, "Agent actions aren't available yet.")
