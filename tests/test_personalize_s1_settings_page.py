"""Settings → Apps & styles: app awareness, text near the cursor, styles."""
import os
import tempfile
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLabel, QLineEdit

from services.app_styles import NEW_INSTALL_TONES
from services.focus_context import AppIdentity
from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs import settings_styles
from ui_qt.dialogs.settings_destinations import BASIC_DICTATION, OVERVIEW, STYLES
from ui_qt.widgets.setting_tile import TileBase

CLEANUP_ON = {SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True}


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_metadata():
    controls = dict(settings_metadata.CONTROL_DESTINATIONS)
    fields = dict(settings_metadata.PAGE_SEARCH_FIELDS)
    yield
    settings_metadata.CONTROL_DESTINATIONS.clear()
    settings_metadata.CONTROL_DESTINATIONS.update(controls)
    settings_metadata.PAGE_SEARCH_FIELDS.clear()
    settings_metadata.PAGE_SEARCH_FIELDS.update(fields)


@pytest.fixture(autouse=True)
def _windows_text(monkeypatch):
    # Text reading is Windows-only; pin it so the page reads the same on
    # every test machine. Tests that need another platform override it.
    monkeypatch.setattr(settings_styles, "text_reading_supported", lambda: True)


@pytest.fixture
def make_dialog():
    stacks = []
    dialogs = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED, **(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(
            get_loaded_model=lambda: None, background_cache_scan=False)
        dialog.on_settings_changed = MagicMock()
        dialogs.append(dialog)
        return dialog, store

    yield build
    for dialog in dialogs:
        dialog.close()
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def _page(dialog):
    dialog.select_destination(STYLES)
    return dialog.apps_styles_page


def _click(widget):
    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)


# --- rail value ----------------------------------------------------------------------


@pytest.mark.parametrize("settings,value", [
    ({}, "Needs AI cleanup"),
    (CLEANUP_ON, "On · All formal"),
    ({**CLEANUP_ON, SettingsKey.APP_STYLE_TONES: NEW_INSTALL_TONES}, "On · 2 casual"),
    ({**CLEANUP_ON, SettingsKey.APP_STYLES_ENABLED: False}, "Off"),
    ({**CLEANUP_ON, SettingsKey.APP_CONTEXT_ENABLED: False}, "Off"),
])
def test_rail_value(settings, value):
    assert settings_styles.rail_value(settings) == value


def test_apps_summary_follows_the_users_choices():
    overrides = (("Notion", "work"), ("Gmail", "personal"))
    assert settings_styles.apps_summary("work", overrides) == "Notion, Slack, Teams, Google Chat and more."
    assert settings_styles.apps_summary("email", overrides) == "Outlook, Mail, Thunderbird and more."
    assert settings_styles.apps_summary("other", overrides) == (
        "Everything else, like Word, Google Docs and VS Code.")
    assert settings_styles.apps_summary("other", ()) == (
        "Everything else, like Word, Notion, Google Docs and VS Code.")


# --- app awareness -------------------------------------------------------------------


def test_defaults_show_awareness_on_and_reading_off(make_dialog):
    dialog, _store = make_dialog()
    _page(dialog)

    assert dialog.page_title.text() == "Apps & styles"
    assert dialog.app_context_check.isChecked()
    assert dialog.app_context_tile.property("checked") is True
    assert not dialog.read_text_check.isChecked()
    assert dialog.read_text_tile.isEnabled()
    # Exclusions also keep Command Mode out, so they stay editable.
    assert dialog.excluded_apps_tile.isEnabled()


def test_exclusions_need_app_awareness(make_dialog):
    dialog, _store = make_dialog({SettingsKey.APP_CONTEXT_ENABLED: False})
    _page(dialog)

    assert not dialog.excluded_apps_tile.isEnabled()


