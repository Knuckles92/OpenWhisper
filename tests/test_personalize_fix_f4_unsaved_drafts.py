"""Snippet and transform edits save on leaving, as "Changes save automatically" says."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QMessageBox

from services.snippets import load_snippets
from services.text_transforms import load_transforms
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs.settings_destinations import COMMANDS, GENERAL, SNIPPETS
from tests.fakes.personalize import settings_dialogs


@pytest.fixture(autouse=True)
def _restore_metadata():
    controls = dict(settings_metadata.CONTROL_DESTINATIONS)
    fields = dict(settings_metadata.PAGE_SEARCH_FIELDS)
    yield
    settings_metadata.CONTROL_DESTINATIONS.clear()
    settings_metadata.CONTROL_DESTINATIONS.update(controls)
    settings_metadata.PAGE_SEARCH_FIELDS.clear()
    settings_metadata.PAGE_SEARCH_FIELDS.update(fields)


class _Asked(list):
    answer = None


@pytest.fixture
def questions(monkeypatch):
    """Every question asked; answers come from ``questions.answer``."""
    asked = _Asked()

    def question(_parent, title, text, *_args):
        asked.append((title, text))
        return asked.answer

    asked.answer = QMessageBox.StandardButton.Cancel
    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    return asked


@pytest.fixture
def make_dialog(questions):
    with settings_dialogs(mocks=("on_settings_changed",), get_loaded_model=lambda: None) as new:
        def build():
            dialog, store = new()
            dialog.show()
            return dialog, store

        yield build
        questions.answer = QMessageBox.StandardButton.Discard
        for dialog in new.dialogs:
            dialog.close()


def _snippet_draft(dialog, trigger="my address", text="1 Main St"):
    dialog.select_destination(SNIPPETS)
    panel = dialog.snippets_panel
    panel.new_snippet()
    panel.trigger_edit.setText(trigger)
    panel.text_edit.setPlainText(text)
    assert panel.has_unsaved_changes()
    return panel


def _transform_draft(dialog, name="Make it a haiku", instruction="Rewrite it as a haiku."):
    dialog.select_destination(COMMANDS)
    panel = dialog.transforms_panel
    panel.new_transform()
    panel.name_edit.setText(name)
    panel.instruction_edit.setPlainText(instruction)
    assert panel.has_unsaved_changes()
    return panel


def _snippets(store):
    return [s.trigger for s in load_snippets(store.load_all_settings())]


def _transforms(store):
    return [t.name for t in load_transforms(store.load_all_settings())]


def test_leaving_the_page_saves_a_snippet_draft(make_dialog, questions):
    dialog, store = make_dialog()
    panel = _snippet_draft(dialog)

    dialog.select_destination(GENERAL)

    assert _snippets(store) == ["my address"]
    assert not panel.has_unsaved_changes()
    assert questions == []
    dialog.on_settings_changed.assert_called_with("snippets")


def test_done_saves_a_transform_draft(make_dialog, questions):
    dialog, store = make_dialog()
    _transform_draft(dialog)

    dialog.close()

    assert "Make it a haiku" in _transforms(store)
    assert not dialog.isVisible()
    assert questions == []


def test_escape_saves_drafts_on_every_page(make_dialog, questions):
    dialog, store = make_dialog()
    _snippet_draft(dialog)
    _transform_draft(dialog)

    QTest.keyClick(dialog, Qt.Key.Key_Escape)

    assert _snippets(store) == ["my address"]
    assert "Make it a haiku" in _transforms(store)
    assert not dialog.isVisible()


def test_going_home_saves_a_draft(make_dialog):
    dialog, store = make_dialog()
    _snippet_draft(dialog)

    dialog.show_home()

    assert _snippets(store) == ["my address"]


def test_an_unfinished_draft_stays_when_the_page_is_left(make_dialog, questions):
    dialog, store = make_dialog()
    panel = _snippet_draft(dialog, text="")

    dialog.select_destination(GENERAL)
    dialog.select_destination(SNIPPETS)

    assert questions == []
    assert _snippets(store) == []
    assert panel.trigger_edit.text() == "my address"
    assert panel.message.text() == "Add the text to insert."


def test_done_with_an_unfinished_draft_can_keep_editing(make_dialog, questions):
    dialog, store = make_dialog()
    panel = _snippet_draft(dialog, text="")
    dialog.select_destination(GENERAL)

    dialog.close()

    assert questions == [(
        "Unsaved snippet",
        "Add the text to insert. Discard your changes to this snippet?",
    )]
    assert dialog.isVisible()
    assert dialog.rail.current_key() == SNIPPETS
    assert panel.trigger_edit.text() == "my address"
    assert _snippets(store) == []


def test_done_with_an_unfinished_draft_can_discard_it(make_dialog, questions):
    dialog, store = make_dialog()
    panel = _transform_draft(dialog, instruction="")
    questions.answer = QMessageBox.StandardButton.Discard

    dialog.close()

    assert [title for title, _text in questions] == ["Unsaved transform"]
    assert not dialog.isVisible()
    assert not panel.has_unsaved_changes()
    assert "Make it a haiku" not in _transforms(store)


def test_closing_without_drafts_asks_nothing(make_dialog, questions):
    dialog, _store = make_dialog()
    dialog.select_destination(SNIPPETS)
    dialog.select_destination(COMMANDS)

    dialog.close()

    assert questions == []
    assert not dialog.isVisible()
