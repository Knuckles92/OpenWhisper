"""Settings → Hooks: one editor serves every item, so an answer that arrives
after it moved to another item (a test result, a secret) must be dropped."""

import os
import threading
from concurrent.futures import Future

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from services.automation import catalog, programs, webhooks
from services.settings import SettingsKey
from tests import test_automation_ui_hooks_page as hooks_page
from tests.test_automation_ui_hooks_page import record, wait_for
from ui_qt.widgets import hooks_panel

ALPHA = {"id": "alpha00", "name": "Alpha", "url": "https://a.example/hook",
         "events": [catalog.TRANSCRIPTION_COMPLETED], "enabled": True}
BETA = {"id": "beta000", "name": "Beta", "url": "https://b.example/hook",
        "events": [catalog.TRANSCRIPTION_COMPLETED], "enabled": True}
GAMMA = {"id": "gamma00", "name": "Gamma", "url": "https://c.example/hook",
         "events": [catalog.TRANSCRIPTION_COMPLETED], "enabled": True}
PROGRAM_A = {"id": "proga00", "name": "Journal", "program": os.path.abspath("journal.exe"),
             "args": [], "events": [catalog.HISTORY_SAVED], "enabled": True, "timeout_s": 30}
PROGRAM_B = dict(PROGRAM_A, id="progb00", name="Logger")


store, open_section = hooks_page.store, hooks_page.open_section


@pytest.fixture
def clipboard(monkeypatch):
    """What the page copies; the system clipboard is never touched."""
    copied = []

    class Clipboard:
        def setText(self, text, *_args):
            copied.append(text)

    board = Clipboard()
    monkeypatch.setattr(hooks_panel.QApplication, "clipboard", staticmethod(lambda: board))
    return copied


@pytest.fixture
def answer_yes(monkeypatch):
    monkeypatch.setattr(hooks_panel.QMessageBox, "question",
                        lambda *args, **kwargs: QMessageBox.StandardButton.Yes)


def pump(cycles=6):
    for _ in range(cycles):
        QApplication.processEvents()


def close(widget):
    widget.close()
    widget.deleteLater()
    pump()


def workers_named(name, before):
    return [t for t in threading.enumerate() if t not in before and t.name == name]


def test_a_late_test_result_and_secret_for_one_webhook_stay_off_the_next(
    store, open_section, clipboard, answer_yes, monkeypatch
):
    store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [ALPHA, BETA]))
    pending = {}

    def send_test(hook_id):
        pending[hook_id] = Future()
        return pending[hook_id]

    release = threading.Event()

    def slow_secret(hook_id):
        assert release.wait(5)
        return f"whsec_secret_of_{hook_id}"

    monkeypatch.setattr(webhooks, "send_test", send_test)
    monkeypatch.setattr(webhooks, "ensure_secret", slow_secret)
    monkeypatch.setattr(webhooks, "rotate_secret", slow_secret)
    before = set(threading.enumerate())
    section, _changes = open_section("webhook")
    section.open_editor("alpha00")
    editor = section.editor
    editor.test_button.click()
    editor.copy_secret.click()
    editor.rotate_secret.click()
    assert not editor.test_button.isEnabled()
    assert not editor.copy_secret.isEnabled() and not editor.rotate_secret.isEnabled()

    section.open_editor("beta000")
    assert editor.item_id() == "beta000"
    pending["alpha00"].set_result(record(200, 120))
    release.set()
    for worker in (workers_named("settings-webhook-secret", before)
                   + workers_named("settings-webhook-rotate", before)):
        worker.join(5)
    pump()
    # Alpha's secret is never copied while Beta is shown, and Beta says nothing of it.
    assert clipboard == []
    assert editor.test_chip.isHidden() and editor.secret_chip.isHidden()
    # Beta starts fresh: nothing of Alpha's holds its buttons.
    assert editor.test_button.isEnabled()
    assert editor.copy_secret.isEnabled() and editor.rotate_secret.isEnabled()

    # Beta's own requests still answer Beta.
    monkeypatch.setattr(webhooks, "ensure_secret", lambda hook_id: f"whsec_secret_of_{hook_id}")
    editor.copy_secret.click()
    wait_for(lambda: editor.secret_chip.text() == "Copied")
    assert clipboard == ["whsec_secret_of_beta000"]
    editor.test_button.click()
    pending["beta000"].set_result(record(201, 80))
    wait_for(lambda: editor.test_chip.text() == "201 · 80 ms")


def test_a_late_run_result_for_one_program_stays_off_the_next(store, open_section, monkeypatch):
    store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_PROGRAMS,
                                                  [PROGRAM_A, PROGRAM_B]))
    pending = {}

    def run_test(program_id):
        pending[program_id] = Future()
        return pending[program_id]

    monkeypatch.setattr(programs, "run_test", run_test)
    section, _changes = open_section("program")
    section.open_editor("proga00")
    editor = section.editor
    editor.test_button.click()
    assert editor.test_chip.text() == "Running…"
    section.open_editor("progb00")
    pending["proga00"].set_result(programs.RunRecord("evt_1", "2026-10-10T18:03:00Z",
                                                     False, 3, 90))
    pump()
    assert editor.test_chip.isHidden()
    assert editor.test_button.isEnabled()


def test_closing_the_editor_forgets_its_requests(store, open_section, monkeypatch):
    store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [ALPHA]))
    pending = Future()
    monkeypatch.setattr(webhooks, "send_test", lambda hook_id: pending)
    section, _changes = open_section("webhook")
    section.open_editor("alpha00")
    section.editor.test_button.click()
    section.cancel()
    section.open_editor("alpha00")
    assert section.editor.test_button.isEnabled()
    pending.set_result(record(500, 30, ok=False))
    pump()
    assert section.editor.test_chip.isHidden()
