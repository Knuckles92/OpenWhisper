"""The dictation language menu shared by the tray and the desktop bar."""


def populate(menu, ui) -> None:
    """Refill ``menu`` with the languages you dictate in, just before it opens."""
    menu.clear()
    menu.addAction("No other languages").setEnabled(False)
