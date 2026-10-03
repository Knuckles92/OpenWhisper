"""Extended interactive state matrix for qa_omarchy_ui.py; no engine/network writes."""

from __future__ import annotations

import json
import os
import subprocess
from types import SimpleNamespace

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QComboBox, QFileDialog, QMessageBox, QPushButton


def exercise_ui(runtime, window, settings, settle, capture, report, keepalive):
    from services.settings import UiTheme
    from ui_qt.dialogs.batch_relation_dialog import BatchRelationDialog
    from ui_qt.dialogs.cleanup_prompt_dialog import CleanupPromptDialog
    from ui_qt.dialogs.cleanup_rule_dialog import CleanupRuleDialog
    from ui_qt.dialogs.history_export_dialog import HistoryExportDialog
    from ui_qt.dialogs.meeting_delete_dialog import MeetingDeleteDialog
    from ui_qt.dialogs.meeting_export_dialog import MeetingExportDialog
    from ui_qt.dialogs.transcript_viewer_dialog import TranscriptViewerDialog
    from ui_qt.overlays import WaveformOverlay
    from ui_qt.widgets.field_help import FieldHelp

    native = runtime.app.platformName().startswith("wayland")
    report["checks"] = []
    forbidden = (
        Qt.WindowType.WindowMinMaxButtonsHint | Qt.WindowType.WindowCloseButtonHint
    )

    def check(label, condition):
        assert condition, label
        report["checks"].append(label)

    def client(widget):
        clients = json.loads(
            subprocess.check_output(["hyprctl", "-j", "clients"], text=True, timeout=5)
        )
        return next(
            c
            for c in clients
            if c["pid"] == os.getpid()
            and c["title"]
            in (
                widget.windowTitle(),
                widget.windowTitle() + " — OpenWhisper",
            )
        )

    def dispatch(widget, operation, arguments):
        addr = client(widget)["address"]
        prefix = arguments + ", " if arguments else ""
        code = f'hl.dsp.window.{operation}({{{prefix}window="address:{addr}"}})'
        result = subprocess.check_output(
            ["hyprctl", "eval", f"hl.dispatch({code})"], text=True, timeout=5
        )
        assert "error" not in result.lower(), result
        settle()

    def resize(widget, width, height):
        if native:
            dispatch(widget, "float", 'action="enable"')
            dispatch(widget, "resize", f"x={width}, y={height}, relative=false")
            dispatch(widget, "center", "")
            actual = client(widget)["size"]
            check(
                f"compositor assigned {width}x{height}, got {actual}",
                actual == [width, height],
            )
        else:
            widget.resize(width, height)
            settle()

    check("main has no titlebar buttons", not window.windowFlags() & forbidden)
    check(
        "no custom window-control widgets",
        not any(
            b.accessibleName() in ("Minimize window", "Maximize window", "Close window")
            for b in window.findChildren(QPushButton)
        ),
    )
    window.tabbed_content.set_current_index(0)
    window.set_host_mode(False, persist=False)
    for cycle in range(3):
        settings.show()
        settle()
        check(f"settings chrome {cycle}", not settings.windowFlags() & forbidden)
        for width, height in ((1000, 600), (740, 450), (1200, 650)):
            resize(settings, width, height)
            capture(f"settings-resize-{cycle}-{width}", settings)
        settings.close()
        window.restore_from_tray()
        capture(f"settings-return-{cycle}")

    for state, flag in (
        ("maximized", Qt.WindowState.WindowMaximized),
        ("fullscreen", Qt.WindowState.WindowFullScreen),
    ):
        if native:
            dispatch(window, "fullscreen", f'mode="{state}", action="set"')
        else:
            window.setWindowState(flag)
        settle()
        check(f"entered {state}", bool(window.windowState() & flag))
        capture(state)
        for host in (True, False):
            window.set_host_mode(host, persist=False)
            capture(f"{state}-host-{host}")
        window.hide()
        window.restore_from_tray()
        capture(f"{state}-restore")
        check(f"restore preserves {state}", bool(window.windowState() & flag))
        if native:
            dispatch(window, "fullscreen", f'mode="{state}", action="unset"')
        else:
            window.showNormal()
        settle()

    for cycle in range(3):
        window.minimize_to_tray()
        check(f"tray hides {cycle}", not window.isVisible())
        window.restore_from_tray()
        capture(f"tray-restore-{cycle}")
    window.set_tray_available(False)
    window.minimize_to_tray()
    check("missing tray does not hide app", window.isVisible())
    window.restore_from_tray()
    window.set_tray_available(True)

    settings.show()
    for key in settings._pages:
        settings.select_destination(key)
        capture(f"settings-{key}", settings)
        for area in (settings._page_scrolls.get(key),):
            if area is not None:
                area.verticalScrollBar().setValue(area.verticalScrollBar().maximum())
                settle(0.1)
                area.verticalScrollBar().setValue(0)
    settings.open_search("hotkey")
    settle()
    capture("settings-search", settings)
    settings.search_palette.close()
    for child in settings.findChildren(QMessageBox):
        child.close()
    settings.close()

    # Transient windows stay in screen bounds and preserve compositor ownership.
    reader = TranscriptViewerDialog(window)
    reader.set_transcript("# Transcript\n\n" + "A sample local transcript. " * 100)
    dialogs = [
        ("transcript", reader),
        ("cleanup-prompt", CleanupPromptDialog("Fix punctuation.", window)),
        ("cleanup-rule", CleanupRuleDialog("A sample correction", parent=window)),
        (
            "batch",
            BatchRelationDialog(
                [f"recording-{n}.wav" for n in range(9)], parent=window
            ),
        ),
        ("history-export", HistoryExportDialog(window, entry_provider=lambda: [])),
        ("meeting-export", MeetingExportDialog(window, meeting_provider=lambda: [])),
        ("meeting-delete", MeetingDeleteDialog(window, has_audio=True)),
        (
            "message",
            QMessageBox(
                QMessageBox.Icon.Information,
                "OpenWhisper UI test message",
                "A sample message.",
                QMessageBox.StandardButton.Ok,
                window,
            ),
        ),
    ]
    file_dialog = QFileDialog(window, "OpenWhisper UI test file picker")
    file_dialog.setOption(QFileDialog.Option.DontUseNativeDialog)
    dialogs.append(("file-picker", file_dialog))
    for name, dialog in dialogs:
        keepalive.append(dialog)
        dialog.show()
        settle()
        check(f"{name} has no titlebar buttons", not dialog.windowFlags() & forbidden)
        room = dialog.screen().availableGeometry()
        check(
            f"{name} fits screen",
            dialog.width() <= room.width() and dialog.height() <= room.height(),
        )
        capture(f"dialog-{name}", dialog)
        dialog.close()
        settle(0.1)

    window.tabbed_content.set_current_index(0)
    window.restore_from_tray()
    settle()
    combo = next(
        c for c in window.quick_record_tab.findChildren(QComboBox) if c.isVisible()
    )
    combo.showPopup()
    settle()
    combo.hidePopup()
    check("combobox popup closes", not combo.view().isVisible())
    menu = window.title_bar.menu_bar.actions()[0].menu()
    menu.popup(window.title_bar.mapToGlobal(window.title_bar.rect().bottomLeft()))
    settle()
    menu.hide()
    check("application menu closes", not menu.isVisible())
    for help_button in window.quick_record_tab.findChildren(FieldHelp):
        if help_button.isVisible():
            help_button.show_help()
            help_button._dismiss_timer.stop()
            settle(0.1)
            if native:
                check("field help is inside the app", not help_button.card.isWindow())
                check(
                    "field help fits",
                    window.rect().contains(help_button.card.geometry()),
                )
            capture("field-help")
            help_button.card.hide()
            break

    overlay = WaveformOverlay(window)
    keepalive.append(overlay)
    for state in (
        overlay.STATE_RECORDING,
        overlay.STATE_STREAMING,
        overlay.STATE_PROCESSING,
        overlay.STATE_CANCELING,
        overlay.STATE_COPIED,
    ):
        overlay.show_at_cursor(state)
        if state == overlay.STATE_STREAMING:
            overlay.update_streaming_text("Live local transcription. " * 20)
        check(
            f"{state} indicator inside window",
            not overlay.isWindow() and window.rect().contains(overlay.geometry()),
        )
        capture(f"indicator-{state}")
        overlay.hide()

    if native:
        from services._hotkey_pynput import HotkeyManager
        from services.runtime.hotkeys import ActiveWindowHotkeyFilter

        manager = HotkeyManager({"record_toggle": "kp *", "cancel": "kp -"})
        pressed = []
        manager.set_callbacks(
            on_record_toggle=lambda: pressed.append("record"),
            on_cancel=lambda: pressed.append("cancel"),
        )
        controller = SimpleNamespace(
            hotkey_manager=manager, ui_controller=SimpleNamespace(main_window=window)
        )
        key_filter = ActiveWindowHotkeyFilter(controller)
        runtime.app.installEventFilter(key_filter)
        address = client(window)["address"]
        subprocess.check_output(
            [
                "hyprctl",
                "eval",
                f'hl.dispatch(hl.dsp.focus({{window="address:{address}"}}))',
            ],
            text=True,
            timeout=5,
        )
        settle()
        check("native main window receives focus", runtime.app.activeWindow() is window)
        QTest.keyClick(window, Qt.Key.Key_Asterisk, Qt.KeyboardModifier.KeypadModifier)
        QTest.keyClick(window, Qt.Key.Key_Minus, Qt.KeyboardModifier.KeypadModifier)
        settle(0.1)
        check(
            "default keypad shortcuts reach callbacks", pressed == ["record", "cancel"]
        )
        pressed.clear()
        settle(0.5)
        for key in ("KP_Multiply", "KP_Subtract"):
            subprocess.check_output(
                [
                    "hyprctl",
                    "eval",
                    f'hl.dispatch(hl.dsp.send_shortcut({{mods="", key="{key}", window="address:{address}"}}))',
                ],
                text=True,
                timeout=5,
            )
            settle(0.2)
        check("Hyprland keypad events reach callbacks", pressed == ["record", "cancel"])
        runtime.app.removeEventFilter(key_filter)
        manager.cleanup()

    for scale in (85, 125, 150):
        runtime.apply_font_scale(scale)
        resize(window, 620, 450)
        capture(f"record-font-{scale}")
        settings.show()
        settings.select_destination("general")
        capture(f"settings-font-{scale}", settings)
        resize(settings, 620, 450)
        settings.open_search("hotkey")
        check(
            f"search fits at {scale}%",
            settings.rect().contains(settings.search_palette.panel.geometry()),
        )
        capture(f"search-font-{scale}", settings)
        settings.search_palette.close()
        settings.close()
    runtime.apply_font_scale(100)
    for theme in (UiTheme.LIGHT, UiTheme.DARK, UiTheme.OMARCHY):
        runtime.set_theme(theme)
        capture(f"theme-{theme}")
    if native:
        dispatch(window, "float", 'action="disable"')
