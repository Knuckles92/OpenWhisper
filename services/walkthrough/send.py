"""Send a finished walkthrough to a coding agent.

The destinations are T3 Code (a new thread in a project, or one of the
project's threads) and a new session of Claude Code, Codex or OpenCode
installed on this computer. Every one gets the same message: what to do,
then the walkthrough's words with each screenshot's full path (see
``WalkthroughResult.agent_message``).

Folders chosen for an installed agent are remembered, newest first, beside
T3 Code's own projects. ``send`` blocks; run it on a worker.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from services import t3_code
from services.actions import agents
from services.actions.model import AGENT_NAMES
from services.settings import SettingsKey, settings_manager

logger = logging.getLogger(__name__)

T3 = "t3"
T3_NAME = "T3 Code"
#: How many project folders Send remembers.
RECENT_FOLDERS = 6


class SendError(Exception):
    """Why a walkthrough didn't go, in words for the user."""


def folder_name(folder: str) -> str:
    """What a project folder is called: its last part."""
    return os.path.basename(os.path.normpath(folder)) if folder else ""


@dataclass(frozen=True)
class Destination:
    """Where a walkthrough goes."""

    #: ``t3``, or an installed agent's id (``claude_code``, ``codex``, ``opencode_cli``).
    target: str
    #: The project folder the agent works in; "" lets an installed agent use its own.
    folder: str = ""
    #: A T3 Code thread to add to, or "" for a new thread.
    thread_id: str = ""
    thread_title: str = ""

    @property
    def name(self) -> str:
        """The app: ``T3 Code``, ``Claude Code``, …"""
        return T3_NAME if self.target == T3 else AGENT_NAMES.get(self.target, self.target)

    @property
    def place(self) -> str:
        """Where in that app: ``a new thread in whisper_local``, ``“Fix billing”``."""
        project = folder_name(self.folder)
        if self.target == T3 and self.thread_id:
            return f"“{self.thread_title}”" if self.thread_title else f"a thread in {project}"
        kind = "thread" if self.target == T3 else "session"
        return f"a new {kind} in {project}" if project else f"a new {kind}"


def folders() -> list[str]:
    """Project folders chosen for Send that still exist, newest first."""
    saved = SettingsKey.WALKTHROUGH_SEND_FOLDERS.read(settings_manager.load_all_settings())
    return [folder for folder in saved if isinstance(folder, str) and os.path.isdir(folder)]


def choose_folder(folder: str) -> None:
    """Make ``folder`` the project Send works in."""
    key = os.path.normcase(os.path.normpath(folder))
    others = [kept for kept in folders() if os.path.normcase(os.path.normpath(kept)) != key]
    # Failing to remember a folder shouldn't stop the walkthrough going.
    try:
        settings_manager.save_setting(SettingsKey.WALKTHROUGH_SEND_FOLDERS,
                                      [folder, *others][:RECENT_FOLDERS])
    except (OSError, ValueError):
        logger.warning("Couldn't save the project folders for Send", exc_info=True)


def send(result, dest: Destination) -> str:
    """Hand ``result`` (a ``WalkthroughResult``) to ``dest``.

    Returns:
        What to tell the user it did.

    Raises:
        SendError: It didn't go; the message says why.
    """
    text = result.agent_message()
    if dest.target == T3:
        try:
            if dest.thread_id:
                t3_code.send(dest.folder, dest.thread_id, text)
                return f"Sent to {dest.place} in {T3_NAME}."
            t3_code.start(dest.folder, text)
        except t3_code.T3Error as exc:
            raise SendError(str(exc)) from exc
        return f"Started {dest.place} in {T3_NAME}."
    outcome = agents.handoff(dest.target, text, folder=dest.folder)
    if not outcome.ok:
        raise SendError(outcome.message)
    return f"Sent to {dest.name}."
