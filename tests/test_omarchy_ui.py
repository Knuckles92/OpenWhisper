"""Regressions for native tiled geometry, palette swaps, and Wayland hooks."""

from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest
from PyQt6.QtCore import QPoint, QSize, Qt
from PyQt6.QtWidgets import QApplication, QPushButton

from services import desktop_session
from services.settings import SettingsKey, UiTheme, resolve_ui_theme, settings_manager
from ui_qt.utils.omarchy_theme import load_omarchy_palette, palette_from_colors
from ui_qt.utils.palette import DARK_PALETTE


@pytest.fixture
def omarchy(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENWHISPER_UI", "omarchy")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return desktop_session.omarchy_theme_paths()


def test_detection_and_explicit_classic_override(monkeypatch):
    monkeypatch.setattr(desktop_session.sys, "platform", "linux")
    monkeypatch.setenv("OMARCHY_PATH", "/usr/share/omarchy")
    monkeypatch.setenv("OPENWHISPER_UI", "auto")
    assert desktop_session.use_omarchy_ui()
    monkeypatch.setenv("OPENWHISPER_UI", "classic")
    assert not desktop_session.use_omarchy_ui()


def test_theme_default_honors_existing_preference(omarchy):
    assert resolve_ui_theme({}) == UiTheme.OMARCHY
    assert resolve_ui_theme({SettingsKey.UI_THEME: UiTheme.LIGHT}) == UiTheme.LIGHT


@pytest.mark.parametrize("index", [0, 1], ids=["omarchy4", "omarchy3"])
def test_both_palette_locations_and_atomic_replacement(omarchy, index):
    path = omarchy[index]
    path.parent.mkdir(parents=True)
    path.write_text(
        'background = "#1a1b26"\nforeground = "#a9b1d6"\naccent = "#7aa2f7"'
    )
    first = load_omarchy_palette()
    assert first.css("bg") == "#1a1b26"
    assert first.css("slate-bg") == "#1a1b26"
    assert first.css("accent-rgb") == "122, 162, 247"
    replacement = path.with_suffix(".new")
    replacement.write_text(
        'mode = "light"\nbackground = "#eeeeee"\nforeground = "#222222"'
    )
    replacement.replace(path)
    second = load_omarchy_palette()
    assert not second.is_dark
    assert first != second
    assert second.css("text") == "#222222"


def test_bad_theme_and_stylesheet_injection_fall_back(omarchy):
    path = omarchy[0]
    path.parent.mkdir(parents=True)
    for text in (
        "not toml",
        'background = "red; } QWidget { color: red"\nforeground = "#ffffff"',
    ):
        path.write_text(text)
        assert load_omarchy_palette() == DARK_PALETTE


def test_monochrome_palette_preserves_roles_when_colors_repeat():
    palette = palette_from_colors({"background": "#000000", "foreground": "#ffffff"})
    for role in ("text", "text-heading", "accent", "slate-text"):
        assert palette.css(role) == "#ffffff"
    assert set(palette.tokens) == set(DARK_PALETTE.tokens)


def test_desktop_theme_changes_within_same_dark_mode(omarchy):
    from ui_qt.utils.theme_manager import ThemeManager

    path = omarchy[0]
    path.parent.mkdir(parents=True)
    path.write_text('background = "#111111"\nforeground = "#dddddd"')
    manager = ThemeManager()
    assert manager.set_theme("dark", desktop_palette=True)
    assert not manager.set_theme("dark", desktop_palette=True)
    path.write_text('background = "#222222"\nforeground = "#dddddd"')
    assert manager.set_theme("dark", desktop_palette=True)
    assert manager.palette.css("bg") == "#222222"
    manager.set_theme("dark")


def test_native_window_accepts_compositor_sizes_across_modes(omarchy, monkeypatch):
    from ui_qt.main_window import MainWindow
    from ui_qt.widgets.desktop_header import DesktopHeader

    monkeypatch.setattr(
        "ui_qt.dialogs.meeting_intro_dialog.maybe_show_meeting_mode_intro",
        lambda *a: None,
    )
    window = MainWindow()
    window.show()
    QApplication.processEvents()
    assert isinstance(window.title_bar, DesktopHeader)
    assert not window.windowFlags() & Qt.WindowType.FramelessWindowHint
    for size in (QSize(1800, 1000), QSize(620, 500), QSize(420, 320)):
        window.resize(size)
        QApplication.processEvents()
        assert window.size() == size
        assert window._get_resize_edge(QPoint(1, 1)) == (0, 0)
        for transition in (
            lambda: window.set_host_mode(True, persist=False),
            lambda: window.set_host_mode(False, persist=False),
            lambda: window.set_compact_mode(True, persist=False),
            lambda: window.set_compact_mode(False, persist=False),
            window.toggle_history,
            lambda: window._on_sidebar_width_animated(380),
            lambda: window._animate_resize(700, 900),
        ):
            transition()
            QApplication.processEvents()
            assert window.size() == size
        assert window.centralWidget().size() == window.contentsRect().size()
    window._force_quit = True
    window.close()


def test_native_window_ignores_saved_offscreen_geometry(omarchy):
    from ui_qt.main_window import MainWindow

    settings_manager.save_setting(SettingsKey.HOST_MODE, True)
    settings_manager.save_setting(
        SettingsKey.HOST_WINDOW_GEOMETRY,
        {
            "x": -2000,
            "y": -17,
            "width": 780,
            "height": 1000,
        },
    )
    window = MainWindow()
    assert window.host_mode
    assert window.height() < 1000
    assert window.maximumWidth() > 10000
    window.resize(1400, 650)
    window._restore_host_geometry()
    assert window.size() == QSize(1400, 650)
    window._force_quit = True
    window.close()


def test_native_splash_is_opaque_and_has_no_animation(omarchy):
    from ui_qt.loading_screen import LoadingScreen

    splash = LoadingScreen()
    assert not splash.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not splash._glow_timer.isActive()
    image = splash.grab().toImage()
    assert image.pixelColor(0, image.height() - 1).alpha() == 255
    splash.destroy()


def test_wayland_hint_uses_native_popup_without_opacity(monkeypatch):
    from ui_qt.widgets import buttons

    monkeypatch.setattr(buttons, "compositor_managed", lambda: True)
    native = Mock()
    monkeypatch.setattr(buttons.QToolTip, "showText", native)
    button = QPushButton("Record")
    hint = buttons.HotkeyHintFilter(button, "Ctrl+R")
    hint.show_hint()
    native.assert_called_once()
    assert hint._hint is None


def test_wayland_never_opens_xlib_even_with_xwayland_display(monkeypatch):
    from services import _hotkey_pynput as backend

    monkeypatch.setattr(backend, "is_wayland_session", lambda: True)
    monkeypatch.setenv("DISPLAY", ":0")
    native = Mock(side_effect=AssertionError("Must not open Xlib"))
    monkeypatch.setattr(backend, "get_listener_class", native)
    manager = backend.HotkeyManager({"record_toggle": "ctrl+alt+r"})
    called = Mock()
    manager.on_record_toggle = called
    assert manager.handle_hotkey_press(frozenset({"ctrl", "alt"}), "r", source="qt")
    called.assert_called_once()
    manager.rehook()
    manager.cleanup()
    native.assert_not_called()


def test_wayland_watchdog_does_not_call_blocking_rehook(monkeypatch):
    from services.runtime.hotkeys import HotkeyRuntime

    monkeypatch.setattr(desktop_session, "is_wayland_session", lambda: True)
    manager = Mock()
    runtime = HotkeyRuntime(SimpleNamespace(hotkey_manager=manager))
    runtime.setup_hook_watchdog()
    assert not hasattr(runtime.controller, "_watchdog_timer")
    runtime.rehook_keyboard()
    manager.rehook.assert_not_called()


def test_linux_default_keypad_shortcuts_match_qt_events(monkeypatch):
    from services.runtime import hotkeys

    if not hotkeys.USE_PYNPUT_BACKEND:
        pytest.skip("Qt hotkey fallback is used on Linux/macOS")
    import sys

    if not sys.platform.startswith("linux"):
        pytest.skip("Linux keypad naming")
    from PyQt6.QtCore import QEvent
    from PyQt6.QtGui import QKeyEvent

    from services import _hotkey_pynput
    from services._hotkey_pynput import HotkeyManager

    monkeypatch.setattr(_hotkey_pynput, "is_wayland_session", lambda: True)
    event = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Asterisk,
        Qt.KeyboardModifier.KeypadModifier,
        "*",
    )
    name = hotkeys._qt_event_key_name(event)
    assert name == "kp *"
    manager = HotkeyManager({"record_toggle": "kp *"})
    called = Mock()
    manager.on_record_toggle = called
    try:
        assert manager.handle_hotkey_press(frozenset(), name, source="qt")
        called.assert_called_once()
    finally:
        manager.cleanup()