def test_switches_save_and_tell_the_app(make_dialog):
    dialog, store = make_dialog()
    page = _page(dialog)

    _click(dialog.read_text_tile)
    assert store.get(SettingsKey.APP_CONTEXT_READ_TEXT) is True
    assert dialog.excluded_apps_tile.isEnabled()
    dialog.on_settings_changed.assert_called_with("styles")

    dialog.app_context_check.setChecked(False)
    assert store.get(SettingsKey.APP_CONTEXT_ENABLED) is False
    assert not dialog.read_text_tile.isEnabled()
    assert not dialog.excluded_apps_tile.isEnabled()
    assert "first" in page.privacy.text()


def test_privacy_note_names_where_the_text_goes(make_dialog):
    dialog, _store = make_dialog({**CLEANUP_ON,
                                  SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter",
                                  SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "openrouter/free"})
    page = _page(dialog)
    assert page.privacy.text().startswith("Sent only to your AI cleanup provider (OpenRouter)")
    assert "Never saved" in page.privacy.text()

    local, _store = make_dialog({**CLEANUP_ON,
                                 SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "ollama",
                                 SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "llama3.2"})
    assert "on this computer" in _page(local).privacy.text()

    off, _store = make_dialog()
    assert _page(off).privacy.text().startswith("AI cleanup is off")


def test_other_platforms_say_reading_text_is_windows_only(make_dialog, monkeypatch):
    monkeypatch.setattr(settings_styles, "text_reading_supported", lambda: False)
    dialog, _store = make_dialog()
    page = _page(dialog)

    assert not dialog.read_text_tile.isEnabled()
    assert page.privacy.text().startswith("Works on Windows for now")


def test_excluded_apps_add_and_remove(make_dialog):
    dialog, store = make_dialog({SettingsKey.APP_CONTEXT_READ_TEXT: True})
    page = _page(dialog)
    assert page.excluded_rows.rows == []

    for name in ("1Password", "  1password ", "Bitwarden"):
        page.excluded_picker.setEditText(name)
        _click(page.excluded_add)
    assert store.get(SettingsKey.APP_CONTEXT_EXCLUDED_APPS) == ["1Password", "Bitwarden"]
    assert len(page.excluded_rows.rows) == 2
    assert page.excluded_picker.currentText() == ""

    page.excluded_picker.setEditText("")
    _click(page.excluded_add)
    assert store.get(SettingsKey.APP_CONTEXT_EXCLUDED_APPS) == ["1Password", "Bitwarden"]

    remove = page.excluded_rows.rows[0].findChild(QAbstractButton)
    assert remove.accessibleName() == "Remove 1Password"
    _click(remove)
    assert store.get(SettingsKey.APP_CONTEXT_EXCLUDED_APPS) == ["Bitwarden"]


def test_pickers_offer_this_sessions_apps_first(make_dialog, monkeypatch):
    seen = (AppIdentity("chrome.exe", "Chrome", title_hint="Gmail"),
            AppIdentity("foo_tool.exe", "Foo Tool"))
    monkeypatch.setattr(settings_styles, "recent_apps", lambda: seen)
    dialog, _store = make_dialog()
    page = _page(dialog)

    names = [page.override_picker.itemText(index) for index in range(page.override_picker.count())]
    assert names[:2] == ["Gmail", "Foo Tool"]
    assert "Slack" in names and len(names) == len({name.casefold() for name in names})


# --- styles --------------------------------------------------------------------------


def test_cleanup_off_shows_the_gate_and_locks_styles(make_dialog):
    dialog, store = make_dialog()
    page = _page(dialog)

    assert not dialog.styles_cleanup_gate_tile.isHidden()
    assert dialog.styles_cleanup_gate_tile.title_label.text() == "AI cleanup is off"
    assert dialog.styles_gate_tile.isHidden()
    assert not dialog.app_styles_tile.isEnabled()
    assert not any(card.tile.isEnabled() for card in page.cards.values())
    assert not dialog.style_overrides_tile.isEnabled()

    dialog.turn_on_cleanup()
    assert store.get(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED) is True
    assert dialog.rail.current_key() == STYLES


