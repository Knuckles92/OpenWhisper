"""Settings → Automate → Hooks: webhooks, programs and text filters."""

import os
import tempfile
import time
from concurrent.futures import Future
from contextlib import ExitStack
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from services.automation import catalog, filters, models, programs, webhooks
from services.settings import SettingsKey, SettingsManager
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_hooks
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import HOOKS, MCP
from ui_qt.widgets import hooks_panel
from ui_qt.widgets.engine_field import EngineStatus

HOOK = {"id": "hook01", "name": "Home", "url": "https://example.com/in",
        "events": [catalog.TRANSCRIPTION_COMPLETED], "enabled": True}
PROGRAM = {"id": "prog01", "name": "Journal", "program": os.path.abspath("journal.exe"),
           "args": ["--append"], "events": [catalog.HISTORY_SAVED], "enabled": True,
           "timeout_s": 30}
FILTER = {"id": "filt01", "name": "Names", "kind": "http", "stage": "after_cleanup",
          "url": "http://127.0.0.1:5005/fix", "timeout_ms": 1500, "enabled": False}


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _restore_metadata():
    # Registering a page module writes its routes into these shared maps.
    controls = dict(settings_metadata.CONTROL_DESTINATIONS)
    fields = dict(settings_metadata.PAGE_SEARCH_FIELDS)
    yield
    settings_metadata.CONTROL_DESTINATIONS.clear()
    settings_metadata.CONTROL_DESTINATIONS.update(controls)
    settings_metadata.PAGE_SEARCH_FIELDS.clear()
    settings_metadata.PAGE_SEARCH_FIELDS.update(fields)


@pytest.fixture
def answers(monkeypatch):
    """QMessageBox.question answers, in order; records the questions asked."""
    queue, asked = [], []

    def question(_parent, title, text, *args, **kwargs):
        asked.append((title, text))
        return queue.pop(0) if queue else QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(hooks_panel.QMessageBox, "question", question)
    return queue, asked


@pytest.fixture
def store(tmp_path):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    manager.save_all_settings({})
    return manager


