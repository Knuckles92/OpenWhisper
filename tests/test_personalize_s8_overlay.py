"""The dictation overlay's focus safety, language chip, hands-free badge and captions."""
import os
import subprocess
import sys
import textwrap
from unittest.mock import patch

import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QWidget

from services import dictation_language
from services.settings import SettingsKey, settings_manager
from ui_qt.overlays import waveform_overlay
from ui_qt.overlays.waveform_overlay import WaveformOverlay
from ui_qt.utils.palette import DARK_PALETTE, LIGHT_PALETTE, current_palette, set_current_palette

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def overlay():
    widget = WaveformOverlay()
    yield widget
    widget.hide()
    widget.close()


@pytest.fixture
def two_languages():
    settings_manager.update_settings({
        SettingsKey.SELECTED_MODEL: "local_whisper",
        SettingsKey.DICTATION_LANGUAGES: ["en", "es"],
        SettingsKey.DICTATION_ACTIVE_LANGUAGE: "es",
    })


@pytest.fixture
def clickable(monkeypatch):
    monkeypatch.setattr(waveform_overlay, "_platform_takes_overlay_clicks", lambda: True)


def _paint(widget):
    with patch.object(waveform_overlay.logger, "error") as error:
        widget.grab()
    error.assert_not_called()


def _chip_center(widget) -> QPoint:
    _paint(widget)
    return widget._chip_rect.center().toPoint()


class TestFocus:
    def test_standalone_overlay_never_takes_focus(self, overlay):
        flags = overlay.windowFlags()
        assert flags & Qt.WindowType.WindowDoesNotAcceptFocus
        assert flags & Qt.WindowType.Tool
        assert overlay.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        assert overlay.focusPolicy() == Qt.FocusPolicy.NoFocus

    def test_embedded_overlay_ignores_the_mouse(self):
        host = QWidget()
        embedded = WaveformOverlay(host)
        try:
            assert embedded.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            assert not embedded.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
        finally:
            host.close()

    @pytest.mark.skipif(sys.platform != "win32", reason="WS_EX_NOACTIVATE is a Windows window style")
    def test_native_window_is_non_activating_on_windows(self, tmp_path):
        script = textwrap.dedent("""
            import ctypes, sys
            sys.path.insert(0, sys.argv[1])
            from PyQt6.QtWidgets import QApplication
            app = QApplication([])
            from ui_qt.overlays.waveform_overlay import WaveformOverlay
            overlay = WaveformOverlay()
            hwnd = int(overlay.winId())
            get = ctypes.windll.user32.GetWindowLongPtrW
            get.restype = ctypes.c_ssize_t
            get.argtypes = (ctypes.c_void_p, ctypes.c_int)
            print(get(hwnd, -20) & 0x08000000)
        """)
        env = {**os.environ, "QT_QPA_PLATFORM": "windows", "OPENWHISPER_DATA_DIR": str(tmp_path)}
        result = subprocess.run(
            [sys.executable, "-c", script, PROJECT_ROOT],
            capture_output=True, text=True, timeout=60, env=env, cwd=str(tmp_path),
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip().splitlines()[-1] == str(0x08000000)


class TestLanguageChip:
    def test_chip_shows_the_saved_language_when_recording_starts(self, overlay, two_languages):
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        assert overlay._chip_visible()
        assert overlay._language == "es"
        assert overlay._language_choices == ("en", "es")
        _paint(overlay)
        assert not overlay._chip_rect.isEmpty()
        assert overlay._chip_rect.right() > overlay.width() / 2

    @pytest.mark.parametrize("languages", [[], ["en"], ["en", "de"]])
    def test_no_chip_with_one_or_no_language_the_engine_takes(self, overlay, languages):
        settings_manager.update_settings({
            SettingsKey.SELECTED_MODEL: "moonshine",
            SettingsKey.DICTATION_LANGUAGES: languages,
        })
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        _paint(overlay)
        assert not overlay._chip_visible()
        assert overlay._chip_rect.isEmpty()

    def test_chip_only_while_listening(self, overlay, two_languages):
        overlay.show_at_cursor(overlay.STATE_STREAMING)
        assert overlay._chip_visible()
        overlay.set_state(overlay.STATE_PROCESSING)
        _paint(overlay)
        assert overlay._chip_rect.isEmpty()

    def test_clicking_the_chip_asks_to_switch(self, overlay, two_languages, clickable):
        spy = QSignalSpy(overlay.language_cycle_requested)
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(20, 60))
        assert len(spy) == 0
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=_chip_center(overlay))
        assert len(spy) == 1

    def test_chip_is_display_only_where_clicks_could_take_focus(self, overlay, two_languages, monkeypatch):
        monkeypatch.setattr(waveform_overlay, "_platform_takes_overlay_clicks", lambda: False)
        spy = QSignalSpy(overlay.language_cycle_requested)
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=_chip_center(overlay))
        assert len(spy) == 0
        assert not overlay._chip_clickable()

    def test_chip_is_display_only_when_embedded(self, two_languages, clickable):
        host = QWidget()
        host.resize(600, 400)
        host.show()
        embedded = WaveformOverlay(host)
        try:
            embedded.show_at_cursor(embedded.STATE_RECORDING)
            assert embedded._chip_visible()
            assert not embedded._chip_clickable()
        finally:
            host.close()

    def test_hover_shows_a_pointing_hand(self, overlay, two_languages, clickable):
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        QTest.mouseMove(overlay, _chip_center(overlay))
        assert overlay._chip_hover
        assert overlay.cursor().shape() == Qt.CursorShape.PointingHandCursor
        overlay.set_state(overlay.STATE_PROCESSING)
        assert not overlay._chip_hover

    def test_switching_while_shown_flashes_the_chip(self, overlay, two_languages):
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        assert overlay._language_flash() == 0.0
        overlay.set_language("en", ["en", "es"])
        assert overlay._language == "en"
        assert overlay._language_flash() > 0.5
        _paint(overlay)

    def test_hidden_overlay_takes_a_new_language_quietly(self, overlay):
        overlay.set_language("de", ["en", "de"])
        assert not overlay.isVisible()
        assert overlay._language_flash() == 0.0

    def test_dictating_right_after_a_notice_keeps_the_overlay(self, overlay, two_languages):
        overlay.set_language("es", ["en", "es"])
        overlay.show_language_notice()
        assert overlay.hidden_timer.isActive()
        overlay.set_state(overlay.STATE_RECORDING)
        assert not overlay.hidden_timer.isActive()
        assert overlay.isVisible()

    def test_language_notice_shows_then_hides_itself(self, overlay, two_languages):
        overlay.set_language("es", ["en", "es"])
        overlay.show_at_cursor(overlay.STATE_LANGUAGE)
        assert overlay.hidden_timer.isActive()
        _paint(overlay)
        overlay.hidden_timer.timeout.emit()
        assert not overlay.isVisible()
        assert overlay._language == ""