def test_awareness_off_gate_turns_it_back_on(make_dialog):
    dialog, store = make_dialog({**CLEANUP_ON, SettingsKey.APP_CONTEXT_ENABLED: False})
    page = _page(dialog)

    assert dialog.styles_gate_tile.title_label.text() == "Styles need app awareness"
    assert page.gate_button.text() == "Turn on"
    _click(page.gate_button)
    assert store.get(SettingsKey.APP_CONTEXT_ENABLED) is True
    assert dialog.styles_gate_tile.isHidden()
    assert dialog.app_styles_tile.isEnabled()


def test_picking_a_tone_saves_every_category_and_shows_an_example(make_dialog):
    dialog, store = make_dialog(CLEANUP_ON)
    page = _page(dialog)
    work = page.cards["work"]
    assert dialog.styles_gate_tile.isHidden() and work.tile.isEnabled()
    assert work.example.text() == settings_styles.EXAMPLES["work"]["formal"]
    assert work.tile.description_label.text().startswith("Slack, Teams")

    _click(work.bar.buttons[2])

    assert store.get(SettingsKey.APP_STYLE_TONES) == {
        "email": "formal", "work": "very_casual", "personal": "formal", "other": "formal",
    }
    assert work.example.text() == settings_styles.EXAMPLES["work"]["very_casual"]
    assert dialog.rail.value(STYLES) == "On · 1 casual"
    dialog.on_settings_changed.assert_called_with("styles")


def test_loading_tones_never_saves(make_dialog):
    dialog, store = make_dialog({**CLEANUP_ON, SettingsKey.APP_STYLE_TONES: NEW_INSTALL_TONES})
    page = _page(dialog)
    assert page.cards["personal"].bar.currentIndex() == 1
    dialog.refresh()
    assert store.get(SettingsKey.APP_STYLE_TONES) == NEW_INSTALL_TONES
    dialog.on_settings_changed.assert_not_called()


def test_styles_off_dims_the_cards(make_dialog):
    dialog, store = make_dialog(CLEANUP_ON)
    page = _page(dialog)

    _click(dialog.app_styles_tile)

    assert store.get(SettingsKey.APP_STYLES_ENABLED) is False
    assert not any(card.tile.isEnabled() for card in page.cards.values())
    assert not dialog.style_overrides_tile.isEnabled()
    assert dialog.rail.value(STYLES) == "Off"


def test_overrides_add_replace_and_remove(make_dialog):
    dialog, store = make_dialog(CLEANUP_ON)
    page = _page(dialog)

    page.override_picker.setEditText("Notion")
    page.override_category.setCurrentIndex(page.override_category.findData("work"))
    _click(page.override_add)

    assert store.get(SettingsKey.APP_STYLE_OVERRIDES) == [{"match": "Notion", "category": "work"}]
    labels = [row.findChild(QLabel, "appStylesRowLabel").text() for row in page.override_rows.rows]
    assert labels == ["Notion  →  Work messages"]
    assert page.cards["work"].tile.description_label.text().startswith("Notion, Slack")
    assert "Notion" not in page.cards["other"].tile.description_label.text()

    page.override_picker.setEditText("notion")
    page.override_category.setCurrentIndex(page.override_category.findData("personal"))
    _click(page.override_add)
    assert store.get(SettingsKey.APP_STYLE_OVERRIDES) == [{"match": "notion", "category": "personal"}]

    _click(page.override_rows.rows[0].findChild(QAbstractButton))
    assert store.get(SettingsKey.APP_STYLE_OVERRIDES) == []
    assert page.override_rows.rows == []


