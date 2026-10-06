"""History actions reached from outside the history sidebar (tray, shortcuts)."""

import logging
import sys

from services.history_manager import ORIGINAL_VERSION, entry_version, history_manager

logger = logging.getLogger(__name__)

NOTHING_YET = "Nothing to paste yet"
NOTHING_TO_COPY = "Nothing to copy yet"
NOT_EDITED = "AI cleanup didn't change your last dictation"
PASTED = "Pasted the original of your last dictation"
UNAVAILABLE = "Pasting isn't available right now"
COPIED = (
    "Copied the original of your last dictation — paste it with "
    + ("Cmd+V" if sys.platform == "darwin" else "Ctrl+V")
)
COPY_FAILED = "Couldn't copy the original of your last dictation"


def _last_edited_dictation(ui, nothing_yet: str):
    """The last dictation if AI cleanup changed it, else None with the status saying why."""
    try:
        entry = history_manager.last_dictation()
    except Exception:
        logger.exception("Could not read the last dictation")
        ui.set_status("Couldn't read your last dictation")
        return None
    if entry is None:
        ui.set_status(nothing_yet)
        return None
    if not entry_version(entry):
        ui.set_status(NOT_EDITED)
        return None
    return entry


def paste_last_original(ui) -> None:
    """Paste the last dictation's text from before AI cleanup at the caret.

    The shortcut's undo for a cleanup that already went into the app: the
    original goes where the cleaned text went, and History marks the entry
    as original. Only the newest dictation counts, so pressing it twice
    never reaches back to an older one.
    """
    entry = _last_edited_dictation(ui, NOTHING_YET)
    if entry is None:
        return
    paste = getattr(ui, "on_paste_text_now", None)
    if not callable(paste):
        ui.set_status(UNAVAILABLE)
        return
    if not paste(entry.raw_text):
        return  # The runtime said why: busy, or the paste itself failed.
    try:
        history_manager.use_version(entry.id, ORIGINAL_VERSION)
    except Exception:
        logger.exception("Could not mark the last dictation as original")
    else:
        ui.refresh_history()
    ui.set_status(PASTED)


def copy_last_original(ui) -> None:
    """Put the last dictation's text from before AI cleanup on the clipboard.

    The tray's version: from a menu there is no telling which window a paste
    would reach, so the user pastes it. Nothing was pasted, so History keeps
    showing the version it showed.
    """
    entry = _last_edited_dictation(ui, NOTHING_TO_COPY)
    if entry is None:
        return
    if not ui.copy_to_clipboard(entry.raw_text):
        ui.set_status(COPY_FAILED)
        return
    ui.set_status(COPIED)
