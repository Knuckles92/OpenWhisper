"""The floating Scratchpad: showing it, saving it, dictating into it, transforms."""
import gc
import logging
import os
import threading
import time
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from PyQt6.QtCore import QEvent, QRect, Qt
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QApplication, QAbstractButton

from config import config
from services import focus_context, text_rewrite, text_transforms
from services.settings import SettingsKey, settings_manager
from services.text_transforms import Transform
from ui_qt.widgets import scratchpad
from ui_qt.widgets.scratchpad import ScratchpadWindow

POLISH = Transform("polish", "Polish", "Improve the flow.")


@pytest.fixture
def ui():
    controller = SimpleNamespace(copy_to_clipboard=MagicMock(return_value=True))
    yield controller
    pad = getattr(controller, "_scratchpad", None)
    if pad is not None:
        pad.hide()
        pad.deleteLater()


@pytest.fixture
def pad(ui):
    scratchpad.toggle(ui)
    return ui._scratchpad


@pytest.fixture
def omarchy(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENWHISPER_UI", "omarchy")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def _focused(window, monkeypatch, active=True):
    monkeypatch.setattr(window, "isActiveWindow", lambda: active)


def _saved_text():
    return Path(config.SCRATCHPAD_FILE).read_bytes().decode("utf-8")


def _settle(pad, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not pad._saver.idle() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pad._saver.idle()


def _install_rewriter(monkeypatch, rewrite):
    """Stand in for rewrite_standalone, which returns (text, None) or ("", message)."""
    monkeypatch.setattr(text_rewrite, "rewrite_standalone", rewrite)


def _wait_for_transform(pad, timeout=3.0):
    deadline = time.monotonic() + timeout
    while pad.editor.isReadOnly() and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.01)
    assert not pad.editor.isReadOnly()


class TestWindow:
    def test_created_on_first_use_and_kept_on_the_controller(self, ui):
        assert not hasattr(ui, "_scratchpad")
        scratchpad.toggle(ui)
        pad = ui._scratchpad
        assert isinstance(pad, ScratchpadWindow)
        assert pad.isVisible()
        scratchpad.toggle(ui)
        assert ui._scratchpad is pad

    def test_classic_window_floats_above_without_quitting_the_app(self, pad):
        flags = pad.windowFlags()
        assert flags & Qt.WindowType.Tool
        assert flags & Qt.WindowType.FramelessWindowHint
        assert flags & Qt.WindowType.WindowStaysOnTopHint
        assert not pad.testAttribute(Qt.WidgetAttribute.WA_QuitOnClose)
        assert pad.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert pad.property("openwhisperHotkeyWindow") is True
        assert pad.header is not None and pad.size_grip is not None
        assert pad.editor.placeholderText() == "Dictate or type notes…"

    def test_toggle_hides_only_when_it_has_focus(self, ui, pad, monkeypatch):
        _focused(pad, monkeypatch, active=False)
        scratchpad.toggle(ui)
        assert pad.isVisible()
        _focused(pad, monkeypatch, active=True)
        scratchpad.toggle(ui)
        assert not pad.isVisible()

    def test_closing_only_hides(self, pad):
        pad.close()
        assert not pad.isVisible()
        pad.present()
        assert pad.isVisible()

    def test_on_top_can_be_turned_off_and_is_remembered(self, pad):
        pad.on_top_button.setChecked(False)
        assert not pad.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
        assert pad.isVisible()
        assert settings_manager.get(SettingsKey.SCRATCHPAD_ALWAYS_ON_TOP) is False

        again = ScratchpadWindow()
        try:
            assert not again.windowFlags() & Qt.WindowType.WindowStaysOnTopHint
            assert not again.on_top_button.isChecked()
        finally:
            again.deleteLater()

    def test_omarchy_gets_a_compositor_managed_window(self, omarchy, ui):
        scratchpad.toggle(ui)
        pad = ui._scratchpad
        flags = pad.windowFlags()
        assert flags & Qt.WindowType.Window
        assert not flags & Qt.WindowType.FramelessWindowHint
        assert not flags & Qt.WindowType.WindowCloseButtonHint
        assert pad.header is None and pad.size_grip is None and pad.on_top_button is None
        assert not pad.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        assert not pad.testAttribute(Qt.WidgetAttribute.WA_QuitOnClose)
        assert pad.property("openwhisperHotkeyWindow") is True
        pad.set_always_on_top(False)
        assert settings_manager.get(SettingsKey.SCRATCHPAD_ALWAYS_ON_TOP) is False


class TestGeometry:
    def test_saved_geometry_comes_back_clamped_to_the_screen(self, ui):
        available = QApplication.primaryScreen().availableGeometry()
        settings_manager.save_setting(SettingsKey.SCRATCHPAD_GEOMETRY, {
            "x": available.right() + 5000, "y": available.top() - 900, "width": 430, "height": 330,
        })
        scratchpad.toggle(ui)
        geometry = ui._scratchpad.geometry()
        assert (geometry.width(), geometry.height()) == (430, 330)
        assert available.contains(geometry)

    def test_moving_saves_the_geometry(self, pad):
        pad.setGeometry(QRect(120, 140, 410, 320))
        assert pad._geometry_timer.isActive()
        pad._geometry_timer.timeout.emit()
        assert settings_manager.get(SettingsKey.SCRATCHPAD_GEOMETRY) == {
            "x": 120, "y": 140, "width": 410, "height": 320,
        }

    def test_bad_saved_geometry_falls_back_to_a_default(self, ui):
        settings_manager.save_setting(SettingsKey.SCRATCHPAD_GEOMETRY, {"x": "left", "y": 1, "width": 2, "height": 3})
        scratchpad.toggle(ui)
        assert ui._scratchpad.width() >= ui._scratchpad.minimumSizeHint().width()


class TestSaving:
    def test_loads_saved_notes_on_first_show(self, ui):
        Path(config.SCRATCHPAD_FILE).write_bytes("Shopping: café crème\r\noat milk".encode("utf-8"))
        scratchpad.toggle(ui)
        pad = ui._scratchpad
        assert pad.text() == "Shopping: café crème\noat milk"
        assert pad.status_label.text() == "5 words"
        assert not pad._autosave_timer.isActive()

    def test_edits_autosave_atomically_as_utf8_with_lf(self, pad):
        pad.editor.insertPlainText("Line one ✓\nLine two")
        assert pad._autosave_timer.isActive()
        assert pad._autosave_timer.interval() == scratchpad.AUTOSAVE_MS
        pad._autosave_timer.timeout.emit()
        _settle(pad)
        assert _saved_text() == "Line one ✓\nLine two"
        assert b"\r\n" not in Path(config.SCRATCHPAD_FILE).read_bytes()
        leftovers = [name for name in os.listdir(os.path.dirname(config.SCRATCHPAD_FILE)) if name.endswith(".tmp")]
        assert leftovers == []

    def test_hiding_writes_pending_edits_at_once(self, pad):
        pad.editor.insertPlainText("Remember the milk")
        pad.hide()
        assert _saved_text() == "Remember the milk"

    def test_an_older_save_never_lands_over_a_newer_one(self, tmp_path):
        target = tmp_path / "notes.txt"
        saver = scratchpad._Saver(lambda: str(target))
        saver.write_now("newer")
        saver._write(1, "older")
        assert target.read_text(encoding="utf-8") == "newer"

    def test_nothing_is_written_before_the_notes_are_loaded(self):
        window = ScratchpadWindow()
        try:
            window.flush()
            assert not Path(config.SCRATCHPAD_FILE).exists()
        finally:
            window.deleteLater()


class TestDictation:
    def test_insert_needs_a_visible_focused_scratchpad(self, ui, monkeypatch):
        assert scratchpad.insert(ui, "hello") is False
        scratchpad.toggle(ui)
        pad = ui._scratchpad
        _focused(pad, monkeypatch, active=False)
        assert scratchpad.insert(ui, "hello") is False
        _focused(pad, monkeypatch, active=True)
        assert scratchpad.insert(ui, "") is False
        assert scratchpad.insert(ui, "hello") is True
        pad.hide()
        assert scratchpad.insert(ui, "hello") is False
        assert pad.text() == "hello"

    @pytest.mark.parametrize("before,after,dictated,expected", [
        ("Hello", "", "world", "Hello world"),
        ("Hello ", "", "world", "Hello world"),
        ("", "", "First note", "First note"),
        ("Call (", ")", "Sam", "Call (Sam)"),
        ("Done", "", ".", "Done."),
        ("Start ", "end", "middle", "Start middle end"),
        ("Line\n", "", "next", "Line\nnext"),
    ])
    def test_dictation_lands_at_the_cursor_with_sensible_spacing(
            self, pad, monkeypatch, before, after, dictated, expected):
        pad.editor.setPlainText(before + after)
        cursor = pad.editor.textCursor()
        cursor.setPosition(len(before))
        pad.editor.setTextCursor(cursor)
        _focused(pad, monkeypatch)
        assert scratchpad.insert(SimpleNamespace(_scratchpad=pad), dictated) is True
        assert pad.text() == expected
        assert pad._autosave_timer.isActive()

    def test_dictation_replaces_a_selection(self, pad, monkeypatch):
        pad.editor.setPlainText("Meet at noon today")
        cursor = pad.editor.textCursor()
        cursor.setPosition(8)
        cursor.setPosition(12, QTextCursor.MoveMode.KeepAnchor)
        pad.editor.setTextCursor(cursor)
        _focused(pad, monkeypatch)
        scratchpad.insert(SimpleNamespace(_scratchpad=pad), "three")
        assert pad.text() == "Meet at three today"

    def test_the_join_step_sees_the_text_around_the_cursor(self, pad, monkeypatch):
        seen = []

        def join(text, context):
            seen.append(context)
            return text.lower()

        monkeypatch.setattr(focus_context, "join_with_context", join)
        pad.editor.setPlainText("Emoji 😀 before")
        pad.editor.moveCursor(QTextCursor.MoveOperation.End)
        _focused(pad, monkeypatch)
        scratchpad.insert(SimpleNamespace(_scratchpad=pad), "And After")
        assert seen[0].before == "Emoji 😀 before"
        assert seen[0].after == "" and seen[0].selected == ""
        assert seen[0].caret_known and seen[0].selection_known
        assert pad.text() == "Emoji 😀 before and after"


class TestFooter:
    def test_word_count_follows_the_text(self, pad):
        assert pad.status_label.text() == ""
        pad.editor.setPlainText("one")
        assert pad.status_label.text() == "1 word"
        pad.editor.setPlainText("one two  three\nfour")
        assert pad.status_label.text() == "4 words"

    def test_copy_all_uses_the_apps_clipboard_helper(self, ui, pad):
        pad.copy_all()
        assert pad.status_label.text() == "Nothing to copy yet"
        ui.copy_to_clipboard.assert_not_called()
        pad.editor.setPlainText("Keep this")
        pad.copy_all()
        ui.copy_to_clipboard.assert_called_once_with("Keep this")
        assert pad.status_label.text() == "Copied"
        pad._notice_timer.timeout.emit()
        assert pad.status_label.text() == "2 words"

    def test_clear_has_one_step_undo(self, pad):
        pad.editor.setPlainText("Important thought")
        pad.clear_button.click()
        assert pad.text() == ""
        assert pad.clear_button.text() == "Undo clear"
        assert pad._undo_timer.isActive()
        pad.clear_button.click()
        assert pad.text() == "Important thought"
        assert pad.clear_button.text() == "Clear"

    def test_typing_after_clear_drops_the_undo(self, pad):
        pad.editor.setPlainText("Old")
        pad.clear_text()
        pad.editor.insertPlainText("New")
        assert pad.clear_button.text() == "Clear"
        pad.clear_button.click()
        assert pad.text() == ""

    def test_undo_offer_expires(self, pad):
        pad.editor.setPlainText("Old")
        pad.clear_text()
        pad._undo_timer.timeout.emit()
        assert pad.clear_button.text() == "Clear"
        assert pad._cleared_text is None


class TestTransforms:
    def test_menu_lists_saved_transforms(self, pad, monkeypatch):
        monkeypatch.setattr(text_transforms, "load_transforms", lambda settings: [POLISH])
        pad.fill_transform_menu()
        assert [action.text() for action in pad.transform_menu.actions()] == ["Polish"]

    def test_menu_says_when_there_are_none(self, pad, monkeypatch):
        monkeypatch.setattr(text_transforms, "load_transforms", lambda settings: [])
        pad.fill_transform_menu()
        actions = pad.transform_menu.actions()
        assert [action.text() for action in actions] == ["No transforms yet"]
        assert not actions[0].isEnabled()

    def test_transform_rewrites_everything_off_the_qt_thread(self, pad, monkeypatch, caplog):
        import threading

        threads = []

        def rewrite(text, instruction, settings):
            threads.append(threading.current_thread() is threading.main_thread())
            assert instruction == POLISH.instruction
            return text.upper(), None

        _install_rewriter(monkeypatch, rewrite)
        pad.editor.setPlainText("private draft words")
        with caplog.at_level(logging.DEBUG):
            pad.apply_transform(POLISH)
            assert pad.editor.isReadOnly()
            assert not pad.transform_button.isEnabled()
            _wait_for_transform(pad)
        assert threads == [False]
        assert pad.text() == "PRIVATE DRAFT WORDS"
        assert pad.status_label.text().startswith("Polish applied")
        assert pad.transform_button.isEnabled()
        assert "private draft" not in caplog.text
        pad.editor.undo()
        assert pad.text() == "private draft words"

    def test_transform_rewrites_only_the_selection(self, pad, monkeypatch):
        _install_rewriter(monkeypatch, lambda text, instruction, settings: (f"[{text}]", None))
        pad.editor.setPlainText("keep this but change that")
        cursor = pad.editor.textCursor()
        cursor.setPosition(14)
        cursor.setPosition(25, QTextCursor.MoveMode.KeepAnchor)
        pad.editor.setTextCursor(cursor)
        pad.apply_transform(POLISH)
        _wait_for_transform(pad)
        assert pad.text() == "keep this but [change that]"

    def test_a_failed_transform_leaves_the_text(self, pad, monkeypatch):
        def rewrite(text, instruction, settings):
            return "", "Set up AI cleanup to rewrite text"

        _install_rewriter(monkeypatch, rewrite)
        pad.editor.setPlainText("as it was")
        pad.apply_transform(POLISH)
        _wait_for_transform(pad)
        assert pad.text() == "as it was"
        assert pad.status_label.text() == "Set up AI cleanup to rewrite text"

    def test_nothing_to_transform(self, pad, monkeypatch):
        _install_rewriter(monkeypatch, MagicMock())
        pad.apply_transform(POLISH)
        assert pad.status_label.text() == "Write or dictate something to transform"
        assert not pad.editor.isReadOnly()

    def test_a_window_deleted_mid_transform_is_not_held_by_the_worker(self, monkeypatch):
        # The worker used to capture the window: it kept a deleted window
        # alive, then dropped the last reference (deleting a QWidget) or
        # emitted on it from its own thread, which can crash the process.
        gate = threading.Event()

        def rewrite(text, instruction, settings):
            gate.wait(5)
            return text.upper(), None

        _install_rewriter(monkeypatch, rewrite)
        window = ScratchpadWindow()
        window.editor.setPlainText("private draft words")
        window.apply_transform(POLISH)
        workers = [t for t in threading.enumerate() if t.name == "scratchpad-transform"]
        ref = weakref.ref(window)
        window.deleteLater()
        del window
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        # PyQt frees the window's own lambda connections with a queued call
        # and then a deferred delete; flush both.
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
        gc.collect()
        assert ref() is None
        gate.set()
        for worker in workers:
            worker.join(5)
            assert not worker.is_alive()
        QApplication.processEvents()


def test_the_app_opens_it_and_dictation_lands_in_it(monkeypatch):
    from ui_qt.ui_controller import UIController

    app_ui = UIController()
    try:
        app_ui.main_window.scratchpad_action.trigger()
        pad = app_ui._scratchpad
        assert pad.isVisible()
        assert pad._copy_text == app_ui.copy_to_clipboard
        _focused(pad, monkeypatch)
        assert app_ui.insert_into_scratchpad("From the hotkey") is True
        assert pad.text() == "From the hotkey"
        pad.hide()
        assert app_ui.insert_into_scratchpad("Elsewhere") is False
    finally:
        pad = getattr(app_ui, "_scratchpad", None)
        if pad is not None:
            pad.deleteLater()
        app_ui.cleanup()


@pytest.mark.parametrize("ui_mode", ["classic", "omarchy"])
def test_footer_fits_a_narrow_window_at_large_fonts(monkeypatch, tmp_path, ui_mode):
    from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
    from ui_qt.utils.palette import current_palette, set_current_palette
    from ui_qt.utils.theme_manager import ThemeManager

    monkeypatch.setenv("OPENWHISPER_UI", ui_mode)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    app = QApplication.instance()
    previous_style, previous_font = app.styleSheet(), app.font()
    previous_scale, previous_palette = current_ui_font_scale_percent(), current_palette()
    window = None
    try:
        apply_ui_font_scale(130, app=app, theme_manager=ThemeManager())
        window = ScratchpadWindow()
        window.present()
        window.resize(300, 260)
        for _ in range(8):
            app.processEvents()
        assert window.width() <= 460
        for button in window.findChildren(QAbstractButton):
            if button.isVisible():
                assert button.mapTo(window, button.rect().bottomRight()).x() < window.width()
                assert button.mapTo(window, button.rect().topLeft()).x() >= 0
        assert window.editor.height() > 40
    finally:
        if window is not None:
            window.hide()
            window.deleteLater()
        apply_ui_font_scale(previous_scale, app=app)
        set_current_palette(previous_palette)
        app.setFont(previous_font)
        app.setStyleSheet(previous_style)