@pytest.fixture
def make_dialog():
    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({**(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(background_cache_scan=False)
        changes = []
        dialog.on_settings_changed = changes.append
        return dialog, store, changes

    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def pump(cycles=4):
    for _ in range(cycles):
        QApplication.processEvents()


def wait_for(condition, timeout=5.0):
    """Let worker threads answer through the relay."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        QTest.qWait(10)


def resolved(value=None, error=None):
    future = Future()
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(value)
    return future


def record(status=200, elapsed=120, ok=True, error="", event_type="webhook.test", attempt=1):
    return webhooks.DeliveryRecord("evt_1", event_type, "2026-10-10T18:02:11.120Z", ok, status,
                                   elapsed, error, attempt)


def section_of(kind, store):
    section = {"webhook": hooks_panel.WebhookSection, "program": hooks_panel.ProgramSection,
               "filter": hooks_panel.FilterSection}[kind](store)
    changes = []
    section.changed.connect(lambda: changes.append(kind))
    return section, changes


def close(widget):
    widget.close()
    widget.deleteLater()
    pump()


# ---- the page module ---------------------------------------------------------

class TestPageModule:
    def test_page_builds_lazily_with_a_tile_per_search_field(self, make_dialog):
        dialog, _store, _changes = make_dialog()
        try:
            assert HOOKS not in dialog._built_pages
            assert dialog.rail.value(HOOKS) == "None yet"
            dialog.select_destination(HOOKS)
            assert HOOKS in dialog._built_pages
            assert dialog.page_title.text() == settings_hooks.TITLE
            page = dialog._pages[HOOKS]
            for attr, title, _description in settings_hooks.SEARCH_FIELDS:
                tile = dialog.__dict__[attr]
                assert page.isAncestorOf(tile)
                assert tile.title_label.text() == title
            assert set(settings_hooks.CONTROL_ATTRS) <= set(dialog.__dict__)
            for attr in settings_hooks.CONTROL_ATTRS:
                assert settings_metadata.CONTROL_DESTINATIONS[attr] == HOOKS
        finally:
            dialog.close()

    def test_a_section_attribute_builds_the_page(self, make_dialog):
        dialog, _store, _changes = make_dialog()
        try:
            section = dialog.hooks_programs
            assert HOOKS in dialog._built_pages
            assert dialog._pages[HOOKS].isAncestorOf(section)
        finally:
            dialog.close()

    def test_rail_value_counts_hooks_and_those_switched_off(self):
        assert settings_hooks.rail_value({}) == "None yet"
        assert settings_hooks.rail_value({SettingsKey.AUTOMATION_WEBHOOKS: [HOOK]}) == "1 hook"
        assert settings_hooks.rail_value({
            SettingsKey.AUTOMATION_WEBHOOKS: [HOOK],
            SettingsKey.AUTOMATION_PROGRAMS: [PROGRAM],
            SettingsKey.AUTOMATION_FILTERS: [FILTER],
        }) == "3 hooks · 2 on"

    def test_saves_notify_the_app_and_redraw_the_rail(self, make_dialog, answers):
        dialog, store, changes = make_dialog()
        try:
            section = dialog.hooks_webhooks
            section.open_editor("")
            editor = section.editor
            editor.name_edit.setText("Home")
            editor.url_edit.setText("https://example.com/in")
            assert section.save()
            assert changes == ["webhooks"]
            assert dialog.rail.value(HOOKS) == "1 hook"
            dialog.hooks_programs.open_editor("")
            dialog.hooks_programs.editor.name_edit.setText("Journal")
            dialog.hooks_programs.editor.program_edit.setText(os.path.abspath("journal.exe"))
            assert dialog.hooks_programs.save()
            filters_section = dialog.hooks_filters
            filters_section.open_editor("")
            filters_section.editor.name_edit.setText("Names")
            filters_section.editor.url_edit.setText("http://127.0.0.1:5005/fix")
            assert filters_section.save()
            assert changes == ["webhooks", "programs", "filters"]
            assert len(models.load_filters(store.load_all_settings())) == 1
        finally:
            dialog.close()

    def test_load_and_refresh_reread_saved_hooks(self, make_dialog):
        dialog, store, _changes = make_dialog({SettingsKey.AUTOMATION_WEBHOOKS: [HOOK]})
        try:
            assert [h.id for h in dialog.hooks_webhooks.items()] == ["hook01"]
            store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_PROGRAMS, [PROGRAM]))
            settings_hooks.refresh(dialog)
            assert [p.id for p in dialog.hooks_programs.items()] == ["prog01"]
        finally:
            dialog.close()

    def test_leaving_the_page_saves_a_valid_draft_and_asks_about_an_invalid_one(
        self, make_dialog, answers
    ):
        queue, asked = answers
        dialog, store, _changes = make_dialog()
        try:
            section = dialog.hooks_webhooks
            section.open_editor("")
            section.editor.name_edit.setText("Draft")
            section.editor.url_edit.setText("https://example.com/hook")
            assert settings_hooks.save_drafts(dialog, ask=True)
            assert [h.name for h in models.load_webhooks(store.load_all_settings())] == ["Draft"]

            section.open_editor("")
            section.editor.name_edit.setText("No address")
            queue.append(QMessageBox.StandardButton.Cancel)
            assert not settings_hooks.save_drafts(dialog, ask=True)
            assert "Enter the address to send to." in asked[-1][1]
            assert section.has_unsaved_changes()
            queue.append(QMessageBox.StandardButton.Discard)
            assert settings_hooks.save_drafts(dialog, ask=True)
            assert section.editor.isHidden()
            assert len(models.load_webhooks(store.load_all_settings())) == 1
        finally:
            dialog.close()

    def test_open_api_link_goes_to_api_and_mcp(self, make_dialog):
        dialog, _store, _changes = make_dialog()
        try:
            dialog.select_destination(HOOKS)
            dialog.hooks_filters.leases.open_api.click()
            assert dialog.rail.current_key() == MCP
        finally:
            dialog.close()


# ---- webhooks ----------------------------------------------------------------

class TestWebhooks:
    def test_add_edit_and_delete_persist_and_forget_the_secret(self, store, answers, monkeypatch):
        queue, _asked = answers
        forgotten = []
        monkeypatch.setattr(webhooks, "delete_secret", forgotten.append)
        section, changes = section_of("webhook", store)
        try:
            assert not section.empty_state.isHidden()
            assert section.count_label.isHidden()
            section.add_button.click()
            editor = section.editor
            assert not editor.isHidden() and section.empty_state.isHidden()
            assert editor.delete_button.isHidden()
            assert editor.extras.isHidden()
            assert editor.events.events() == (catalog.TRANSCRIPTION_COMPLETED,)
            editor.name_edit.setText("  Home  ")
            editor.url_edit.setText("https://example.com/in")
            editor.events.chips[catalog.HISTORY_SAVED].click()
            editor.save_button.click()
            [hook] = models.load_webhooks(store.load_all_settings())
            assert (hook.name, hook.url, hook.enabled) == ("Home", "https://example.com/in", True)
            assert hook.events == (catalog.TRANSCRIPTION_COMPLETED, catalog.HISTORY_SAVED)
            assert changes == ["webhook"]
            assert editor.message.text() == "Saved."
            assert not editor.extras.isHidden() and not editor.delete_button.isHidden()
            assert section.count_label.text() == f"1 / {models.MAX_WEBHOOKS}"
            row = section._rows[hook.id]
            assert row.property("selected") is True
            assert "example.com" in row.detail.text()

            editor.url_edit.setText("https://example.org/new")
            assert section.has_unsaved_changes()
            section.refresh()  # a draft in progress survives a refresh
            assert editor.url_edit.text() == "https://example.org/new"
            assert section.save()
            assert models.find_webhook(store.load_all_settings(), hook.id).url == "https://example.org/new"
            assert len(models.load_webhooks(store.load_all_settings())) == 1

            queue.append(QMessageBox.StandardButton.Yes)
            editor.delete_button.click()
            assert models.load_webhooks(store.load_all_settings()) == []
            wait_for(lambda: forgotten == [hook.id])
            assert editor.isHidden() and not section.empty_state.isHidden()
            assert changes == ["webhook", "webhook", "webhook"]
        finally:
            close(section)

    def test_delete_can_be_called_off(self, store, answers):
        store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [HOOK]))
        section, changes = section_of("webhook", store)
        try:
            section.open_editor("hook01")
            section.editor.delete_button.click()  # answered Cancel
            assert len(models.load_webhooks(store.load_all_settings())) == 1
            assert changes == []
        finally:
            close(section)

    @pytest.mark.parametrize(
        "name, url, events, message",
        [
            ("", "https://example.com", True, "Give the webhook a name."),
            ("Home", "", True, "Enter the address to send to."),
            ("Home", "ftp://example.com", True, "Use an address that starts with http:// or https://."),
            ("Home", "https://example.com", False, "Choose at least one event."),
        ],
    )
    def test_validation_messages_show_inline(self, store, name, url, events, message):
        section, changes = section_of("webhook", store)
        try:
            section.open_editor("")
            editor = section.editor
            editor.name_edit.setText(name)
            editor.url_edit.setText(url)
            if not events:
                editor.events.set_events(())
            assert not section.save()
            assert editor.message.text() == message
            assert editor.message.property("tone") == "error"
            assert models.load_webhooks(store.load_all_settings()) == []
            assert changes == []
        finally:
            close(section)

    def test_duplicate_names_and_the_limit_are_refused(self, store):
        hooks = [dict(HOOK, id=f"h{i}", name=f"Hook {i}") for i in range(models.MAX_WEBHOOKS)]
        store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, hooks))
        section, _changes = section_of("webhook", store)
        try:
            assert not section.add_button.isEnabled()
            assert "up to 20" in section.add_button.toolTip()
            section.open_editor("h1")
            section.editor.name_edit.setText("hook 0")
            assert not section.save()
            assert section.editor.message.text() == "A webhook with that name already exists."
        finally:
            close(section)

    @pytest.mark.parametrize(
        "url, warned",
        [
            ("http://93.184.216.34/hook", True),
            ("http://hooks.example.com/in", True),
            ("https://hooks.example.com/in", False),
            ("http://127.0.0.1:8080/in", False),
            ("http://192.168.1.20/in", False),
            ("http://homeassistant.local:8123/api", False),
        ],
    )
    def test_plain_http_beyond_this_network_gets_a_gentle_note(self, store, url, warned):
        section, _changes = section_of("webhook", store)
        try:
            section.open_editor("")
            section.editor.url_edit.setText(url)
            assert section.editor.url_note.isHidden() is not warned
            if warned:
                assert "unencrypted" in section.editor.url_note.text()
        finally:
            close(section)

    def test_the_row_switch_saves_and_the_editor_keeps_it(self, store):
        store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [HOOK]))
        section, changes = section_of("webhook", store)
        try:
            section.open_editor("hook01")
            section.editor.name_edit.setText("Renamed")
            section._rows["hook01"].switch.click()
            assert models.find_webhook(store.load_all_settings(), "hook01").enabled is False
            assert section._rows["hook01"].detail.text().startswith("Off · ")
            assert section.editor.name_edit.text() == "Renamed"
            assert section.save()
            saved = models.find_webhook(store.load_all_settings(), "hook01")
            assert (saved.name, saved.enabled) == ("Renamed", False)
            assert changes == ["webhook", "webhook"]
        finally:
            close(section)

    def test_a_failed_switch_save_shows_why_and_keeps_the_saved_state(self, store, monkeypatch):
        store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [HOOK]))
        section, changes = section_of("webhook", store)
        try:
            def fail(_mutator):
                raise OSError("disk is read-only")

            monkeypatch.setattr(store, "mutate_settings", fail)
            section._rows["hook01"].switch.click()
            assert section._rows["hook01"].switch.isChecked()
            assert "read-only" in section.message.text()
            assert changes == []
        finally:
            close(section)

    def test_opening_another_hook_asks_to_save_the_draft(self, store, answers):
        queue, asked = answers
        second = dict(HOOK, id="hook02", name="Office")
        store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [HOOK, second]))
        section, _changes = section_of("webhook", store)
        try:
            section.open_editor("hook01")
            section.editor.name_edit.setText("Home 2")
            queue.append(QMessageBox.StandardButton.Cancel)
            assert not section.open_editor("hook02")
            assert section.editing_id() == "hook01"
            queue.append(QMessageBox.StandardButton.Save)
            assert section.open_editor("hook02")
            assert section.editing_id() == "hook02"
            assert models.find_webhook(store.load_all_settings(), "hook01").name == "Home 2"
            assert [title for title, _text in asked] == ["Unsaved webhook", "Unsaved webhook"]
            # The editor sits under the row it edits.
            layout = section._list
            assert layout.indexOf(section.editor) == layout.indexOf(section._rows["hook02"]) + 1
        finally:
            close(section)

    def test_send_test_shows_the_result_and_recent_deliveries(self, store, monkeypatch):
        store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [HOOK]))
        sent, history = [], []
        monkeypatch.setattr(webhooks, "send_test", lambda hook_id: sent.append(hook_id) or resolved(
            record(200, 120)))
        monkeypatch.setattr(webhooks, "deliveries", lambda hook_id: list(history))
        section, _changes = section_of("webhook", store)
        try:
            section.open_editor("hook01")
            editor = section.editor
            assert editor.recent._shown == ()
            history.extend([
                record(200, 118, event_type=catalog.TRANSCRIPTION_COMPLETED),
                record(502, 1430, ok=False, error="Bad gateway", attempt=3),
            ])
            editor.test_button.click()
            assert sent == ["hook01"]
            assert (editor.test_chip.text(), editor.test_chip.property("tone")) == ("200 · 120 ms", "ok")
            rows = editor.recent._shown
            assert [(what, result, ok) for _at, what, result, ok in rows] == [
                ("Finished dictations", "200 · 118 ms", True),
                ("Test", "502 · 1430 ms · try 3", False),
            ]
            section.refresh_live()
            row = section._rows["hook01"]
            assert row.detail.text().endswith("Last delivery 200 · 118 ms")
            assert row.dot.status() == EngineStatus.READY
        finally:
            close(section)

    def test_send_test_reports_errors_honestly(self, store, monkeypatch):
        store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [HOOK]))
        section, _changes = section_of("webhook", store)
        try:
            section.open_editor("hook01")
            editor = section.editor
            # An engine that can't send says why in its own words, on the line
            # under the button (an error is too long for the chip); no fake success.
            error_line = editor.test_chip.error_line
            monkeypatch.setattr(webhooks, "send_test", lambda hook_id: resolved(
                error=RuntimeError("Webhooks aren't available yet.")))
            editor.test_button.click()
            assert error_line.text() == "Webhooks aren't available yet."
            assert error_line.property("tone") == "error" and editor.test_chip.isHidden()

            def refuse(hook_id):
                raise RuntimeError("Saved webhooks changed; try again.")

            monkeypatch.setattr(webhooks, "send_test", refuse)
            editor.test_button.click()
            assert error_line.text() == "Saved webhooks changed; try again."
            assert editor.test_button.isEnabled()
            monkeypatch.setattr(webhooks, "send_test", lambda hook_id: resolved(
                record(None, 5000, ok=False, error="Timed out")))
            editor.test_button.click()
            assert error_line.text() == "Timed out" and not error_line.isHidden()
            monkeypatch.setattr(webhooks, "send_test", lambda hook_id: resolved(record(404, 80, ok=False)))
            editor.test_button.click()
            assert (editor.test_chip.text(), editor.test_chip.property("tone")) == ("404 · 80 ms", "error")
            assert error_line.isHidden()
            pending = Future()
            monkeypatch.setattr(webhooks, "send_test", lambda hook_id: pending)
            editor.test_button.click()
            assert (editor.test_chip.text(), editor.test_chip.property("tone")) == ("Sending…", "busy")
            assert not editor.test_button.isEnabled()
            pending.set_result(record(200, 95))
            wait_for(lambda: editor.test_chip.text() == "200 · 95 ms")
            assert editor.test_button.isEnabled()
        finally:
            close(section)

    def test_send_test_saves_a_changed_draft_first(self, store, monkeypatch):
        store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [HOOK]))
        monkeypatch.setattr(webhooks, "send_test", lambda hook_id: resolved(record()))
        section, changes = section_of("webhook", store)
        try:
            section.open_editor("hook01")
            section.editor.url_edit.setText("https://example.com/v2")
            section.editor.test_button.click()
            assert models.find_webhook(store.load_all_settings(), "hook01").url == "https://example.com/v2"
            assert changes == ["webhook"]
            section.editor.url_edit.setText("not a url")
            section.editor.test_button.click()
            assert section.editor.test_chip.isHidden() or section.editor.test_chip.text() == "200 · 120 ms"
            assert "http://" in section.editor.message.text()
        finally:
            close(section)

    def test_copy_and_new_secret_go_through_the_engine(self, store, answers, monkeypatch):
        queue, _asked = answers
        store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [HOOK]))
        calls = []
        monkeypatch.setattr(webhooks, "ensure_secret", lambda hook_id: calls.append(("ensure", hook_id))
                            or "whsec_first")
        monkeypatch.setattr(webhooks, "rotate_secret", lambda hook_id: calls.append(("rotate", hook_id))
                            or "whsec_second")
        section, _changes = section_of("webhook", store)
        try:
            section.open_editor("hook01")
            editor = section.editor
            editor.copy_secret.click()
            wait_for(lambda: editor.secret_chip.text() == "Copied")
            assert QApplication.clipboard().text() == "whsec_first"
            editor.rotate_secret.click()  # answered Cancel
            assert ("rotate", "hook01") not in calls
            queue.append(QMessageBox.StandardButton.Yes)
            editor.rotate_secret.click()
            wait_for(lambda: editor.secret_chip.text() == "New secret copied")
            assert QApplication.clipboard().text() == "whsec_second"
            assert calls == [("ensure", "hook01"), ("rotate", "hook01")]
            # The secret never reaches settings.
            assert "whsec_" not in str(store.load_all_settings())
        finally:
            close(section)

    def test_secret_errors_show_under_the_buttons(self, store, monkeypatch):
        store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [HOOK]))

        def unavailable(hook_id):
            # The engine's SecretUnavailable is a RuntimeError with user-facing copy.
            raise RuntimeError("Your system's credential store is locked.")

        monkeypatch.setattr(webhooks, "ensure_secret", unavailable)
        section, _changes = section_of("webhook", store)
        try:
            section.open_editor("hook01")
            section.editor.copy_secret.click()
            error_line = section.editor.secret_chip.error_line
            wait_for(lambda: not error_line.isHidden())
            assert error_line.text() == "Your system's credential store is locked."
            assert section.editor.secret_chip.isHidden()
            assert section.editor.copy_secret.isEnabled()
        finally:
            close(section)

    def test_cancel_closes_a_new_draft_without_saving(self, store):
        section, changes = section_of("webhook", store)
        try:
            section.open_editor("")
            section.editor.name_edit.setText("Never saved")
            section.editor.cancel_button.click()
            assert section.editor.isHidden() and not section.has_unsaved_changes()
            assert models.load_webhooks(store.load_all_settings()) == []
            assert changes == []
        finally:
            close(section)


# ---- programs ----------------------------------------------------------------

class TestPrograms:
    def test_add_with_the_file_picker_arguments_and_time_limit(self, store, monkeypatch):
        chosen = os.path.abspath(os.path.join("tools", "log.exe"))
        monkeypatch.setattr(hooks_panel.QFileDialog, "getOpenFileName",
                            lambda *args, **kwargs: (chosen, ""))
        section, changes = section_of("program", store)
        try:
            section.open_editor("")
            editor = section.editor
            editor.name_edit.setText("Log")
            editor.choose_button.click()
            assert editor.program_edit.text() == chosen
            editor.args_edit.setPlainText("--quiet\n\n  --tag=voice  \n")
            editor.timeout.setValue(45)
            editor.events.chips[catalog.ACTION_RAN].click()
            assert section.save()
            [program] = models.load_programs(store.load_all_settings())
            assert program.program == chosen
            assert program.args == ("--quiet", "--tag=voice")
            assert program.timeout_s == 45
            assert program.events == (catalog.TRANSCRIPTION_COMPLETED, catalog.ACTION_RAN)
            assert changes == ["program"]
            assert "log.exe" in section._rows[program.id].detail.text()
        finally:
            close(section)

    def test_a_relative_program_path_is_refused(self, store):
        section, _changes = section_of("program", store)
        try:
            section.open_editor("")
            section.editor.name_edit.setText("Log")
            section.editor.program_edit.setText("log.exe")
            assert not section.save()
            assert section.editor.message.text() == "Choose the program with its full path."
        finally:
            close(section)

    def test_run_test_and_recent_runs(self, store, monkeypatch):
        store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_PROGRAMS, [PROGRAM]))
        runs = [programs.RunRecord("evt_2", "2026-10-10T18:03:00Z", True, 0, 340),
                programs.RunRecord("evt_1", "2026-10-10T17:40:00Z", False, None, 30000,
                                   "Timed out after 30 s")]
        monkeypatch.setattr(programs, "runs", lambda program_id: list(runs))
        monkeypatch.setattr(programs, "run_test", lambda program_id: resolved(runs[0]))
        section, _changes = section_of("program", store)
        try:
            section.open_editor("prog01")
            editor = section.editor
            editor.test_button.click()
            assert (editor.test_chip.text(), editor.test_chip.property("tone")) == ("Exit 0 · 340 ms", "ok")
            assert [(result, ok) for _at, _what, result, ok in editor.recent._shown] == [
                ("Exit 0 · 340 ms", True), ("Timed out after 30 s", False),
            ]
            monkeypatch.setattr(programs, "run_test", lambda program_id: resolved(
                programs.RunRecord("evt_3", "2026-10-10T18:04:00Z", False, 2, 80)))
            editor.test_button.click()
            assert (editor.test_chip.text(), editor.test_chip.property("tone")) == ("Exit 2 · 80 ms", "error")
            monkeypatch.setattr(programs, "run_test", lambda program_id: resolved(
                error=RuntimeError("Programs aren't available yet.")))
            editor.test_button.click()
            assert editor.test_chip.error_line.text() == "Programs aren't available yet."
        finally:
            close(section)


# ---- text filters -------------------------------------------------------------

class TestFilters:
    def test_web_address_and_program_filters_save_only_their_own_fields(self, store):
        section, changes = section_of("filter", store)
        try:
            section.open_editor("")
            editor = section.editor
            assert editor.kind_bar.currentIndex() == 0
            assert not editor.url_edit.isHidden() and editor.program_row.isHidden()
            assert "posts the words as JSON" in editor.reply_hint.text()
            editor.name_edit.setText("Names")
            editor.url_edit.setText("http://127.0.0.1:5005/fix")
            editor.program_edit.setText(os.path.abspath("ignored.exe"))
            editor.stage_bar.setCurrentIndex(0)
            editor.timeout.setValue(800)
            assert section.save()
            [saved] = models.load_filters(store.load_all_settings())
            assert (saved.kind, saved.stage, saved.url, saved.program, saved.timeout_ms) == (
                "http", models.BEFORE_CLEANUP, "http://127.0.0.1:5005/fix", "", 800)

            editor.kind_bar.buttons[1].click()
            assert editor.url_edit.isHidden() and not editor.program_row.isHidden()
            assert "program's input" in editor.reply_hint.text()
            editor.program_edit.setText(os.path.abspath("swearjar.exe"))
            editor.args_edit.setPlainText("--strict")
            assert section.save()
            saved = models.find_filter(store.load_all_settings(), saved.id)
            assert (saved.kind, saved.url, saved.args) == ("program", "", ("--strict",))
            assert changes == ["filter", "filter"]
            assert "swearjar.exe · Before AI cleanup · 800 ms" in section._rows[saved.id].detail.text()
            assert section._rows[saved.id].dot.isHidden()
        finally:
            close(section)

    def test_time_limit_validation(self, store):
        section, _changes = section_of("filter", store)
        try:
            section.open_editor("")
            editor = section.editor
            assert (editor.timeout.minimum(), editor.timeout.maximum()) == (
                models.FILTER_TIMEOUT_MIN_MS, models.FILTER_TIMEOUT_MAX_MS)
            assert editor.timeout.value() == models.FILTER_TIMEOUT_DEFAULT_MS
            editor.name_edit.setText("Names")
            assert not section.save()
            assert editor.message.text() == "Enter the address to send to."
        finally:
            close(section)

    def test_leased_filters_show_read_only_with_their_expiry(self, store, monkeypatch):
        leases = [filters.Lease("l1", "http://127.0.0.1:7070/f", models.AFTER_CLEANUP, 1500,
                                ("dictation",), time.time() + 42),
                  filters.Lease("l2", "http://127.0.0.1:7071/f", models.BEFORE_CLEANUP, 1500,
                                ("dictation",), time.time() - 1)]
        monkeypatch.setattr(filters, "leases", lambda: list(leases))
        section, _changes = section_of("filter", store)
        try:
            lease_list = section.leases
            assert len(lease_list._shown) == 1
            _id, url, stage, seconds = lease_list._shown[0]
            assert (url, stage) == ("http://127.0.0.1:7070/f", models.AFTER_CLEANUP)
            assert 40 <= seconds <= 42
            assert lease_list.note.isHidden() and lease_list.open_api.isHidden()
            [line] = lease_list._lines
            assert line.detail.text().startswith("After AI cleanup · ends in ")
            # A renewal moves the countdown on the same row.
            leases[0] = filters.Lease("l1", "http://127.0.0.1:7070/f", models.AFTER_CLEANUP, 1500,
                                      ("dictation",), time.time() + 20.4)
            section.refresh_live()
            assert lease_list._lines == [line]
            assert line.detail.text() == "After AI cleanup · ends in 20 s"
            leases.clear()
            section.refresh_live()
            assert lease_list._shown == ()
            assert not lease_list.note.isHidden()
        finally:
            close(section)


def test_a_secret_worker_never_keeps_the_editor_alive(store, monkeypatch):
    """Workers answer through the relay; deleting Settings mid-request is safe."""
    import gc
    import threading
    import weakref

    from PyQt6.QtCore import QCoreApplication, QEvent

    store.mutate_settings(lambda s: s.__setitem__(SettingsKey.AUTOMATION_WEBHOOKS, [HOOK]))
    release = threading.Event()

    def slow_secret(hook_id):
        assert release.wait(5)
        return "whsec_late"

    monkeypatch.setattr(webhooks, "ensure_secret", slow_secret)
    before = set(threading.enumerate())
    section, _changes = section_of("webhook", store)
    section.open_editor("hook01")
    section.editor.copy_secret.click()
    workers = [t for t in threading.enumerate()
               if t not in before and t.name == "settings-webhook-secret"]
    assert len(workers) == 1
    gone = weakref.ref(section.editor)
    section.deleteLater()
    del section
    for _ in range(2):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        QApplication.processEvents()
    gc.collect()
    assert gone() is None
    release.set()
    workers[0].join(5)
    pump()  # the late answer finds no editor and is dropped


# ---- helpers and layout -------------------------------------------------------

def test_result_text_and_clock():
    assert hooks_panel.delivery_result(record(200, 120)) == ("200 · 120 ms", True)
    assert hooks_panel.delivery_result(record(None, 0, ok=False, error="")) == ("No answer", False)
    assert hooks_panel.events_summary(()) == "No events"
    assert hooks_panel.events_summary((catalog.ACTION_RAN, catalog.HISTORY_SAVED)) == "Actions that ran +1"
    assert hooks_panel.clock("not a time") == "not a time"
    assert len(hooks_panel.clock("2026-10-10T18:02:11.120Z")) == 8
    assert set(hooks_panel.event_types()) == set(catalog.PUBLIC_TYPES)


@pytest.mark.parametrize("ui_mode", ["classic", "omarchy"])
@pytest.mark.parametrize("font_percent", [100, 130])
def test_sections_fit_narrow_columns_at_every_scale(store, monkeypatch, ui_mode, font_percent):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_scale, previous_palette, previous_sheet = (
        current_ui_font_scale_percent(), current_palette(), app.styleSheet())
    previous_font = app.font()
    manager = ThemeManager()
    store.mutate_settings(lambda s: s.update({
        SettingsKey.AUTOMATION_WEBHOOKS: [HOOK],
        SettingsKey.AUTOMATION_PROGRAMS: [PROGRAM],
    }))
    webhook_section, _ = section_of("webhook", store)
    program_section, _ = section_of("program", store)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    host = QWidget()
    column = QVBoxLayout(host)
    column.addWidget(webhook_section)
    column.addWidget(program_section)
    column.addStretch()
    scroll.setWidget(host)
    try:
        apply_ui_font_scale(font_percent, app=app, theme_manager=manager)
        scroll.show()
        webhook_section.open_editor("hook01")
        program_section.open_editor("prog01")
        for width in (360, 480, 720):
            scroll.resize(width, 900)
            pump(8)
            assert scroll.horizontalScrollBar().maximum() == 0
            for section in (webhook_section, program_section):
                row = next(iter(section._rows.values()))
                edit, switch = row.edit_button, row.switch
                assert not QRect(edit.mapTo(row, QPoint(0, 0)), edit.size()).intersects(
                    QRect(switch.mapTo(row, QPoint(0, 0)), switch.size()))
                for button in section.findChildren(QAbstractButton):
                    if not button.isVisible():
                        continue
                    position = button.mapTo(host, QPoint(0, 0))
                    assert position.x() >= 0
                    assert position.x() + button.width() <= host.width() + 1, button.text()
                    assert button.height() >= min(button.sizeHint().height(), 34) - 1, button.text()
            footer = webhook_section.editor.footer
            for button in (webhook_section.editor.save_button, webhook_section.editor.cancel_button):
                assert button.width() >= button.sizeHint().width() - 1
                assert footer.rect().contains(QRect(button.mapTo(footer, QPoint(0, 0)), button.size()))
    finally:
        scroll.close()
        scroll.deleteLater()
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setStyleSheet(previous_sheet)
        app.setFont(previous_font)
