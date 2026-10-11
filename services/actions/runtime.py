"""Runs an action once its words are ready (stream C fills this in).

Contract: docs/design/automation/design.md, "services/actions/runtime.py".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    message: str
    output: Optional[str] = None


class ActionRuntime:
    """Delivers an action job's text; one per controller."""

    def __init__(self, controller) -> None:
        self.controller = controller

    def deliver(self, job, text: str, *, raw_text: Optional[str], html: str) -> None:
        """Run ``job``'s action with ``text``; Qt thread, returns at once."""
        ui = self.controller.ui_controller
        ui.discard_clipboard_prefetch()
        copied = ui.copy_to_clipboard(text)
        message = "Copied — actions aren't ready yet" if copied else "Actions aren't ready yet"
        ui.set_status(message)
        show = getattr(ui, "show_notice", None)
        if callable(show):
            show(message)

    def test(self, action, sample_text: str, done: Callable[[ActionResult], None]) -> None:
        """Try ``action`` with ``sample_text`` from Settings; ``done`` on the Qt thread."""
        done(ActionResult(False, "Actions aren't ready yet."))

    def close(self) -> None:
        """Stop work in progress at quit."""