def test_desktop_theme_poll_only_restyles_changed_files(omarchy, monkeypatch):
    from ui_qt.app import QtApplication

    monkeypatch.setattr(
        "ui_qt.app.current_ui_theme_preference", lambda: UiTheme.OMARCHY
    )
    runtime = SimpleNamespace(_desktop_theme_signature=None, set_theme=Mock())
    QtApplication._refresh_desktop_theme(runtime)
    QtApplication._refresh_desktop_theme(runtime)
    assert runtime.set_theme.call_count == 1
    path = omarchy[0]
    path.parent.mkdir(parents=True)
    path.write_text('background = "#111111"\nforeground = "#dddddd"')
    QtApplication._refresh_desktop_theme(runtime)
    assert runtime.set_theme.call_count == 2
    QtApplication._refresh_desktop_theme(runtime)
    assert runtime.set_theme.call_count == 2


def test_embedded_overlay_stays_inside_main_window(omarchy):
    from PyQt6.QtWidgets import QWidget

    from ui_qt.overlays import WaveformOverlay

    parent = QWidget()
    parent.resize(620, 400)
    overlay = WaveformOverlay(parent)
    parent.show()
    overlay.show_at_cursor()
    QApplication.processEvents()
    assert not overlay.isWindow()
    assert parent.rect().contains(overlay.geometry())
    parent.resize(420, 300)
    QApplication.processEvents()
    assert parent.rect().contains(overlay.geometry())
    parent.hide()
    assert not overlay.timer.isActive()
    parent.close()


