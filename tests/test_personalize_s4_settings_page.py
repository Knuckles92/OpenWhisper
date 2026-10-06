"""Settings → Personalize → Dictionary: adding, starring, editing, learning and layout."""
import os
import tempfile
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QAbstractButton, QApplication, QLineEdit, QPushButton

from services import dictionary
from services.settings import SettingsKey, SettingsManager, SettingsView
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs import settings_dictionary as page_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.dialogs import settings_metadata
from ui_qt.dialogs import settings_models as models_module
from ui_qt.dialogs.settings_destinations import BASIC_DICTATION, DICTIONARY, STYLES
from ui_qt.widgets.setting_tile import TileBase

KEY = SettingsKey.DICTATION_DICTIONARY
WORDS = [
    {"id": "a", "term": "Ksenia", "starred": True, "heard": ["Sonia"], "learned": False, "new": False},
    {"id": "b", "term": "Oluwaseun", "starred": False, "heard": [], "learned": True, "new": True},
    {"id": "c", "term": "Kubernetes", "starred": False, "heard": ["cooper netties"], "learned": False, "new": False},
    {"id": "d", "term": "Siobhan Ó Floinn-MacNamara of the Very Long Surname Company",
     "starred": False, "heard": ["shivon", "shavaun"], "learned": False, "new": False},
]


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
    page_module.set_backend_provider(None)


@pytest.fixture
def make_dialog():
    stacks = []
    temp = tempfile.TemporaryDirectory()

    def build(values=None):
        store = SettingsManager(os.path.join(temp.name, f"settings{len(stacks)}.json"))
        store.save_all_settings({SettingsKey.SETTINGS_VIEW: SettingsView.ADVANCED,
                                 SettingsKey.SELECTED_MODEL: "parakeet", **(values or {})})
        stack = ExitStack()
        for module in (settings_dialog_module, models_module, downloads_module):
            stack.enter_context(patch.object(module, "settings_manager", store))
        stack.enter_context(patch.object(settings_dialog_module.history_manager, "set_retention"))
        for module in (models_module, downloads_module):
            stack.enter_context(patch.object(module, "scan_cached_models", return_value={}))
        stack.enter_context(patch("services.local_asr.cache.inventory", return_value={}))
        stacks.append(stack)
        dialog = settings_dialog_module.SettingsDialog(get_loaded_model=lambda: None,
                                                       background_cache_scan=False)
        dialog.on_settings_changed = Mock()
        return dialog, store

    yield build
    for stack in reversed(stacks):
        stack.close()
    temp.cleanup()


def saved(store):
    return dictionary.load_dictionary(store.load_all_settings())


def open_page(dialog):
    dialog.select_destination(DICTIONARY)
    return dialog._dictionary_page


