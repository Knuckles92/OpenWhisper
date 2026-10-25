"""Layout tests for the redesigned Settings rail."""

import os
import unittest
from unittest.mock import patch

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QSize, Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
)

from ui_qt.dialogs.settings_dialog import (
    ACTIONS,
    ADVANCED,
    APPEARANCE,
    API_KEYS,
    BACKUP,
    CLEANUP,
    CLEANUP_RULES,
    CLEANUP_PROFILES,
    COMMANDS,
    DICTIONARY,
    DOWNLOADS,
    GENERAL,
    HOOKS,
    HOTKEYS,
    MCP,
    MEETING_AFTER,
    MEETING_DASHBOARD,
    MEETING_INTELLIGENCE,
    MEETING_FAST,
    MEETING_VOICE,
    OVERVIEW,
    RECORDING,
    REMOTE_ENGINE,
    RUNTIME,
    SNIPPETS,
    STYLES,
    VOICE_MODEL,
    WALKTHROUGHS,
    SettingsDialog,
)
from ui_qt.utils.font_scale import apply_ui_font_scale
from ui_qt.utils.theme_manager import ThemeManager
from ui_qt.widgets.setting_tile import FieldTile, InfoTile, SettingTile, TileBase
from ui_qt.widgets.nav_rail import NavRail
from ui_qt.widgets.wrapped_label import WrappedLabel


class TestSettingsGeneralLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_walkthrough_copy_preference_saves_and_reloads(self):
        import tempfile

        from services.settings import (
            SettingsKey,
            SettingsManager,
            WalkthroughCopyFormat,
        )
        from ui_qt.dialogs import settings_dialog as module

        with tempfile.TemporaryDirectory() as folder:
            manager = SettingsManager(os.path.join(folder, "settings.json"))
            with patch.object(module, "settings_manager", manager):
                dialog = SettingsDialog()
                try:
                    dialog.ensure_page(WALKTHROUGHS)
                    combo = dialog.walkthrough_copy_format_combo
                    self.assertEqual(combo.currentData(), WalkthroughCopyFormat.BOTH)
                    self.assertEqual(
                        [combo.itemData(i) for i in range(combo.count())],
                        list(WalkthroughCopyFormat.ALL),
                    )
                    for value in (WalkthroughCopyFormat.AGENT, WalkthroughCopyFormat.PDF):
                        combo.setCurrentIndex(combo.findData(value))
                        self.assertEqual(manager.get(SettingsKey.WALKTHROUGH_COPY_FORMAT), value)
                finally:
                    dialog.close()
                dialog = SettingsDialog()
                try:
                    dialog.ensure_page(WALKTHROUGHS)
                    self.assertEqual(
                        dialog.walkthrough_copy_format_combo.currentData(), WalkthroughCopyFormat.PDF
                    )
                finally:
                    dialog.close()

    def test_uses_rail_and_independently_scrollable_pages(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
            dialog.resize(SettingsDialog.DEFAULT_SIZE)
            dialog.ensure_all_pages()
        try:
            self.assertIsInstance(dialog.rail, NavRail)
            # Chapters by feature, under the Home landing page.
            self.assertEqual(
                dialog.rail.keys(),
                (
                    OVERVIEW,
                    RECORDING,
                    VOICE_MODEL,
                    CLEANUP,
                    REMOTE_ENGINE,
                    WALKTHROUGHS,
                    DICTIONARY,
                    SNIPPETS,
                    STYLES,
                    CLEANUP_RULES,
                    CLEANUP_PROFILES,
                    COMMANDS,
                    ACTIONS,
                    HOOKS,
                    MCP,
                    MEETING_VOICE,
                    MEETING_INTELLIGENCE,
                    MEETING_FAST,
                    MEETING_AFTER,
                    MEETING_DASHBOARD,
                    DOWNLOADS,
                    RUNTIME,
                    BACKUP,
                    GENERAL,
                    APPEARANCE,
                    HOTKEYS,
                    API_KEYS,
                    ADVANCED,
                ),
            )
            self.assertEqual(dialog.rail.current_key(), OVERVIEW)
            for key in dialog.rail.keys():
                if key == DOWNLOADS:
                    # Its catalog list is the page's own scroller.
                    self.assertNotIn(key, dialog._page_scrolls)
                    self.assertIs(dialog._pages[key].parentWidget(), dialog.stack)
                    continue
                scroll = dialog._page_scrolls[key]
                self.assertIs(scroll.widget(), dialog._pages[key])
                self.assertTrue(scroll.widgetResizable())
            def group_titles(page):
                return [
                    label.text()
                    for label in page.findChildren(QLabel)
                    if label.objectName() == "settingsTileGroupTitle"
                ]

            general = dialog._pages[GENERAL]
            tiles = general.findChildren(SettingTile)
            self.assertEqual(len(tiles), 7)
            self.assertEqual(general.findChildren(FieldTile), [])
            self.assertIs(dialog.auto_paste_check, dialog.auto_paste_tile.checkbox)
            self.assertIs(
                dialog.update_notify_check, dialog.update_notify_tile.checkbox
            )
            self.assertEqual(
                group_titles(general), ["Output", "Window", "Updates"]
            )
            # Everything about walkthroughs is on its own page under Dictation.
            walkthroughs = dialog._pages[WALKTHROUGHS]
            self.assertEqual(group_titles(walkthroughs), ["Copy", "Document", "AI write-up"])
            self.assertEqual(
                set(walkthroughs.findChildren(FieldTile)),
                {dialog.walkthrough_title_tile, dialog.walkthrough_page_size_tile,
                 dialog.walkthrough_copy_format_tile,
                 dialog.walkthrough_write_up_agent_tile,
                 dialog.walkthrough_write_up_instructions_tile},
            )
            # Notes are for the write-up's agent: greyed out while it's off.
            notes = dialog.walkthrough_write_up_notes_tile
            self.assertTrue(walkthroughs.isAncestorOf(notes))
            self.assertEqual(notes.isEnabled(), dialog.walkthrough_write_up_check.isChecked())
            for key in ("walkthrough", "screenshot"):
                self.assertTrue(walkthroughs.isAncestorOf(dialog.hotkey_inputs[key]))
                self.assertFalse(dialog._pages[HOTKEYS].isAncestorOf(dialog.hotkey_inputs[key]))
            # Every visual setting lives on Appearance.
            appearance = dialog._pages[APPEARANCE]
            self.assertEqual(
                set(appearance.findChildren(FieldTile)),
                {
                    dialog.ui_theme_tile,
                    dialog.ui_font_scale_tile,
                    dialog.recording_look_tile,
                    dialog.streaming_layout_tile,
                    dialog.streaming_font_size_tile,
                },
            )
            self.assertIs(dialog.streaming_layout_tile.control, dialog.streaming_layout_bar)
            self.assertIs(
                dialog.ui_font_scale_tile.control, dialog.ui_font_scale_combo
            )
            self.assertIs(dialog.ui_theme_tile.control, dialog.ui_theme_combo)
            self.assertIs(
                dialog.streaming_font_size_tile.control,
                dialog.streaming_font_size_spinbox,
            )
            self.assertEqual(group_titles(appearance), ["Theme and text", "Overlay"])
            recording = dialog._pages[RECORDING]
            self.assertGreaterEqual(len(recording.findChildren(QFormLayout)), 1)
            self.assertEqual(len(recording.findChildren(FieldTile)), 2)
            self.assertEqual(len(recording.findChildren(SettingTile)), 1)
            self.assertIs(
                dialog.streaming_enabled_check,
                dialog.streaming_enabled_tile.checkbox,
            )
            self.assertIs(
                dialog.recording_retention_tile.control,
                dialog.recording_retention_combo,
            )
            intelligence = dialog._pages[MEETING_INTELLIGENCE]
            self.assertEqual(len(intelligence.findChildren(SettingTile)), 2)
            self.assertEqual(len(dialog._pages[MEETING_FAST].findChildren(SettingTile)), 7)
            self.assertIs(
                dialog.meeting_context_folder_path.parentWidget(),
                dialog.meeting_context_folder_tile.body,
            )
            self.assertIs(
                dialog.typesafe_enabled_check, dialog.typesafe_enabled_tile.checkbox
            )
            after = dialog._pages[MEETING_AFTER]
            self.assertEqual(len(after.findChildren(SettingTile)), 7)
            dashboard = dialog._pages[MEETING_DASHBOARD]
            self.assertEqual(len(dashboard.findChildren(FieldTile)), 2)
            self.assertEqual(len(dashboard.findChildren(TileBase)), 2)
            self.assertIs(
                dialog.meeting_bind_warning.parentWidget(),
                dialog.meeting_bind_tile.body,
            )
            self.assertGreaterEqual(dialog.max_recordings_spinbox.minimumWidth(), 120)
            self.assertGreaterEqual(
                dialog.streaming_font_size_spinbox.minimumWidth(), 120
            )
            port = dialog.meeting_port_spinbox
            self.assertEqual(port.specialValueText(), "Automatic")
            port.adjustSize()
            port.show()
            self.app.processEvents()
            edit = port.lineEdit()
            self.assertIsNotNone(edit)
            margins = edit.textMargins()
            available = edit.width() - margins.left() - margins.right()
            self.assertGreaterEqual(
                available,
                port.fontMetrics().horizontalAdvance("Automatic"),
            )
            self.assertEqual(
                dialog.max_recordings_spinbox.up_button.objectName(), "spinStepUp"
            )
            self.assertEqual(
                dialog.max_recordings_spinbox.down_button.objectName(),
                "spinStepDown",
            )
            helpers = recording.findChildren(WrappedLabel)
            self.assertGreaterEqual(len(helpers), 2)
            cleanup = dialog._pages[CLEANUP]
            # The AI-cleanup switch; the level and custom prompt fields.
            self.assertEqual(len(cleanup.findChildren(SettingTile)), 1)
            self.assertEqual(len(cleanup.findChildren(FieldTile)), 2)
            self.assertEqual(len(cleanup.findChildren(InfoTile)), 1)
            self.assertIs(
                dialog.transcript_cleanup_check,
                dialog.transcript_cleanup_tile.checkbox,
            )
            self.assertIs(dialog.cleanup_level_tile.control, dialog.cleanup_level_bar)
            self.assertIs(
                dialog.cleanup_prompt_tile.control, dialog.cleanup_prompt_edit
            )
            self.assertFalse(hasattr(dialog, "cleanup_reasoning_combo"))
            self.assertLessEqual(dialog.cleanup_prompt_edit.maximumHeight(), 120)
            rules = dialog._pages[CLEANUP_RULES]
            self.assertEqual(len(rules.findChildren(InfoTile)), 3)
            self.assertEqual(len(rules.findChildren(SettingTile)), 0)
            self.assertIs(dialog.cleanup_rules_gate_tile.parentWidget(), rules)
            self.assertIs(
                dialog.cleanup_rules_turn_on_btn.parentWidget(),
                dialog.cleanup_rules_gate_tile.body,
            )
            self.assertIs(
                dialog.cleanup_rule_input.parentWidget(),
                dialog.cleanup_rules_composer_tile.body,
            )
            self.assertIs(
                dialog.cleanup_rules_list.parentWidget(),
                dialog.cleanup_rules_library_tile.body,
            )
            self.assertEqual(len(intelligence.findChildren(InfoTile)), 1)
            self.assertIs(
                dialog.models.meeting_model_picker.parentWidget(),
                dialog.meeting_model_tile.body,
            )
            self.assertIs(
                dialog.models.text_model_picker.parentWidget(),
                dialog.cleanup_model_tile.body,
            )
            api_keys = dialog._pages[API_KEYS]
            self.assertEqual(len(api_keys.findChildren(FieldTile)), 1)
            self.assertEqual(len(api_keys.findChildren(InfoTile)), 1)
            self.assertIs(
                dialog.api_key_credential_tile.control, dialog.api_key_combo
            )
            self.assertIs(
                dialog.api_key_edit.parentWidget().parentWidget(),
                dialog.api_key_entry_tile.body,
            )
            self.assertIs(
                dialog.api_key_store_caption,
                dialog.api_key_entry_tile.description_label,
            )
            advanced = dialog._pages[ADVANCED]
            self.assertEqual(
                set(advanced.findChildren(SettingTile)),
                {dialog.developer_mode_tile, dialog.meeting_redecode_coverage_guard_tile},
            )
            self.assertIs(
                dialog.meeting_redecode_coverage_guard_check,
                dialog.meeting_redecode_coverage_guard_tile.checkbox,
            )
            self.assertEqual(len(advanced.findChildren(FieldTile)), 0)
            self.assertIs(
                dialog.developer_mode_check, dialog.developer_mode_tile.checkbox
            )
            # The Hugging Face policy sits next to the catalog it governs.
            self.assertTrue(dialog.downloads.isAncestorOf(dialog.hf_policy_combo))
            hotkeys = dialog._pages[HOTKEYS]
            shortcut_cards = [
                card
                for card in hotkeys.findChildren(QFrame)
                if card.objectName() == "hotkeyShortcutCard"
            ]
            self.assertEqual(len(shortcut_cards), 10)
            self.assertEqual(
                set(dialog.hotkey_inputs),
                {
                    "record_toggle",
                    "command_mode",
                    "route_record",
                    "cancel",
                    "meeting_toggle",
                    "scratchpad_toggle",
                    "cycle_language",
                    "paste_last_original",
                    "walkthrough",
                    "screenshot",
                    "enable_disable",
                    "minimize_tray",
                },
            )
            self.assertEqual(
                set(dialog.hotkey_clear_buttons),
                {
                    "command_mode",
                    "route_record",
                    "meeting_toggle",
                    "scratchpad_toggle",
                    "cycle_language",
                    "paste_last_original",
                    "walkthrough",
                    "screenshot",
                },
            )
        finally:
            dialog.close()

    def test_general_tiles_toggle_and_gate_update_notify(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
            dialog.resize(SettingsDialog.DEFAULT_SIZE)
            dialog.ensure_all_pages()
        try:
            dialog.show()
            self.app.processEvents()
            dialog.rail.select(GENERAL)
            self.app.processEvents()
            tile = dialog.copy_clipboard_tile
            before = tile.checkbox.isChecked()
            QTest.mouseClick(tile, Qt.MouseButton.LeftButton, pos=QPoint(8, 8))
            self.assertEqual(tile.checkbox.isChecked(), not before)
            self.assertEqual(tile.property("checked"), not before)

            dialog.update_check_check.setChecked(True)
            self.assertTrue(dialog.update_notify_check.isEnabled())
            dialog.update_check_check.setChecked(False)
            self.assertFalse(dialog.update_notify_tile.isEnabled())
            self.assertFalse(dialog.update_notify_check.isEnabled())
        finally:
            dialog.close()

    def test_cleanup_toggle_gates_the_prompt_and_rule_tiles(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
            dialog.resize(SettingsDialog.DEFAULT_SIZE)
            dialog.ensure_all_pages()
        try:
            dialog.transcript_cleanup_check.setChecked(True)
            self.assertTrue(dialog.cleanup_level_tile.isEnabled())
            self.assertTrue(dialog.cleanup_prompt_tile.isEnabled())
            self.assertTrue(dialog.cleanup_rules_composer_tile.isEnabled())
            self.assertTrue(dialog.cleanup_rules_gate_tile.isHidden())
            dialog.transcript_cleanup_check.setChecked(False)
            for tile in (
                dialog.cleanup_level_tile,
                dialog.cleanup_prompt_tile,
                dialog.cleanup_rules_composer_tile,
                dialog.cleanup_rules_library_tile,
            ):
                self.assertFalse(tile.isEnabled())
            self.assertFalse(dialog.cleanup_rule_add_btn.isEnabled())
            # The cleanup model stays editable so it can be set up before turning
            # cleanup on.
            self.assertTrue(dialog.cleanup_model_tile.isEnabled())
            self.assertTrue(dialog.models.text_model_picker.isEnabled())
            self.assertFalse(dialog.cleanup_rules_gate_tile.isHidden())
            self.assertTrue(dialog.cleanup_rules_gate_tile.isEnabled())
            self.assertTrue(dialog.cleanup_rules_turn_on_btn.isEnabled())
            self.assertEqual(dialog.cleanup_rules_turn_on_btn.text(), "Turn on (Medium)")
        finally:
            dialog.close()

    def test_learned_rules_gate_button_turns_cleanup_on(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
            dialog.resize(SettingsDialog.DEFAULT_SIZE)
            dialog.ensure_all_pages()
        try:
            dialog.show()
            self.app.processEvents()
            dialog.transcript_cleanup_check.setChecked(False)
            dialog.rail.select(CLEANUP_RULES)
            dialog.cleanup_rules_turn_on_btn.click()
            self.app.processEvents()
            self.assertEqual(dialog.rail.current_key(), CLEANUP_RULES)
            self.assertTrue(dialog.transcript_cleanup_check.isChecked())
            self.assertTrue(dialog.cleanup_rules_gate_tile.isHidden())
            self.assertTrue(dialog.cleanup_rules_composer_tile.isEnabled())
        finally:
            dialog.close()

    def test_report_view_tiles_follow_the_final_report_toggle(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
            dialog.resize(SettingsDialog.DEFAULT_SIZE)
            dialog.ensure_all_pages()
        try:
            dialog.meeting_end_report_check.setChecked(True)
            self.assertTrue(dialog.meeting_report_ribbon_tile.isEnabled())
            dialog.meeting_end_report_check.setChecked(False)
            for tile in (
                dialog.meeting_report_ribbon_tile,
                dialog.meeting_report_brief_tile,
                dialog.meeting_report_signal_tile,
            ):
                self.assertFalse(tile.isEnabled())
                self.assertFalse(tile.checkbox.isEnabled())
            self.assertFalse(dialog.meeting_report_views_title.isEnabled())
        finally:
            dialog.close()

    def test_pages_scroll_without_forcing_the_window_taller(self):
        previous_stylesheet = self.app.styleSheet()
        self.app.setStyleSheet(ThemeManager().stylesheet)
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
            dialog.resize(SettingsDialog.DEFAULT_SIZE)
            dialog.ensure_all_pages()
        try:
            # The offscreen test screen (800x800) is smaller than the default
            # size, and fitting it is test_window_fit_and_title_bar.py's job;
            # this one checks the pages at the sizes it sets.
            with patch.object(SettingsDialog, "_fit_to_screen", lambda self: None):
                dialog.show()
            for _ in range(8):
                self.app.processEvents()
            self.assertEqual(dialog.size(), SettingsDialog.DEFAULT_SIZE)
            close = dialog.findChild(QPushButton, "modelManagerCloseButton")
            for size in (QSize(980, 560), SettingsDialog.MINIMUM_SIZE):
                dialog.resize(size)
                for key in dialog.rail.keys():
                    dialog.rail.select(key)
                    for _ in range(8):
                        self.app.processEvents()
                    with self.subTest(size=size, page=key):
                        self.assertEqual(dialog.size(), size)
                        self.assertEqual(dialog.minimumSize(), SettingsDialog.MINIMUM_SIZE)
                        page = dialog._pages[key]
                        if key not in dialog._page_scrolls:
                            # Downloads fills the stack and scrolls its list.
                            self.assertGreaterEqual(
                                page.height(), page.minimumSizeHint().height()
                            )
                            self.assertLessEqual(
                                page.geometry().bottom(), dialog.stack.height()
                            )
                            continue
                        scroll = dialog._page_scrolls[key]
                        self.assertEqual(page.width(), scroll.viewport().width())
                        self.assertGreaterEqual(page.height(), page.minimumSizeHint().height())
                        self.assertTrue(dialog.rect().contains(
                            close.mapTo(dialog, close.rect().bottomRight())
                        ))
                        bar = scroll.verticalScrollBar()
                        bar.setValue(bar.maximum())
                        self.app.processEvents()
                        self.assertLessEqual(
                            page.mapTo(scroll.viewport(), page.rect().bottomRight()).y(),
                            scroll.viewport().height(),
                        )
            self.assertGreater(dialog._page_scrolls[GENERAL].verticalScrollBar().maximum(), 0)
            # Only the current chapter's pages show, but a short window can
            # still scroll the rail: the current page stays in view.
            item_rect = dialog.rail.visualItemRect(dialog.rail.currentItem())
            self.assertTrue(dialog.rail.viewport().rect().contains(item_rect))
        finally:
            dialog.close()
            self.app.setStyleSheet(previous_stylesheet)

    def test_cards_reflow_when_the_window_narrows(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
            dialog.resize(SettingsDialog.DEFAULT_SIZE)
            dialog.ensure_all_pages()
        try:
            dialog.show()
            # The window floor keeps two columns; the reflow itself is what
            # this test pins down, so let the body get narrow enough for one.
            dialog.setMinimumSize(0, 0)
            dialog.select_destination(GENERAL)
            for width in (1200, 800, 1200):
                dialog.resize(width, 600)
                for _ in range(8):
                    self.app.processEvents()
                left, right = dialog.auto_paste_tile, dialog.restore_clipboard_tile
                with self.subTest(width=width):
                    if width == 800:
                        self.assertEqual(left.x(), right.x())
                        self.assertGreater(right.y(), left.geometry().bottom())
                    else:
                        self.assertEqual(left.y(), right.y())
                        self.assertGreater(right.x(), left.geometry().right())
        finally:
            dialog.close()

    @pytest.mark.usefixtures("isolated_app_theme")
    def test_tile_titles_and_descriptions_fit_after_font_and_width_changes(self):
        manager = ThemeManager()
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
            dialog.resize(SettingsDialog.DEFAULT_SIZE)
            dialog.ensure_all_pages()
        try:
            # Fitting the 800x800 offscreen screen would lower the minimum
            # width below the 940 px these layouts are designed down to.
            with patch.object(SettingsDialog, "_fit_to_screen", lambda self: None):
                dialog.show()
            for scale in (100, 115, 130, 100):
                apply_ui_font_scale(scale, app=self.app, theme_manager=manager)
                # 800 is clamped to the 940 px minimum: the narrowest layout.
                for width in (800, 1100):
                    dialog.resize(width, 560)
                    for key in dialog.rail.keys():
                        dialog.rail.select(key)
                        for _ in range(8):
                            self.app.processEvents()
                        page = dialog._pages[key]
                        controls = (page.findChildren(QAbstractButton)
                                    + page.findChildren(QComboBox)
                                    + page.findChildren(QLineEdit))
                        for control in controls:
                            if not control.isVisible():
                                continue
                            with self.subTest(scale=scale, width=width, page=key,
                                              control=control.objectName()):
                                self.assertGreaterEqual(
                                    control.mapTo(page, control.rect().topLeft()).x(), 0
                                )
                                self.assertLess(
                                    control.mapTo(page, control.rect().bottomRight()).x(),
                                    page.width(),
                                )
                        for tile in page.findChildren(TileBase):
                            for label in (tile.title_label, tile.description_label):
                                if not label.isVisible():
                                    continue
                                with self.subTest(scale=scale, width=width, page=key,
                                                  tile=tile.title_label.text(),
                                                  label=label.objectName()):
                                    self.assertTrue(label.wordWrap())
                                    self.assertEqual(QLabel.text(label), label.text())
                                    self.assertGreaterEqual(
                                        label.height(), label.heightForWidth(label.width())
                                    )
                                    self.assertTrue(tile.rect().contains(
                                        label.mapTo(tile, label.rect().bottomRight())
                                    ))
                                    self.assertTrue(dialog._pages[key].rect().contains(
                                        tile.geometry()
                                    ))
        finally:
            dialog.close()
