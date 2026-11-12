"""Ask once whether AI cleanup should run through Claude Code or Codex.

``services.cleanup_agent_offer`` decides when the question is due and saves
the answer; this looks for the agents off the UI thread and shows it.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

from services import cleanup_agent_offer
from services.installed_agents import InstalledAgent
from services.settings import resolve_transcript_cleanup_provider
from services.text_llm import profile_display_name
from ui_qt.widgets import agent_picker

logger = logging.getLogger(__name__)


def ask(parent: Optional[QWidget], agents: List[InstalledAgent], current: str) -> Optional[str]:
    """Show the question; the chosen agent's id, or None to keep ``current``."""
    names = [agent.spec.name for agent in agents]
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Question)
    box.setWindowTitle("AI cleanup")
    if len(agents) == 1:
        box.setText(f"Use {names[0]} for AI cleanup?")
        found = f"{names[0]} is installed and signed in on this computer."
        sent = f"{names[0]}'s model"
    else:
        box.setText("Use a coding agent for AI cleanup?")
        found = f"{' and '.join(names)} are installed and signed in on this computer."
        sent = "the model of the one you pick"
    box.setInformativeText(
        f"{found} AI cleanup can run through the account you already use "
        f"there, with no API key. Your transcript text is sent to {sent}; "
        "no audio is sent.\n\n"
        "You can change this any time in Settings → AI cleanup."
    )
    buttons = {
        box.addButton(f"Use {agent.spec.name}", QMessageBox.ButtonRole.AcceptRole): agent.id
        for agent in agents
    }
    keep = box.addButton(f"Keep {current}", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(next(iter(buttons)))
    box.setEscapeButton(keep)
    box.exec()
    return buttons.get(box.clickedButton())


class CleanupAgentOffer(QObject):
    """Makes the offer when it is due; safe to call on every cleanup change.

    Attributes:
        on_accepted: Called after the runner was switched, to refresh views.
    """

    _scanned = pyqtSignal(object)

    def __init__(self, parent_widget: QWidget, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._parent_widget = parent_widget
        self._busy = False
        self.on_accepted: Optional[Callable[[], None]] = None
        # Queued: the question never opens inside the toggle that caused it.
        self._scanned.connect(self._on_scanned, Qt.ConnectionType.QueuedConnection)

    def maybe_offer(self) -> None:
        if self._busy or not self._pending():
            return
        self._busy = True
        cached = agent_picker.cached_agents()
        if cached is not None:
            self._scanned.emit(cached)
            return
        # Bound here: the worker must not reach back into this object. The
        # picker's scan, so tests that stub it never run a real agent.
        scan = agent_picker.scan_installed_agents
        done = self._scanned

        def worker() -> None:
            try:
                result = scan()
            except Exception:
                logger.warning("Scanning for a cleanup agent failed", exc_info=True)
                result = {}
            done.emit(result)

        threading.Thread(target=worker, name="cleanup-agent-offer", daemon=True).start()

    @staticmethod
    def _pending() -> bool:
        try:
            return cleanup_agent_offer.pending()
        except Exception:
            logger.warning("Could not check the cleanup agent offer", exc_info=True)
            return False

    def _on_scanned(self, scan) -> None:
        try:
            agents = cleanup_agent_offer.offerable(scan)
            # Rechecked: the scan takes seconds and the user may have moved on.
            if not agents or not self._pending():
                return
            current = profile_display_name(resolve_transcript_cleanup_provider())
            parent = QApplication.activeWindow() or self._parent_widget
            choice = ask(parent, agents, current)
            if choice is None:
                cleanup_agent_offer.decline()
                return
            cleanup_agent_offer.accept(choice)
            logger.info("AI cleanup now runs through %s", choice)
            if self.on_accepted:
                self.on_accepted()
        except Exception:
            logger.warning("The cleanup agent offer failed", exc_info=True)
        finally:
            self._busy = False
