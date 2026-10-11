"""Settings → Hooks: "Saved." shows only while the fields match what was saved."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from services.automation import catalog, models
from services.settings import SettingsManager
from tests.test_automation_fix_ui_hooks_answers import close
from ui_qt.widgets import hooks_panel


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    manager.save_all_settings({})
    return manager


def test_saved_goes_away_once_the_fields_change_again(store):
    section = hooks_panel.WebhookSection(store)
    try:
        section.open_editor("")
        editor = section.editor
        editor.name_edit.setText("Home")
        editor.url_edit.setText("https://example.com/in")
        assert section.save()
        assert editor.message.text() == "Saved." and not editor.message.isHidden()
        editor.url_edit.setText("https://example.com/other")
        assert editor.has_unsaved_changes()
        assert editor.message.isHidden()
    finally:
        close(section)


@pytest.mark.parametrize("change", ["name", "events"])
def test_webhook_fields_clear_it(store, change):
    section = hooks_panel.WebhookSection(store)
    try:
        section.open_editor("")
        editor = section.editor
        editor.name_edit.setText("Home")
        editor.url_edit.setText("https://example.com/in")
        assert section.save()
        if change == "name":
            editor.name_edit.setText("Home 2")
        else:
            editor.events.chips[catalog.ACTION_RAN].click()
        assert editor.message.isHidden()
    finally:
        close(section)


def test_program_fields_clear_it(store):
    section = hooks_panel.ProgramSection(store)
    try:
        section.open_editor("")
        editor = section.editor
        editor.name_edit.setText("Log")
        editor.program_edit.setText(os.path.abspath("log.exe"))
        for change in (lambda: editor.args_edit.setPlainText("--quiet"),
                       lambda: editor.timeout.setValue(45),
                       lambda: editor.program_edit.setText(os.path.abspath("other.exe"))):
            assert section.save(), editor.message.text()
            assert editor.message.text() == "Saved."
            change()
            assert editor.message.isHidden()
    finally:
        close(section)


def test_filter_fields_clear_it(store):
    section = hooks_panel.FilterSection(store)
    try:
        section.open_editor("")
        editor = section.editor
        editor.name_edit.setText("Names")
        editor.url_edit.setText("http://127.0.0.1:5005/fix")
        editor.program_edit.setText(os.path.abspath("fix.exe"))
        for change in (lambda: editor.stage_bar.setCurrentIndex(0),
                       lambda: editor.timeout.setValue(900),
                       lambda: editor.kind_bar.setCurrentIndex(1)):
            assert section.save(), editor.message.text()
            assert editor.message.text() == "Saved."
            change()
            assert editor.message.isHidden()
    finally:
        close(section)


def test_an_edit_undone_does_not_bring_it_back_but_a_save_does(store):
    section = hooks_panel.WebhookSection(store)
    try:
        section.open_editor("")
        editor = section.editor
        editor.name_edit.setText("Home")
        editor.url_edit.setText("https://example.com/in")
        assert section.save()
        editor.url_edit.setText("https://example.com/in2")
        editor.url_edit.setText("https://example.com/in")
        assert editor.message.isHidden()
        assert section.save()
        assert editor.message.text() == "Saved."
    finally:
        close(section)


def test_an_error_stays_until_the_next_save(store):
    section = hooks_panel.WebhookSection(store)
    try:
        section.open_editor("")
        editor = section.editor
        editor.name_edit.setText("Home")
        assert not section.save()
        message = editor.message.text()
        editor.events.chips[catalog.ACTION_RAN].click()
        assert editor.message.text() == message and not editor.message.isHidden()
        assert models.load_webhooks(store.load_all_settings()) == []
    finally:
        close(section)
