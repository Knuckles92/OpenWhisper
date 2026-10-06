"""The dictation language menu shared by the tray and the desktop bar.

Also the one place a language switch is announced, so the tray, the
shortcut, the overlay chip and the Omarchy bar all say the same thing.
"""

import logging

from services.settings import settings_manager
from ui_qt.dialogs.settings_destinations import VOICE_MODEL

logger = logging.getLogger(__name__)

_FAILED = "Couldn't switch the dictation language"


def populate(menu, ui) -> None:
    """Refill ``menu`` with the languages you dictate in, just before it opens."""
    menu.clear()
    try:
        from services import dictation_language as languages

        settings = settings_manager.load_all_settings()
        choices = languages.language_choices(settings)
        current = languages.current_language(settings)
        reason = languages.single_language_reason(settings)
    except Exception:
        logger.exception("Couldn't list the dictation languages")
        choices, current, reason = [], "", ""
    if len(choices) > 1:
        for code in choices:
            action = menu.addAction(languages.label(code))
            action.setCheckable(True)
            action.setChecked(code == current)
            action.triggered.connect(lambda _checked=False, code=code: select(ui, code))
    else:
        menu.addAction(reason or "No other languages").setEnabled(False)
    menu.addSeparator()
    menu.addAction("Choose languages…").triggered.connect(
        lambda _checked=False: ui.open_settings_destination(VOICE_MODEL)
    )


def select(ui, code: str) -> None:
    """Make ``code`` the language the next dictation uses (the tray's choice)."""
    _switch(ui, lambda languages, settings: languages.set_active(settings, code), notice=False)


def cycle(ui) -> None:
    """Switch to the next language you dictate in (the shortcut, chip and bar)."""
    _switch(ui, lambda languages, settings: languages.cycle(settings), notice=True)


def _switch(ui, change, *, notice: bool) -> None:
    try:
        from services import dictation_language as languages

        settings_manager.mutate_settings(lambda settings: change(languages, settings))
        settings = settings_manager.load_all_settings()
        choices = languages.language_choices(settings)
        code = languages.current_language(settings)
        reason = languages.single_language_reason(settings)
        name = languages.label(code)
    except Exception:
        logger.exception(_FAILED)
        ui.set_status(_FAILED)
        return
    if len(choices) < 2:
        ui.set_status(reason or "Add another language in Settings → Voice model to switch")
        return
    overlay = ui.overlay
    overlay.set_language(code, choices)
    # With nothing recording the overlay is hidden; a short notice near the
    # pointer confirms a shortcut press that would otherwise show nothing.
    if notice and (not overlay.isVisible() or overlay.current_state == overlay.STATE_LANGUAGE):
        overlay.show_language_notice()
    ui.set_status(f"Dictation language: {name}")
