"""Layout tests for the redesigned Settings rail."""

import os
import unittest
from unittest.mock import patch

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
    ADVANCED,
    API_KEYS,
    CLEANUP,
    CLEANUP_RULES,
    CLEANUP_PROFILES,
    GENERAL,
    HOTKEYS,
    MEETING_AFTER,
    MEETING_DASHBOARD,
    MEETING_INTELLIGENCE,
    MEETING_FAST,
    RECORDING,
    SettingsDialog,
)
from ui_qt.utils.font_scale import apply_ui_font_scale, current_ui_font_scale_percent
from ui_qt.utils.theme_manager import ThemeManager
from ui_qt.widgets.setting_tile import FieldTile, InfoTile, SettingTile, TileBase
from ui_qt.widgets.nav_rail import NavRail
from ui_qt.widgets.wrapped_label import WrappedLabel


class TestSettingsGeneralLayout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_uses_rail_and_independently_scrollable_pages(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            self.assertIsInstance(dialog.rail, NavRail)
            self.assertEqual(
                dialog.rail.keys(),
                (
                    GENERAL,
                    RECORDING,
                    CLEANUP,
                    CLEANUP_RULES,
                    CLEANUP_PROFILES,
                    MEETING_INTELLIGENCE,
                    MEETING_FAST,
                    MEETING_AFTER,
                    MEETING_DASHBOARD,
                    API_KEYS,
                    HOTKEYS,
                    ADVANCED,
                ),
            )
            for key in dialog.rail.keys():
                scroll = dialog._page_scrolls[key]
                self.assertIs(scroll.widget(), dialog._pages[key])
                self.assertTrue(scroll.widgetResizable())
            general = dialog._pages[GENERAL]
            tiles = general.findChildren(SettingTile)
            self.assertEqual(len(tiles), 5)
            self.assertEqual(len(general.findChildren(FieldTile)), 2)
            self.assertIs(dialog.auto_paste_check, dialog.auto_paste_tile.checkbox)
            self.assertIs(
                dialog.update_notify_check, dialog.update_notify_tile.checkbox
            )
            self.assertIs(
                dialog.ui_font_scale_tile.control, dialog.ui_font_scale_combo
            )
            self.assertIs(dialog.ui_theme_tile.control, dialog.ui_theme_combo)
            group_titles = [
                label.text()
                for label in general.findChildren(QLabel)
                if label.objectName() == "settingsTileGroupTitle"
            ]
            self.assertEqual(
                group_titles, ["OUTPUT", "WINDOW", "APPEARANCE", "UPDATES"]
            )
            recording = dialog._pages[RECORDING]
            forms = recording.findChildren(QFormLayout)
            self.assertGreaterEqual(len(forms), 2)
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
            # The AI-cleanup switch plus the TypeSafe sensitivity gate.
            self.assertEqual(len(cleanup.findChildren(SettingTile)), 2)
            self.assertIs(
                dialog.cleanup_sensitivity_gate_check,
                dialog.cleanup_sensitivity_gate_tile.checkbox,
            )
            self.assertEqual(len(cleanup.findChildren(FieldTile)), 1)
            self.assertEqual(len(cleanup.findChildren(InfoTile)), 1)
            self.assertIs(
                dialog.transcript_cleanup_check,
                dialog.transcript_cleanup_tile.checkbox,
            )
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
                dialog.open_cleanup_btn.parentWidget(),
                dialog.cleanup_rules_gate_tile,
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
                dialog.meeting_model_summary.parentWidget(),
                dialog.meeting_model_tile.body,
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
            self.assertEqual(len(advanced.findChildren(FieldTile)), 1)
            self.assertIs(
                dialog.developer_mode_check, dialog.developer_mode_tile.checkbox
            )
            self.assertIs(dialog.hf_policy_tile.control, dialog.hf_policy_combo)
            hotkeys = dialog._pages[HOTKEYS]
            shortcut_cards = [
                card
                for card in hotkeys.findChildren(QFrame)
                if card.objectName() == "hotkeyShortcutCard"
            ]
            self.assertEqual(len(shortcut_cards), 5)
            self.assertEqual(
                set(dialog.hotkey_inputs),
                {
                    "record_toggle",
                    "cancel",
                    "meeting_toggle",
                    "enable_disable",
                    "minimize_tray",
                },
            )
        finally:
            dialog.close()

    def test_general_tiles_toggle_and_gate_update_notify(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
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
        try:
            dialog.transcript_cleanup_check.setChecked(True)
            self.assertTrue(dialog.cleanup_prompt_tile.isEnabled())
            self.assertTrue(dialog.cleanup_rules_composer_tile.isEnabled())
            self.assertTrue(dialog.cleanup_rules_gate_tile.isHidden())
            dialog.transcript_cleanup_check.setChecked(False)
            for tile in (
                dialog.cleanup_prompt_tile,
                dialog.cleanup_rules_composer_tile,
                dialog.cleanup_rules_library_tile,
            ):
                self.assertFalse(tile.isEnabled())
            self.assertFalse(dialog.cleanup_rule_add_btn.isEnabled())
            # The model summary stays live so Model Manager is still reachable.
            self.assertTrue(dialog.cleanup_model_tile.isEnabled())
            self.assertTrue(dialog.open_model_manager_btn.isEnabled())
            self.assertFalse(dialog.cleanup_rules_gate_tile.isHidden())
            self.assertTrue(dialog.cleanup_rules_gate_tile.isEnabled())
            self.assertTrue(dialog.open_cleanup_btn.isEnabled())
            self.assertIn(
                "Clean up transcripts with AI",
                dialog.cleanup_rules_gate_tile.description_label.text(),
            )
        finally:
            dialog.close()

    def test_learned_rules_gate_link_opens_cleanup(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            dialog.show()
            self.app.processEvents()
            dialog.transcript_cleanup_check.setChecked(False)
            dialog.rail.select(CLEANUP_RULES)
            self.assertEqual(dialog.rail.current_key(), CLEANUP_RULES)
            dialog.open_cleanup_btn.click()
            self.app.processEvents()
            self.assertEqual(dialog.rail.current_key(), CLEANUP)
            self.assertIs(dialog.stack.currentWidget(), dialog._page_scrolls[CLEANUP])
            self.assertTrue(dialog.transcript_cleanup_check.hasFocus())
        finally:
            dialog.close()

    def test_report_view_tiles_follow_the_final_report_toggle(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
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
        try:
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
                        scroll = dialog._page_scrolls[key]
                        page = dialog._pages[key]
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
            self.assertGreater(dialog.rail.verticalScrollBar().maximum(), 0)
            item_rect = dialog.rail.visualItemRect(dialog.rail.currentItem())
            self.assertTrue(dialog.rail.viewport().rect().contains(item_rect))
        finally:
            dialog.close()
            self.app.setStyleSheet(previous_stylesheet)

    def test_cards_reflow_when_the_window_narrows(self):
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            dialog.show()
            for width in (1200, 800, 1200):
                dialog.resize(width, 600)
                for _ in range(8):
                    self.app.processEvents()
                left, right = dialog.auto_paste_tile, dialog.copy_clipboard_tile
                with self.subTest(width=width):
                    if width == 800:
                        self.assertEqual(left.x(), right.x())
                        self.assertGreater(right.y(), left.geometry().bottom())
                    else:
                        self.assertEqual(left.y(), right.y())
                        self.assertGreater(right.x(), left.geometry().right())
        finally:
            dialog.close()

    def test_tile_titles_and_descriptions_fit_after_font_and_width_changes(self):
        previous_stylesheet = self.app.styleSheet()
        previous_font = self.app.font()
        previous_scale = current_ui_font_scale_percent()
        manager = ThemeManager()
        with patch.object(SettingsDialog, "_load_settings", lambda self: None):
            dialog = SettingsDialog()
        try:
            dialog.show()
            for scale in (100, 115, 130, 100):
                apply_ui_font_scale(scale, app=self.app, theme_manager=manager)
                for width in (980, 800, 1100):
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
            apply_ui_font_scale(previous_scale, app=self.app)
            self.app.setStyleSheet(previous_stylesheet)
            self.app.setFont(previous_font)