class TestComposer:
    def test_enter_adds_a_word_with_its_variants(self, make_dialog):
        dialog, store = make_dialog()
        page = open_page(dialog)
        assert dialog.rail.value(DICTIONARY) == "No words yet"
        dialog.dictionary_term_edit.setText("  Ksenia ")
        dialog.dictionary_heard_edit.setText("Sonia, Senya")
        QTest.keyClick(dialog.dictionary_heard_edit, Qt.Key.Key_Return)
        (word,) = saved(store)
        assert (word.term, word.heard) == ("Ksenia", ("Sonia", "Senya"))
        assert dialog.dictionary_message.text() == "Added “Ksenia”."
        assert dialog.dictionary_message.property("tone") == "ok"
        assert dialog.dictionary_term_edit.text() == dialog.dictionary_heard_edit.text() == ""
        assert page.library.visible_ids == [word.id]
        assert dialog.rail.value(DICTIONARY) == "1 word"
        dialog.on_settings_changed.assert_called_with("dictionary")

    @pytest.mark.parametrize("term,message", [("", "Type a word"), ("Ksenia", "already")])
    def test_problems_are_explained_and_nothing_changes(self, make_dialog, term, message):
        dialog, store = make_dialog({KEY: WORDS[:1]})
        open_page(dialog)
        dialog.dictionary_term_edit.setText(term)
        dialog.dictionary_add_button.click()
        assert message in dialog.dictionary_message.text()
        assert dialog.dictionary_message.property("tone") == "error"
        assert [word.term for word in saved(store)] == ["Ksenia"]
        dialog.dictionary_term_edit.setText("Ksenia2")
        QTest.keyClick(dialog.dictionary_term_edit, Qt.Key.Key_A)
        assert dialog.dictionary_message.isHidden()

    def test_a_failed_save_says_so(self, make_dialog, monkeypatch):
        dialog, store = make_dialog()
        open_page(dialog)
        monkeypatch.setattr(store, "mutate_settings", Mock(side_effect=OSError("disk full")))
        dialog.dictionary_term_edit.setText("Ksenia")
        dialog.dictionary_add_button.click()
        assert dialog.dictionary_message.text() == "Couldn't add the word. Try again."

    def test_editing_a_word(self, make_dialog):
        dialog, store = make_dialog({KEY: WORDS})
        page = open_page(dialog)
        page.library.row("c").edit.click()
        assert dialog.dictionary_composer_tile.title_label.text() == "Edit a word"
        assert dialog.dictionary_term_edit.text() == "Kubernetes"
        assert dialog.dictionary_heard_edit.text() == "cooper netties"
        assert dialog.dictionary_add_button.text() == "Save"
        assert not dialog.dictionary_cancel_button.isHidden()
        dialog.dictionary_term_edit.setText("K8s")
        dialog.dictionary_heard_edit.setText("kates")
        dialog.dictionary_add_button.click()
        edited = next(word for word in saved(store) if word.id == "c")
        assert (edited.term, edited.heard) == ("K8s", ("kates",))
        assert dialog.dictionary_message.text() == "Saved “K8s”."
        assert dialog.dictionary_add_button.text() == "Add"
        assert dialog.dictionary_cancel_button.isHidden()

    def test_cancelling_an_edit(self, make_dialog):
        dialog, store = make_dialog({KEY: WORDS})
        page = open_page(dialog)
        page.library.row("a").edit.click()
        dialog.dictionary_cancel_button.click()
        assert dialog.dictionary_term_edit.text() == ""
        assert dialog.dictionary_composer_tile.title_label.text() == "Add a word"
        dialog.dictionary_term_edit.setText("Olu")
        dialog.dictionary_add_button.click()
        assert [word.term for word in saved(store)][0] == "Olu"


class TestLibrary:
    def test_rows_show_stars_badges_and_variants(self, make_dialog):
        dialog, _store = make_dialog({KEY: WORDS})
        page = open_page(dialog)
        assert page.library.visible_ids == ["a", "b", "c", "d"]
        ksenia, olu = page.library.row("a"), page.library.row("b")
        assert ksenia.star.isChecked() and ksenia.star.property("starred") is True
        assert ksenia.detail.text() == "Sounds like Sonia"
        assert ksenia.learned_badge.isHidden() and ksenia.remove.text() == "Remove"
        assert not olu.learned_badge.isHidden() and not olu.new_badge.isHidden()
        assert olu.remove.text() == "Undo"
        assert dialog.rail.value(DICTIONARY) == "4 words · 1 new"
        assert not dialog.dictionary_new_tile.isHidden()
        assert "1 new word" in dialog.dictionary_new_tile.description_label.text()

    def test_star_remove_and_undo(self, make_dialog):
        dialog, store = make_dialog({KEY: WORDS})
        page = open_page(dialog)
        page.library.row("c").star.click()
        assert next(word for word in saved(store) if word.id == "c").starred
        assert page.library.row("c").star.property("starred") is True
        page.library.row("b").remove.click()
        assert dialog.dictionary_message.text() == "Forgot “Oluwaseun”."
        page.library.row("d").remove.click()
        assert [word.id for word in saved(store)] == ["a", "c"]
        assert page.library.visible_ids == ["a", "c"]
        assert dialog.dictionary_new_tile.isHidden()

    def test_keep_all_clears_the_new_badges(self, make_dialog):
        dialog, store = make_dialog({KEY: WORDS})
        page = open_page(dialog)
        dialog.findChild(QPushButton, "dictionaryKeepNewButton").click()
        assert not any(word.new for word in saved(store))
        assert saved(store)[1].learned
        assert dialog.dictionary_new_tile.isHidden()
        assert page.library.row("b").remove.text() == "Remove"
        assert dialog.rail.value(DICTIONARY) == "4 words"

    def test_a_long_dictionary_is_capped_and_searchable(self, make_dialog):
        words = [{"id": f"w{i}", "term": f"Word{i:03d}"} for i in range(130)]
        dialog, _store = make_dialog({KEY: words})
        page = open_page(dialog)
        library = page.library
        assert len(library.visible_ids) == library.MAX_ROWS
        assert library.more.text() == "Showing 100 of 130. Search to find the rest."
        assert not library.search.isHidden()
        library.search.setText("word12")
        assert library.visible_ids == [f"w{i}" for i in range(120, 130)]
        assert library.more.isHidden()
        library.search.setText("zzz")
        assert library.visible_ids == [] and "No words match" in library.empty.text()

    def test_empty_dictionary(self, make_dialog):
        dialog, _store = make_dialog()
        page = open_page(dialog)
        assert page.library.search.isHidden()
        assert not page.library.empty.isHidden() and "No words yet" in page.library.empty.text()
        assert page.count.isHidden()


