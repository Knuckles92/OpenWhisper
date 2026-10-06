"""History actions reached from outside the history sidebar (tray, shortcuts)."""


def paste_last_original(ui) -> None:
    """Paste the last dictation's text from before AI cleanup at the caret."""
    ui.set_status("Nothing to paste yet")
