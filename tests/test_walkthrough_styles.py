"""Write-up styles: saved prompts, and redoing a write-up with one or with new words."""
from dataclasses import replace
from unittest.mock import Mock

import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QInputDialog, QMessageBox

from services.settings import SettingsKey, settings_manager
from services.walkthrough import library, write_up
from services.walkthrough.styles import (
    DEFAULT_STYLE,
    MAX_STYLES,
    STARTER_STYLES,
    delete_style,
    load_styles,
    save_style,
    style_for,
)
from tests.test_walkthrough_library import capture as capture_fixture
from ui_qt.dialogs.new_style_dialog import NewStyleDialog
from ui_qt.dialogs.walkthrough_library import WalkthroughLibraryDialog
from ui_qt.dialogs.write_up_prompt_dialog import OWN_WORDS, WriteUpPromptDialog
from ui_qt.widgets.walkthrough_tab import NEW_STYLE, OWN_PROMPT, WalkthroughTab

capture = capture_fixture
STYLES = SettingsKey.WALKTHROUGH_WRITE_UP_STYLES


def _saved() -> list:
    return load_styles(settings_manager.load_all_settings())


# ---- the library ----

def test_the_default_comes_first_then_the_starters_until_any_is_saved():
    assert load_styles({}) == [DEFAULT_STYLE, *STARTER_STYLES]
    assert [s.name for s in load_styles({})][:3] == ["Walkthrough", "Step-by-step guide",
                                                     "Bug report"]


def test_deleting_every_style_leaves_only_the_default():
    settings = {}
    for style in STARTER_STYLES:
        delete_style(settings, style.id)
    assert settings[STYLES] == []
    assert load_styles(settings) == [DEFAULT_STYLE]


def test_a_saved_style_is_added_and_one_by_the_same_name_replaced():
    settings = {}
    added = save_style(settings, "  Release notes ", "  Bullets of what changed. ")
    assert load_styles(settings)[-1] == added
    assert (added.name, added.instructions) == ("Release notes", "Bullets of what changed.")

    again = save_style(settings, "release NOTES", "Shorter bullets.")
    styles = load_styles(settings)
    assert again.id == added.id and again.name == "release NOTES"
    assert [s.instructions for s in styles if s.id == added.id] == ["Shorter bullets."]
    assert len(styles) == 1 + len(STARTER_STYLES) + 1


def test_a_starter_can_be_rewritten_by_its_name():
    settings = {}
    save_style(settings, "Bug report", "Just the steps.")
    bug = next(s for s in load_styles(settings) if s.name == "Bug report")
    assert bug.id == "bug-report" and bug.instructions == "Just the steps."


@pytest.mark.parametrize(("name", "words", "says"), [
    ("", "Words.", "name"),
    ("x" * 61, "Words.", "60 characters"),
    ("Checklist", "  ", "instructions"),
    ("walkthrough", "Words.", "built-in"),
])
def test_a_style_that_cant_be_saved_says_why(name, words, says):
    settings = {}
    with pytest.raises(ValueError, match=says):
        save_style(settings, name, words)
    assert STYLES not in settings


def test_there_is_room_for_only_so_many_styles():
    settings = {STYLES: []}
    for number in range(MAX_STYLES):
        save_style(settings, f"Style {number}", "Words.")
    with pytest.raises(ValueError, match="up to"):
        save_style(settings, "One more", "Words.")
    save_style(settings, "Style 3", "Replacing one still works.")


def test_broken_saved_styles_are_skipped():
    settings = {STYLES: [
        {"id": "a", "name": "Fine", "instructions": "Words."},
        {"id": "a", "name": "Same id", "instructions": "Words."},
        {"id": "walkthrough", "name": "Posing as the default", "instructions": "Words."},
        {"id": "b", "name": "", "instructions": "Words."},
        "not a style",
    ]}
    assert [s.name for s in load_styles(settings)] == ["Walkthrough", "Fine"]
    assert load_styles({STYLES: "nonsense"}) == [DEFAULT_STYLE]


def test_the_default_words_and_an_empty_prompt_are_the_default_style():
    styles = load_styles({})
    assert style_for("", styles) == DEFAULT_STYLE
    assert style_for(STARTER_STYLES[1].instructions + "\n", styles) == STARTER_STYLES[1]
    assert style_for("Something else.", styles) is None


def test_a_redo_with_instructions_uses_them_instead_of_settings(capture, monkeypatch):
    settings_manager.save_setting(SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS, "From Settings.")
    called = []
    monkeypatch.setattr(write_up, "write_up", lambda result, shots, **kwargs:
                        called.append(kwargs["instructions"]) or replace(result, written_up=True))

    library.retry_write_up(capture.folder, instructions="  Just a checklist. ")
    library.retry_write_up(capture.folder)

    assert called == ["Just a checklist.", "From Settings."]