class TestRecognitionAndLearning:
    @pytest.mark.parametrize("engine,expected", [
        ("parakeet", "Your engine (Parakeet) uses your dictionary after recognition."),
        ("local_whisper", "Your engine (Whisper) also steers the speech model toward your words."),
        ("api", "Your engine (OpenAI) also steers the speech model toward your words. "
                "They're sent to OpenAI with your audio."),
        ("remote", page_module._GENERIC_ENGINE_LINE),
    ])
    def test_engine_line(self, engine, expected):
        assert page_module.engine_line({SettingsKey.SELECTED_MODEL: engine}) == expected

    def test_a_connected_host_says_whether_hints_reach_it(self):
        host = SimpleNamespace(is_remote=True, is_available=lambda: True, recognition_support="model",
                               name="Whisper turbo on devbox", host_name="devbox")
        line = page_module.engine_line({SettingsKey.SELECTED_MODEL: "remote"}, host)
        assert line == ("Your engine (Whisper turbo on devbox) also steers the speech model "
                        "toward your words. They're sent to devbox with your audio.")
        host.recognition_support = "after"
        assert "after recognition" in page_module.engine_line({SettingsKey.SELECTED_MODEL: "remote"}, host)

    def test_the_page_reads_the_live_backend(self, make_dialog):
        host = SimpleNamespace(is_remote=True, is_available=lambda: True, recognition_support="model",
                               name="Whisper on devbox", host_name="devbox")
        page_module.set_backend_provider(lambda: host)
        dialog, _store = make_dialog({SettingsKey.SELECTED_MODEL: "remote"})
        open_page(dialog)
        assert "sent to devbox" in dialog.dictionary_steer_tile.description_label.text()

    def test_steering_switch(self, make_dialog):
        dialog, store = make_dialog()
        open_page(dialog)
        assert dialog.dictionary_steer_switch.isChecked()
        dialog.dictionary_steer_switch.click()
        assert store.load_all_settings()[SettingsKey.DICTIONARY_STEER_RECOGNITION] is False
        assert dialog.dictionary_steer_tile.description_label.text() == page_module._STEER_OFF_LINE
        assert dialog.dictionary_steer_tile.property("checked") is False

    def test_learning_waits_for_text_reading(self, make_dialog):
        dialog, store = make_dialog()
        open_page(dialog)
        assert dialog.dictionary_learn_tile.isHidden()
        assert not dialog.dictionary_learn_gate_tile.isHidden()
        assert dialog.dictionary_learn_gate_tile.description_label.text() == "Off · needs Read text near the cursor"
        dialog.findChild(QPushButton, "dictionaryLearnTurnOnButton").click()
        assert dialog.rail.current_key() == STYLES
        store.update_settings({SettingsKey.APP_CONTEXT_READ_TEXT: True})
        dialog.show()
        try:
            dialog.select_destination(DICTIONARY)
            QApplication.processEvents()
            assert not dialog.dictionary_learn_tile.isHidden()
            assert dialog.dictionary_learn_gate_tile.isHidden()
            dialog.dictionary_learn_switch.setChecked(False)
            assert store.load_all_settings()[SettingsKey.DICTIONARY_LEARN_ENABLED] is False
        finally:
            dialog.close()

    def test_loading_never_writes(self, make_dialog):
        dialog, store = make_dialog({SettingsKey.DICTIONARY_STEER_RECOGNITION: False})
        open_page(dialog)
        before = store.load_all_settings()
        dialog._dictionary_page.show_settings(before)
        dialog.refresh_page(DICTIONARY)
        assert store.load_all_settings() == before
        assert not dialog.dictionary_steer_switch.isChecked()