def test_wayland_settings_capture_does_not_start_native_listener(monkeypatch):
    from PyQt6.QtTest import QTest

    from ui_qt.dialogs import settings_dialog
    from ui_qt.widgets.profile_hotkey_input import ProfileHotkeyInput

    monkeypatch.setattr(settings_dialog, "is_native_wayland_session", lambda: True)
    thread = Mock(side_effect=AssertionError("Wayland must capture through Qt"))
    monkeypatch.setattr(settings_dialog, "HotkeyCaptureThread", thread)
    dialog = settings_dialog.SettingsDialog(
        get_loaded_model=lambda: None, background_cache_scan=False
    )
    dialog.select_destination("hotkeys")
    field = dialog.hotkey_inputs["record_toggle"]
    assert isinstance(field, ProfileHotkeyInput)
    applied = Mock()
    dialog.on_hotkeys_changed = applied
    field.begin_capture()
    QTest.keyClick(
        field,
        Qt.Key.Key_R,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    modifier = "cmd" if sys.platform == "darwin" else "ctrl"
    assert applied.call_args[0][0]["record_toggle"] == f"{modifier}+shift+r"
    assert not field._capturing
    thread.assert_not_called()
    dialog.close()


def test_linux_shortcut_labels_use_linux_modifier_names(monkeypatch):
    from services import _hotkey_pynput

    monkeypatch.setattr(_hotkey_pynput.sys, "platform", "linux")
    assert _hotkey_pynput.format_hotkey_display("ctrl+alt+r") == "Ctrl+Alt+R"
    assert _hotkey_pynput.format_hotkey_display("super+shift+f2") == "Super+Shift+F2"
    assert _hotkey_pynput.format_hotkey_display("kp *") == "Num *"


def test_native_dialog_resize_and_reopen_keep_form_accessible(omarchy):
    from PyQt6.QtWidgets import QDialog, QLabel, QVBoxLayout

    from ui_qt.utils.desktop import DesktopWindowFilter

    app = QApplication.instance()
    window_filter = DesktopWindowFilter(app)
    app.installEventFilter(window_filter)
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    content = QLabel("An oversized settings form")
    content.setMinimumSize(940, 520)
    layout.addWidget(content)
    try:
        for _ in range(2):
            dialog.show()
            dialog.resize(420, 300)
            app.processEvents()
            assert dialog.size() == QSize(420, 300)
            assert not dialog.windowFlags() & (
                Qt.WindowType.WindowMinMaxButtonsHint
                | Qt.WindowType.WindowCloseButtonHint
            )
            scroll = dialog._desktop_dialog_scroll
            assert scroll.horizontalScrollBar().maximum() > 0
            assert scroll.verticalScrollBar().maximum() > 0
            assert scroll.widget().isAncestorOf(content)
            dialog.close()
    finally:
        app.removeEventFilter(window_filter)
        dialog.close()


@pytest.mark.parametrize(
    "state", [Qt.WindowState.WindowMaximized, Qt.WindowState.WindowFullScreen]
)
def test_native_tray_restore_preserves_window_state(omarchy, state):
    from ui_qt.main_window import MainWindow

    window = MainWindow()
    window.setWindowState(state)
    window.show()
    QApplication.processEvents()
    window.hide()
    window.restore_from_tray()
    QApplication.processEvents()
    assert window.windowState() & state
    window._force_quit = True
    window.close()


def test_streaming_indicator_shrinks_with_tile(omarchy):
    from PyQt6.QtWidgets import QWidget

    from ui_qt.overlays import WaveformOverlay

    parent = QWidget()
    parent.resize(620, 670)
    parent.show()
    overlay = WaveformOverlay(parent)
    overlay.show_at_cursor(overlay.STATE_STREAMING)
    overlay.update_streaming_text("Live transcription text. " * 80)
    parent.resize(420, 240)
    QApplication.processEvents()
    assert parent.rect().contains(overlay.geometry())
    overlay.hide()
    parent.close()


def test_settings_search_fits_small_tile(omarchy):
    from PyQt6.QtWidgets import QWidget

    from ui_qt.dialogs.settings_search import SearchEntry, SearchPalette

    host = QWidget()
    host.resize(420, 240)
    host.show()
    palette = SearchPalette(
        host,
        lambda: [
            SearchEntry("setting", "Recording", "Record settings", "recording")
            for _ in range(20)
        ],
    )
    palette.open("record")
    QApplication.processEvents()
    assert host.rect().contains(palette.panel.geometry())
    assert palette.results.verticalScrollBar().maximum() > 0
    host.close()


def test_overview_reflows_cards_in_narrow_settings(omarchy):
    from ui_qt.dialogs.settings_overview import OverviewPage

    page = OverviewPage()
    page.resize(420, 1200)
    page.show()
    QApplication.processEvents()
    assert page._columns == 1
    assert page.cards["cleanup"].y() > page.cards["voice_model"].y()
    page.resize(1000, 1200)
    QApplication.processEvents()
    assert page._columns == 3
    assert page.cards["cleanup"].y() == page.cards["voice_model"].y()
    page.close()