def test_search_finds_the_page_before_and_after_it_is_built(make_dialog):
    dialog, _store = make_dialog()
    titles = {entry.title for entry in dialog._search_index() if entry.destination == STYLES}
    assert {"Read text near the cursor", "Never read from", "Work messages"} <= titles

    _page(dialog)
    built = {entry.title for entry in dialog._search_index() if entry.destination == STYLES}
    assert {"Read text near the cursor", "Never read from", "Work messages"} <= built
    for attr, title, _keywords in settings_styles.SEARCH_FIELDS:
        assert getattr(dialog, attr).title_label.text() == title


# --- Overview and Basic ----------------------------------------------------------------


@pytest.mark.parametrize("values,where,item", [
    ({}, "local", "Text near the cursor (off)"),
    ({SettingsKey.APP_CONTEXT_READ_TEXT: True, SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter",
      SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "openrouter/free"},
     "cloud", "Text near the cursor (with AI cleanup)"),
    ({SettingsKey.APP_CONTEXT_READ_TEXT: True, SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "ollama",
      SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "llama3.2"}, "local", "Text near the cursor"),
    ({SettingsKey.APP_CONTEXT_READ_TEXT: True, SettingsKey.APP_CONTEXT_ENABLED: False},
     "local", "Text near the cursor (off)"),
])
def test_overview_privacy_strip(make_dialog, values, where, item):
    dialog, _store = make_dialog(values)
    dialog.select_destination(OVERVIEW)
    local = dialog.overview.local_list.text().split(" · ")
    cloud = dialog.overview.cloud_list.text().split(" · ")
    assert item in (local if where == "local" else cloud)
    assert not any(entry.startswith("Text near") for entry in (cloud if where == "local" else local))


def test_basic_row_matches_tone_and_explains_when_it_cannot(make_dialog):
    dialog, store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC})
    page = dialog._basic_pages[BASIC_DICTATION]
    switch = page.controls[SettingsKey.APP_STYLES_ENABLED]
    assert switch.isChecked() and not switch.isEnabled()
    assert "AI cleanup" in switch.toolTip()
    assert STYLES not in dialog._built_pages

    store.save_setting(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, True)
    dialog._refresh_rail_values()
    assert switch.isEnabled() and switch.toolTip() == ""

    switch.click()
    assert store.get(SettingsKey.APP_STYLES_ENABLED) is False
    assert not dialog.app_styles_check.isChecked()


# --- layout ----------------------------------------------------------------------------


@pytest.mark.parametrize("ui_mode,width", [("classic", 940), ("omarchy", 720), ("omarchy", 560)])
def test_page_fits_narrow_windows_at_large_fonts(make_dialog, monkeypatch, ui_mode, width):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
    try:
        apply_ui_font_scale(130, app=app, theme_manager=ThemeManager())
        dialog, _store = make_dialog({
            **CLEANUP_ON,
            SettingsKey.APP_CONTEXT_READ_TEXT: True,
            SettingsKey.APP_CONTEXT_EXCLUDED_APPS: ["A password manager with a long name"],
            SettingsKey.APP_STYLE_OVERRIDES: [{"match": "Notion", "category": "personal"}],
        })
        dialog.show()
        dialog.resize(width, 600)
        dialog.select_destination(STYLES)
        for _ in range(10):
            app.processEvents()
        assert dialog.width() == width
        page = dialog._pages[STYLES]
        controls = (page.findChildren(QAbstractButton) + page.findChildren(QComboBox)
                    + page.findChildren(QLineEdit))
        for control in controls:
            if control.isVisible():
                assert control.mapTo(page, control.rect().topLeft()).x() >= 0
                assert control.mapTo(page, control.rect().bottomRight()).x() < page.width()
        for label in page.findChildren(QLabel):
            if label.isVisible() and label.wordWrap():
                assert label.height() >= label.heightForWidth(label.width())
        for tile in page.findChildren(TileBase):
            if tile.isVisible():
                assert page.rect().contains(tile.geometry())
        scroll = dialog._page_scrolls[STYLES]
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        app.processEvents()
        assert page.mapTo(scroll.viewport(), page.rect().bottomRight()).y() <= scroll.viewport().height()
    finally:
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)
