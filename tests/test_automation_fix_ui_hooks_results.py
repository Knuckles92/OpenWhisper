"""Settings → Hooks: a short result sits in a chip beside its button; an error
is words, so it wraps on a line of its own under the buttons."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QBoxLayout, QScrollArea, QVBoxLayout, QWidget

from services.automation import programs, webhooks
from services.settings import SettingsKey, SettingsManager
from tests.test_automation_fix_ui_hooks_answers import (
    ALPHA,
    PROGRAM_A,
    close,
    pump,
    record,
    resolved,
    wait_for,
)
from ui_qt.widgets import hooks_panel

LONG_ERROR = ("Couldn't connect: [WinError 10061] No connection could be made because the "
              "target machine actively refused it")


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    manager.save_all_settings({SettingsKey.AUTOMATION_WEBHOOKS: [ALPHA],
                               SettingsKey.AUTOMATION_PROGRAMS: [PROGRAM_A]})
    return manager


def in_column(section, width):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    host = QWidget()
    column = QVBoxLayout(host)
    column.addWidget(section)
    column.addStretch()
    scroll.setWidget(host)
    scroll.resize(width, 900)
    scroll.show()
    pump(8)
    return scroll


def test_a_long_error_wraps_under_the_buttons_and_the_button_keeps_its_size(store, monkeypatch):
    monkeypatch.setattr(webhooks, "send_test", lambda hook_id: resolved(
        record(None, 5000, ok=False, error=LONG_ERROR)))
    section = hooks_panel.WebhookSection(store)
    scroll = None
    try:
        section.open_editor("alpha00")
        scroll = in_column(section, 480)
        editor = section.editor
        editor.test_button.click()
        pump(8)
        line = editor.test_chip.error_line
        assert line.isVisible() and line.text() == LONG_ERROR
        assert line.property("tone") == "error" and line.wordWrap()
        assert line.height() >= line.heightForWidth(line.width()) - 1
        assert editor.test_chip.isHidden()
        # The row stays side by side: Send test isn't stretched across the card.
        row = editor.test_button.parentWidget()
        assert row.layout().direction() == QBoxLayout.Direction.LeftToRight
        button = editor.test_button
        assert button.width() <= max(button.sizeHint().width(), button.minimumWidth()) + 1

        # A short result goes back in the chip, and the error goes away.
        monkeypatch.setattr(webhooks, "send_test", lambda hook_id: resolved(record(404, 80, ok=False)))
        editor.test_button.click()
        pump()
        assert (editor.test_chip.text(), editor.test_chip.property("tone")) == ("404 · 80 ms", "error")
        assert not editor.test_chip.isHidden() and line.isHidden()
    finally:
        if scroll is not None:
            close(scroll)
        close(section)


def test_engine_and_secret_errors_use_the_line_too(store, monkeypatch):
    def refuse(hook_id):
        raise RuntimeError("Saved webhooks changed; try again.")

    def locked(hook_id):
        raise RuntimeError("Your system's credential store is locked. Unlock it and try again.")

    monkeypatch.setattr(webhooks, "send_test", refuse)
    monkeypatch.setattr(webhooks, "ensure_secret", locked)
    section = hooks_panel.WebhookSection(store)
    try:
        section.open_editor("alpha00")
        editor = section.editor
        editor.test_button.click()
        assert editor.test_chip.isHidden()
        assert editor.test_chip.error_line.text() == "Saved webhooks changed; try again."
        editor.copy_secret.click()
        wait_for(lambda: editor.copy_secret.isEnabled())
        assert editor.secret_chip.isHidden()
        assert editor.secret_chip.error_line.text().startswith("Your system's credential store")
        assert not editor.secret_chip.error_line.isHidden()
    finally:
        close(section)


def test_a_program_that_did_not_run_says_why_on_the_line(store, monkeypatch):
    monkeypatch.setattr(programs, "run_test", lambda program_id: resolved(programs.RunRecord(
        "evt_1", "2026-10-10T18:03:00Z", False, None, 12,
        "Couldn't start the program: the file was not found at its saved path")))
    section = hooks_panel.ProgramSection(store)
    try:
        section.open_editor("proga00")
        editor = section.editor
        editor.test_button.click()
        assert editor.test_chip.isHidden()
        assert editor.test_chip.error_line.text().startswith("Couldn't start the program")
        monkeypatch.setattr(programs, "run_test", lambda program_id: resolved(programs.RunRecord(
            "evt_2", "2026-10-10T18:04:00Z", False, 2, 80)))
        editor.test_button.click()
        assert (editor.test_chip.text(), editor.test_chip.property("tone")) == ("Exit 2 · 80 ms", "error")
        assert editor.test_chip.error_line.isHidden()
    finally:
        close(section)


def test_moving_to_another_item_clears_the_error_line(store, monkeypatch):
    monkeypatch.setattr(webhooks, "send_test", lambda hook_id: resolved(
        record(None, 5000, ok=False, error=LONG_ERROR)))
    section = hooks_panel.WebhookSection(store)
    try:
        section.open_editor("alpha00")
        section.editor.test_button.click()
        assert not section.editor.test_chip.error_line.isHidden()
        section.open_editor("")
        assert section.editor.test_chip.error_line.isHidden()
    finally:
        close(section)
