"""History actions reached from outside the history sidebar (tray, shortcuts)."""

import logging

from services.history_manager import ORIGINAL_VERSION, entry_version, history_manager

logger = logging.getLogger(__name__)

NOTHING_YET = "Nothing to paste yet"
NOT_EDITED = "AI cleanup didn't change your last dictation"
PASTED = "Pasted the original of your last dictation"
UNAVAILABLE = "Pasting isn't available right now"


def paste_last_original(ui) -> None:
    """Paste the last dictation's text from before AI cleanup at the caret.

    The undo for a cleanup that already went into the app: the original
    goes where the cleaned text went, and History marks the entry as
    original. Only the newest dictation counts, so pressing it twice never
    reaches back to an older one.
    """
    try:
        entry = history_manager.last_dictation()
    except Exception:
        logger.exception("Could not read the last dictation")
        ui.set_status("Couldn't read your last dictation")
        return
    if entry is None:
        ui.set_status(NOTHING_YET)
        return
    if not entry_version(entry):
        ui.set_status(NOT_EDITED)
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
