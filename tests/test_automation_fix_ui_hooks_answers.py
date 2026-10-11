"""Settings → Hooks: one editor serves every item, so an answer that arrives
after it moved to another item (a test result, a secret) must be dropped."""

import os
import threading
import time
from concurrent.futures import Future

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMessageBox

from services.automation import catalog, programs, webhooks
from services.settings import SettingsKey, SettingsManager
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


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    manager.save_all_settings({})
    return manager


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


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        QTest.qWait(10)


def record(status=200, elapsed=120, ok=True, error="", attempt=1):
    return webhooks.DeliveryRecord("evt_1", "webhook.test", "2026-10-10T18:02:11.120Z", ok,
                                   status, elapsed, error, attempt)


def resolved(value):
    future = Future()
    future.set_result(value)
    return future


def close(widget):
    widget.close()
    widget.deleteLater()
    pump()


def workers_named(name, before):
    return [t for t in threading.enumerate() if t not in before and t.name == name]


def test_a_late_test_result_and_secret_for_one_webhook_stay_off_the_next(
    store, clipboard, answer_yes, monkeypatch
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
    section = hooks_panel.WebhookSection(store)
    try:
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
    finally:
        close(section)


def test_a_late_run_result_for_one_program_stays_off_the_next(store, monkeypatch):
    store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_PROGRAMS,
                                                  [PROGRAM_A, PROGRAM_B]))
    pending = {}

    def run_test(program_id):
        pending[program_id] = Future()
        return pending[program_id]

    monkeypatch.setattr(programs, "run_test", run_test)
    section = hooks_panel.ProgramSection(store)
    try:
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
    finally:
        close(section)


def test_closing_the_editor_forgets_its_requests(store, monkeypatch):
    store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [ALPHA]))
    pending = Future()
    monkeypatch.setattr(webhooks, "send_test", lambda hook_id: pending)
    section = hooks_panel.WebhookSection(store)
    try:
        section.open_editor("alpha00")
        section.editor.test_button.click()
        section.cancel()
        section.open_editor("alpha00")
        assert section.editor.test_button.isEnabled()
        pending.set_result(record(500, 30, ok=False))
        pump()
        assert section.editor.test_chip.isHidden()
    finally:
        close(section)