class TestHandsFree:
    def test_latch_lasts_for_the_recording(self, overlay):
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        overlay.set_hands_free(True)
        assert overlay.hands_free
        overlay.set_state(overlay.STATE_STREAMING)
        assert overlay.hands_free
        _paint(overlay)
        overlay.set_state(overlay.STATE_PROCESSING)
        assert not overlay.hands_free

    def test_hide_and_a_new_recording_clear_it(self, overlay):
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        overlay.set_hands_free(True)
        overlay.hide()
        assert not overlay.hands_free

        overlay.set_hands_free(True)
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        assert not overlay.hands_free

    def test_latch_is_remembered_while_hidden(self, overlay):
        overlay.set_hands_free(True)
        assert overlay.hands_free and not overlay.isVisible()


class TestCaption:
    def test_caption_shows_for_a_moment(self, overlay):
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        overlay.show_caption("Switched to Laptop mic")
        assert overlay._caption == "Switched to Laptop mic"
        assert overlay._caption_timer.isActive()
        assert overlay._caption_timer.interval() == waveform_overlay.CAPTION_MS
        _paint(overlay)
        overlay._caption_timer.timeout.emit()
        assert overlay._caption == ""

    def test_caption_is_cleared_by_hide_and_by_the_recording_ending(self, overlay):
        overlay.show_at_cursor(overlay.STATE_RECORDING)
        overlay.show_caption("Switched to Laptop mic")
        overlay.hide()
        assert overlay._caption == "" and not overlay._caption_timer.isActive()

        overlay.show_at_cursor(overlay.STATE_STREAMING)
        overlay.show_caption("Switched to Laptop mic")
        overlay.set_state(overlay.STATE_TRANSCRIBING)
        assert overlay._caption == ""

    def test_hidden_overlay_ignores_captions(self, overlay):
        overlay.show_caption("Switched to Laptop mic")
        assert overlay._caption == ""
        assert not overlay.isVisible()


class TestCommandStates:
    def test_command_listening_records_with_particles_and_a_preview(self, overlay):
        overlay.show_at_cursor(overlay.STATE_COMMAND_LISTENING)
        overlay.update_audio_levels([0.6] * 20)
        for _ in range(5):
            overlay.last_frame_time -= 0.05
            overlay._update_animation()
        assert overlay.style.particles
        _paint(overlay)

        overlay.update_streaming_text("make this friendlier and shorter please " * 4)
        assert overlay.height() > overlay._base_height
        _paint(overlay)
        overlay.set_state(overlay.STATE_REWRITING)
        assert overlay.height() == overlay._base_height
        assert overlay._streaming_preview_text == ""

    def test_rewriting_shimmers_like_cleanup(self, overlay):
        overlay.show_at_cursor(overlay.STATE_REWRITING)
        overlay._update_animation()
        assert overlay.timer.isActive()
        _paint(overlay)

    @pytest.mark.parametrize("palette", [DARK_PALETTE, LIGHT_PALETTE])
    def test_every_new_look_paints_in_both_themes(self, overlay, two_languages, palette):
        previous = current_palette()
        set_current_palette(palette)
        try:
            for state in (overlay.STATE_RECORDING, overlay.STATE_STREAMING,
                          overlay.STATE_COMMAND_LISTENING, overlay.STATE_REWRITING,
                          overlay.STATE_LANGUAGE):
                overlay.show_at_cursor(state)
                overlay.set_hands_free(True)
                overlay.show_caption("Switched to Built-in microphone")
                _paint(overlay)
                overlay.hide()
        finally:
            set_current_palette(previous)


def test_overlay_uses_the_language_module_for_its_chip_text(overlay, monkeypatch):
    monkeypatch.setattr(dictation_language, "short_label", lambda code: f"<{code}>")
    overlay.set_language("de", ["en", "de"])
    assert overlay._language_text() == "<de>"
