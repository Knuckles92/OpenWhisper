"""Settings → Hooks: Tab goes down the page the way it reads, through each row
and the editor that opens under one, also for rows added later."""

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget

from services.automation import models
from services.settings import SettingsKey, SettingsManager
from tests.test_automation_fix_ui_hooks_answers import ALPHA, BETA, GAMMA, close, pump
from ui_qt.widgets import hooks_panel


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    manager = SettingsManager(str(tmp_path / "settings.json"))
    manager.save_all_settings({SettingsKey.AUTOMATION_WEBHOOKS: [ALPHA, BETA, GAMMA]})
    return manager


def tab_walk(start, within, limit=400):
    """The widgets Tab visits from ``start`` inside ``within``, as Qt picks them."""
    visited, widget = [start], start
    for _ in range(limit):
        widget = widget.nextInFocusChain()
        if widget is start:
            break
        if (within.isAncestorOf(widget)
                and widget.focusPolicy() & Qt.FocusPolicy.TabFocus == Qt.FocusPolicy.TabFocus
                and widget.focusProxy() is None
                and widget.isVisible() and widget.isEnabled()):
            visited.append(widget)
    return visited


def name_of(widget):
    return widget.accessibleName() or getattr(widget, "text", lambda: "")() or type(widget).__name__


def assert_reading_order(widgets, within):
    spots = [(w.mapTo(within, QPoint(0, 0)), w) for w in widgets]
    for (before, first), (after, second) in zip(spots, spots[1:]):
        same_line = abs(after.y() - before.y()) <= 6
        assert after.y() > before.y() or (same_line and after.x() > before.x()), (
            f"Tab goes from {name_of(first)!r} back up to {name_of(second)!r}")


def row_stops(section, *item_ids):
    return [widget for item_id in item_ids
            for widget in (section._rows[item_id].edit_button, section._rows[item_id].switch)]


@pytest.fixture
def page(store):
    """A section between two buttons, as on the Hooks page."""
    window = QWidget()
    layout = QVBoxLayout(window)
    first = hooks_panel._small_button("Before")
    layout.addWidget(first)
    section = hooks_panel.WebhookSection(store)
    layout.addWidget(section)
    last = hooks_panel._small_button("After")
    layout.addWidget(last)
    window.resize(720, 1600)
    window.show()
    pump(8)
    yield window, first, section, last
    close(window)


def test_tab_runs_through_rows_and_the_editor_in_screen_order(page):
    window, first, section, last = page
    section.open_editor("beta000")
    pump(8)
    walk = tab_walk(first, window)
    editor = section.editor
    assert walk[:6] == [first, *row_stops(section, "alpha00", "beta000"), editor.name_edit]
    assert walk[-6:] == [editor.cancel_button, editor.save_button,
                         *row_stops(section, "gamma00"), section.add_button, last]
    assert_reading_order(walk, window)


def test_a_hook_added_later_takes_its_place(page, monkeypatch):
    window, first, section, last = page
    section.open_editor("")
    editor = section.editor
    editor.name_edit.setText("Delta")
    editor.url_edit.setText("https://d.example/hook")
    # A new item's editor sits after the rows.
    walk = tab_walk(first, window)
    assert walk[:8] == [first, *row_stops(section, "alpha00", "beta000", "gamma00"),
                        editor.name_edit]
    assert section.save()
    pump(8)
    delta = editor.item_id()
    walk = tab_walk(first, window)
    assert walk[:10] == [first, *row_stops(section, "alpha00", "beta000", "gamma00", delta),
                         editor.name_edit]
    assert walk[-3:] == [editor.save_button, section.add_button, last]
    assert_reading_order(walk, window)


def test_closing_the_editor_leaves_the_rows_in_order(page):
    window, first, section, last = page
    section.open_editor("alpha00")
    pump(8)
    section.cancel()
    pump(8)
    assert tab_walk(first, window) == [
        first, *row_stops(section, "alpha00", "beta000", "gamma00"), section.add_button, last,
    ]


def test_the_hooks_page_reads_top_to_bottom_with_every_kind_of_editor_open(tmp_path):
    from contextlib import ExitStack
    from unittest.mock import patch

    from ui_qt.dialogs import settings_dialog as settings_dialog_module
    from ui_qt.dialogs import settings_downloads as downloads_module
    from ui_qt.dialogs import settings_metadata
    from ui_qt.dialogs import settings_models as models_module
    from ui_qt.dialogs.settings_destinations import HOOKS

    program = {"id": "prog001", "name": "Journal", "program": os.path.abspath("journal.exe"),
               "args": ["--append"], "events": ["history.saved"], "enabled": True, "timeout_s": 30}
    text_filter = {"id": "filt001", "name": "Names", "kind": "program", "stage": "after_cleanup",
                   "program": os.path.abspath("names.exe"), "timeout_ms": 1500, "enabled": True}
    store = SettingsManager(str(tmp_path / "dialog.json"))
    store.save_all_settings({
        SettingsKey.AUTOMATION_WEBHOOKS: [ALPHA, BETA],
        SettingsKey.AUTOMATION_PROGRAMS: [program, dict(program, id="prog002", name="Logger")],
        SettingsKey.AUTOMATION_FILTERS: [text_filter],
    })
    controls = dict(settings_metadata.CONTROL_DESTINATIONS)
    fields = dict(settings_metadata.PAGE_SEARCH_FIELDS)
    with ExitStack() as stack:
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        dialog = settings_dialog_module.SettingsDialog(background_cache_scan=False)
        try:
            dialog._loading = True
            dialog.resize(1100, 900)
            dialog.select_destination(HOOKS)
            dialog.show()
            pump(8)
            page = dialog._pages[HOOKS]
            sections = (dialog.hooks_webhooks, dialog.hooks_programs, dialog.hooks_filters)
            dialog.hooks_programs.open_editor("prog001")
            dialog.hooks_filters.open_editor("filt001")
            pump(8)
            first = dialog.hooks_webhooks._rows["alpha00"].edit_button
            walk = tab_walk(first, page)
            assert_reading_order(walk, page)
            for section in sections:
                rows = [w for row in section._rows.values() for w in (row.edit_button, row.switch)]
                assert set(rows) <= set(walk)
                assert walk.index(section.add_button) > max(walk.index(w) for w in rows)
            # The program and filter editors' fields are all on the way.
            for editor in (dialog.hooks_programs.editor, dialog.hooks_filters.editor):
                assert editor.name_edit in walk and editor.save_button in walk
                assert editor.args_edit in walk
                assert any(editor.timeout.isAncestorOf(w) or w is editor.timeout for w in walk)
        finally:
            dialog.hide()
            dialog.deleteLater()
            pump()
            settings_metadata.CONTROL_DESTINATIONS.clear()
            settings_metadata.CONTROL_DESTINATIONS.update(controls)
            settings_metadata.PAGE_SEARCH_FIELDS.clear()
            settings_metadata.PAGE_SEARCH_FIELDS.update(fields)


def test_a_hook_deleted_elsewhere_leaves_no_gap(page, store):
    window, first, section, last = page
    store.mutate_settings(lambda s: models.delete_webhook(s, "beta000"))
    section.refresh()
    pump(8)
    assert tab_walk(first, window) == [
        first, *row_stops(section, "alpha00", "gamma00"), section.add_button, last,
    ]
