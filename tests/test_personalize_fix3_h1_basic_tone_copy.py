"""Basic's "Match tone to each app" row describes the tones the user really has."""

from services.app_styles import NEW_INSTALL_TONES, AppCategory, Tone
from services.settings import SettingsKey
from ui_qt.dialogs import settings_styles
from tests.test_personalize_s1_settings_page import (  # noqa: F401  (fixtures)
    CLEANUP_ON, _qapp, _restore_metadata, _windows_text, make_dialog,
)

BASIC = dict(CLEANUP_ON)


def _detail(page):
    switch = page.controls[SettingsKey.APP_STYLES_ENABLED]
    for row, control in page._rows:
        if control is switch:
            return row.itemAt(0).layout().itemAt(1).widget().text()
    raise AssertionError("styles row not found")


def test_an_upgraded_install_is_told_every_app_is_formal(make_dialog):  # noqa: F811
    dialog, store = make_dialog(BASIC)
    page = dialog.home
    assert page.controls[SettingsKey.APP_STYLES_ENABLED].isChecked()
    text = _detail(page)
    assert "Formal in every app" in text
    assert "relaxed" not in text and "casual" not in text.casefold()

    store.save_setting(SettingsKey.APP_STYLE_TONES, NEW_INSTALL_TONES)
    page.refresh()
    assert _detail(page) == "Formal for email and other apps, casual for chat."


def test_a_blocker_still_replaces_the_tones(make_dialog):  # noqa: F811
    dialog, _store = make_dialog({})
    assert _detail(dialog.home) == "Styles need AI cleanup, which is off."


def test_the_summary_names_each_tone_where_it_applies():
    def copy(**tones):
        return settings_styles.home_copy({SettingsKey.APP_STYLE_TONES: tones})

    assert copy(**NEW_INSTALL_TONES) == "Formal for email and other apps, casual for chat."
    assert copy(email=Tone.CASUAL, work=Tone.CASUAL, personal=Tone.CASUAL, other=Tone.CASUAL) == (
        "Casual in every app."
    )
    assert copy(**{**NEW_INSTALL_TONES, AppCategory.PERSONAL: Tone.VERY_CASUAL}) == (
        "Formal for email and other apps, casual for work messages, "
        "very casual for personal messages."
    )
    assert copy(**{**NEW_INSTALL_TONES, AppCategory.WORK: Tone.FORMAL}) == (
        "Formal for email, work messages and other apps, casual for personal messages."
    )