# ---- redoing it from the storyboard ----

@pytest.fixture
def review(capture):
    widget = WalkthroughLibraryDialog(root=capture.root, copy_files=Mock(return_value=True))
    yield widget.review
    widget.review._dirty = False
    widget.close()


@pytest.fixture
def redone(review, capture, monkeypatch):
    seen = []

    def retry(folder, *, instructions, cancel_event):
        seen.append(instructions)
        return capture.result
    monkeypatch.setattr(library, "retry_write_up", retry)
    return seen


def _wait(review):
    for _ in range(300):
        if not review._busy:
            return
        QTest.qWait(10)
    pytest.fail("Library worker did not finish")


def test_redo_as_lists_every_style_and_writes_it_up_with_the_one_picked(review, redone):
    settings_manager.mutate_settings(lambda s: save_style(s, "Release notes", "Bullets."))
    review.style_menu.aboutToShow.emit()
    actions = review.style_menu.actions()
    assert [a.text() for a in actions] == [s.name for s in _saved()]
    assert actions[-1].text() == "Release notes"

    next(a for a in actions if a.text() == "Bug report").trigger()
    _wait(review)

    assert redone == [STARTER_STYLES[1].instructions]


def test_the_menu_says_write_up_until_there_is_one(review):
    assert review.style_menu.title() == "Write up as"
    assert review.prompt_button.text() == "Write up with a new prompt…"


def test_a_new_prompt_writes_it_up_once_without_changing_settings(review, redone, monkeypatch):
    settings_manager.save_setting(SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS, "Usual words.")
    opened = []

    def exec_(dialog):
        opened.append(dialog.prompt_text())
        dialog.prompt_edit.setPlainText("A one-paragraph summary.")
        dialog._go()
        return dialog.result()
    monkeypatch.setattr(WriteUpPromptDialog, "exec", exec_)

    review.prompt_button.trigger()
    _wait(review)

    assert opened == ["Usual words."]  # it starts from the usual prompt
    assert redone == ["A one-paragraph summary."]
    assert (settings_manager.load_all_settings()[SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS]
            == "Usual words.")


def test_canceling_the_new_prompt_writes_nothing_up(review, redone, monkeypatch):
    monkeypatch.setattr(WriteUpPromptDialog, "exec", lambda dialog: 0)
    review.prompt_button.trigger()
    assert not review._busy and redone == []


def test_unsaved_edits_hold_a_redo_with_a_new_prompt(review, redone, monkeypatch):
    opened = Mock()
    monkeypatch.setattr(WriteUpPromptDialog, "exec", opened)
    review._editors[0].setPlainText("corrected")
    review.prompt_button.trigger()
    opened.assert_not_called()
    assert "Save" in review.notice.text()


# ---- the prompt dialog ----

@pytest.fixture
def prompt_dialog():
    dialogs = []

    def make(prompt="", **kwargs):
        dialogs.append(WriteUpPromptDialog(prompt, **kwargs))
        return dialogs[-1]
    yield make
    for dialog in dialogs:
        dialog.close()


def test_the_dialog_starts_on_the_style_its_words_are(prompt_dialog):
    dialog = prompt_dialog(STARTER_STYLES[2].instructions)
    assert dialog.style_combo.currentText() == STARTER_STYLES[2].name
    assert dialog.style_combo.findText(OWN_WORDS) < 0

    dialog.prompt_edit.setPlainText("My own.")
    assert dialog.style_combo.currentText() == OWN_WORDS

    index = dialog.style_combo.findText("Bug report")
    dialog.style_combo.activated.emit(index)
    assert dialog.prompt_text() == STARTER_STYLES[1].instructions
    assert dialog.style_combo.currentText() == "Bug report"


def test_an_empty_prompt_isnt_sent(prompt_dialog):
    dialog = prompt_dialog("  ")
    dialog._go()
    assert dialog.result() != WriteUpPromptDialog.DialogCode.Accepted
    assert "Type what you want" in dialog.error.text()


def test_naming_the_prompt_keeps_it_as_a_style(prompt_dialog):
    dialog = prompt_dialog("Short changelog bullets.")
    dialog.name_edit.setText("Changelog")
    dialog._go()
    assert dialog.result() == WriteUpPromptDialog.DialogCode.Accepted
    assert _saved()[-1].name == "Changelog"
    assert _saved()[-1].instructions == "Short changelog bullets."