class TestSpellingRules:
    def test_moving_spelling_rules_updates_the_rules_page(self, make_dialog):
        rules = ['Spell "jon" as "John".', "Use British spelling."]
        dialog, store = make_dialog({SettingsKey.TRANSCRIPT_CLEANUP_RULES: rules})
        dialog.ensure_page("cleanup_rules")
        open_page(dialog)
        assert not dialog.dictionary_rules_tile.isHidden()
        button = dialog.findChild(QPushButton, "dictionaryMoveRulesButton")
        assert button.text() == "Move it here"
        button.click()
        assert store.load_all_settings()[SettingsKey.TRANSCRIPT_CLEANUP_RULES] == ["Use British spelling."]
        assert [dialog.cleanup_rules_list.item(i).text() for i in range(dialog.cleanup_rules_list.count())] == [
            "Use British spelling."
        ]
        assert [(word.term, word.heard) for word in saved(store)] == [("John", ("jon",))]
        assert dialog.dictionary_rules_tile.isHidden()
        assert dialog.dictionary_message.text() == "Moved 1 spelling rule here."


class TestBasicRow:
    def test_basic_shows_the_count_and_opens_the_composer(self, make_dialog):
        dialog, store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC, KEY: WORDS})
        dialog.show()
        try:
            QApplication.processEvents()
            page = dialog._basic_pages[BASIC_DICTATION]
            button = page.findChild(QPushButton, "basicDictionaryAddButton")
            assert button.text() == "Add word"
            labels = [label.text() for label in page.findChildren(type(page.cleanup_status))]
            assert "4 words · 1 new" in labels
            assert DICTIONARY not in dialog._built_pages
            button.click()
            for _ in range(5):
                QApplication.processEvents()
            assert dialog.rail.current_key() == DICTIONARY
            assert dialog.dictionary_term_edit.hasFocus()
        finally:
            dialog.close()

    def test_basic_row_without_words(self, make_dialog):
        dialog, _store = make_dialog({SettingsKey.SETTINGS_VIEW: SettingsView.BASIC})
        page = dialog._basic_pages[BASIC_DICTATION]
        labels = [label.text() for label in page.findChildren(type(page.cleanup_status))]
        assert "Names and terms dictation should always get right." in labels


@pytest.mark.parametrize("ui_mode,width", [("classic", 940), ("omarchy", 560)])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_page_fits_narrow_windows_at_large_fonts(make_dialog, monkeypatch, ui_mode, width, theme):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
    dialog = None
    try:
        apply_ui_font_scale(130, app=app, theme_manager=ThemeManager(theme))
        dialog, _store = make_dialog({
            KEY: WORDS, SettingsKey.TRANSCRIPT_CLEANUP_RULES: ['Always spell my name "Alex Rivera"'],
        })
        dialog.show()
        dialog.resize(width, 600)
        page_state = open_page(dialog)
        page_state.library.row("a").edit.click()
        for _ in range(10):
            app.processEvents()
        page = dialog._pages[DICTIONARY]
        assert dialog.width() == width
        controls = (page.findChildren(QAbstractButton) + page.findChildren(QLineEdit))
        for control in controls:
            if control.isVisible():
                assert control.mapTo(page, control.rect().topLeft()).x() >= 0, control.objectName()
                assert control.mapTo(page, control.rect().bottomRight()).x() < page.width(), control.objectName()
        for tile in page.findChildren(TileBase):
            if tile.isHidden():
                continue
            for label in (tile.title_label, tile.description_label):
                assert label.height() >= label.heightForWidth(label.width())
            assert page.rect().contains(tile.geometry())
        rows = [page_state.library.row(word["id"]) for word in WORDS]
        assert all(row.layout_row.stacked for row in rows) == (ui_mode == "omarchy")
        scroll = dialog._page_scrolls[DICTIONARY]
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        app.processEvents()
        assert page.mapTo(scroll.viewport(), page.rect().bottomRight()).y() <= scroll.viewport().height()
    finally:
        if dialog is not None:
            dialog.close()
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)
