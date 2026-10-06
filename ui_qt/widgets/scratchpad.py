"""The floating Scratchpad: a notepad that dictation types into while it has focus."""


def toggle(ui) -> None:
    """Show the Scratchpad, or hide it when it is already showing."""
    ui.set_status("The Scratchpad isn't available yet")


def insert(ui, text: str) -> bool:
    """Insert a finished dictation into the focused Scratchpad.

    Returns:
        True only when the Scratchpad took the text, so the caller skips the
        paste; False whenever the text should go to the app as usual.
    """
    return False