def test_a_name_that_cant_be_saved_keeps_the_dialog_open(prompt_dialog):
    dialog = prompt_dialog("Words.")
    dialog.name_edit.setText("Walkthrough")
    dialog._go()
    assert dialog.result() != WriteUpPromptDialog.DialogCode.Accepted
    assert "built-in" in dialog.error.text()
    assert len(_saved()) == 1 + len(STARTER_STYLES)


def test_the_dialog_says_write_up_until_there_is_one(prompt_dialog):
    assert prompt_dialog("Words.", written=False).go_button.text() == "Write it up"


# ---- the Walkthrough tab ----

@pytest.fixture
def tab():
    widget = WalkthroughTab()
    yield widget
    widget.close()


def _names(tab) -> list[str]:
    return [tab.preset_combo.itemText(i) for i in range(tab.preset_combo.count())]


def test_your_own_words_can_be_saved_as_a_style(tab, monkeypatch):
    assert not tab.save_style_button.isVisibleTo(tab.prompt_area)
    tab.prompt_edit.setPlainText("A checklist for a new teammate.")
    assert tab.save_style_button.isVisibleTo(tab.prompt_area)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args: ("Onboarding", True))

    tab.save_style_button.click()

    assert tab.preset_combo.currentText() == "Onboarding"
    assert "Onboarding" in _names(tab)
    assert not tab.save_style_button.isVisibleTo(tab.prompt_area)
    assert tab.delete_style_button.isVisibleTo(tab.prompt_area)


def test_a_style_saved_elsewhere_shows_on_return(tab):
    settings_manager.mutate_settings(lambda s: save_style(s, "From the storyboard", "Words."))
    tab.load_settings()
    assert _names(tab)[-3:] == ["From the storyboard", "", NEW_STYLE]  # "" is the separator


def _choose_new_style(tab, monkeypatch, fill=None) -> list:
    """Pick New style… with the dialog filled by ``fill``; returns the dialogs shown."""
    shown = []

    def exec_(dialog):
        shown.append(dialog)
        if fill is None:
            return NewStyleDialog.DialogCode.Rejected
        fill(dialog)
        dialog._save()
        return dialog.result()

    monkeypatch.setattr(NewStyleDialog, "exec", exec_)
    tab.preset_combo.activated.emit(tab.preset_combo.findText(NEW_STYLE))
    return shown


def test_a_style_can_be_made_from_the_menu_and_is_used_at_once(tab, monkeypatch):
    def fill(dialog):
        dialog.name_edit.setText("Release notes")
        dialog.prompt_edit.setPlainText("Short release-note bullets.")

    _choose_new_style(tab, monkeypatch, fill)

    assert _saved()[-1].name == "Release notes"
    assert tab.prompt_edit.toPlainText() == "Short release-note bullets."
    assert tab.preset_combo.currentText() == "Release notes"
    assert settings_manager.load_all_settings()[
        SettingsKey.WALKTHROUGH_WRITE_UP_INSTRUCTIONS] == "Short release-note bullets."
    assert tab.delete_style_button.isVisibleTo(tab.prompt_area)


def test_cancelling_a_new_style_leaves_the_prompt_and_menu_as_they_were(tab, monkeypatch):
    before = tab.prompt_edit.toPlainText()

    assert _choose_new_style(tab, monkeypatch)

    assert tab.prompt_edit.toPlainText() == before
    assert tab.preset_combo.currentText() == DEFAULT_STYLE.name
    assert len(_saved()) == 1 + len(STARTER_STYLES)


def test_a_new_style_that_cant_be_saved_says_why():
    dialog = NewStyleDialog()
    dialog.name_edit.setText("Notes")
    dialog._save()
    assert dialog.result() != NewStyleDialog.DialogCode.Accepted
    assert "instructions" in dialog.error.text()
    assert dialog.saved is None
    dialog.close()


def test_deleting_a_style_keeps_its_words_as_your_own(tab, monkeypatch):
    bug = STARTER_STYLES[1]
    tab.preset_combo.activated.emit(tab.preset_combo.findText(bug.name))
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *args: QMessageBox.StandardButton.Yes)

    tab.delete_style_button.click()

    assert bug.name not in _names(tab)
    assert bug.name not in [s.name for s in _saved()]
    assert tab.prompt_edit.toPlainText() == bug.instructions
    assert tab.preset_combo.currentText() == OWN_PROMPT


def test_the_default_style_cant_be_deleted(tab):
    assert tab.preset_combo.currentText() == DEFAULT_STYLE.name
    assert not tab.delete_style_button.isVisibleTo(tab.prompt_area)
