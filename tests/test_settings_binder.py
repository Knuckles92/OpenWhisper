"""SettingsBinder and the Settings controls it loads and saves."""
import os
import tempfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication, QCheckBox, QComboBox, QSpinBox

from services.settings import SettingsKey, SettingsManager
from ui_qt.dialogs import settings_dialog as settings_dialog_module
from ui_qt.dialogs.settings_binder import SettingsBinder


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


class _Recorder:
    def __init__(self):
        self.loading = False
        self.saved = []

    def __call__(self, key, value):
        if self.loading:
            return False
        self.saved.append((key, value))
        return True


def test_checkbox_saves_on_toggle_and_loads_resolved_value(app):
    persist = _Recorder()
    binder = SettingsBinder(persist)
    check = QCheckBox()
    binder.checkbox(check, "flag", lambda settings: settings.get("flag", True))

    check.setChecked(True)
    check.setChecked(False)
    assert persist.saved == [("flag", True), ("flag", False)]

    persist.loading = True
    binder.load({})
    assert check.isChecked()
    binder.load({"flag": False})
    assert not check.isChecked()
    assert persist.saved == [("flag", True), ("flag", False)]


def test_combo_saves_item_data_and_falls_back_to_first_item(app):
    persist = _Recorder()
    binder = SettingsBinder(persist)
    combo = QComboBox()
    combo.addItem("Normal", "normal")
    combo.addItem("Thorough", "thorough")
    binder.combo(combo, "mode", lambda settings: settings.get("mode"))

    combo.setCurrentIndex(1)
    assert persist.saved == [("mode", "thorough")]

    persist.loading = True
    binder.load({"mode": "unknown"})
    assert combo.currentIndex() == 0
    binder.load({"mode": "thorough"})
    assert combo.currentIndex() == 1


def test_spin_saves_int_values(app):
    persist = _Recorder()
    binder = SettingsBinder(persist)
    spin = QSpinBox()
    spin.setMaximum(100)
    binder.spin(spin, "port", lambda settings: settings.get("port", 0))

    spin.setValue(42)
    assert persist.saved == [("port", 42)]
    persist.loading = True
    binder.load({"port": 7})
    assert spin.value() == 7


def test_load_follows_binding_order(app):
    order = []
    binder = SettingsBinder(lambda key, value: None)
    for name in ("first", "second", "third"):
        binder.checkbox(
            QCheckBox(), name, lambda settings, name=name: order.append(name) or True
        )
    binder.load({})
    assert order == ["first", "second", "third"]


def _feature_check(dialog, feature):
    return dialog.typesafe_feature_tiles[feature].checkbox


# (control, key, stored value, checked after load)
_BOUND_CHECKS = [
    (lambda d: d.auto_paste_check, SettingsKey.AUTO_PASTE, False, False),
    (lambda d: d.copy_clipboard_check, SettingsKey.COPY_CLIPBOARD, False, False),
    (lambda d: d.update_notify_check, SettingsKey.UPDATE_NOTIFY_ENABLED, False, False),
    (lambda d: d.meeting_past_recall_check, SettingsKey.MEETING_PAST_RECALL_ENABLED, True, True),
    (lambda d: d.meeting_context_folder_check, SettingsKey.MEETING_CONTEXT_FOLDER_ENABLED, True, True),
    (lambda d: d.typesafe_topic_shift_check, SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED, "yes", False),
    (lambda d: d.typesafe_voice_commands_check, SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED, True, True),
    (lambda d: _feature_check(d, "citations"), SettingsKey.TYPESAFE_CITATIONS_ENABLED, True, True),
    (lambda d: _feature_check(d, "highlights"), SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED, True, True),
    (lambda d: d.meeting_end_redecode_check, SettingsKey.MEETING_END_REDECODE, True, True),
    (lambda d: d.meeting_end_polish_check, SettingsKey.MEETING_END_POLISH, True, True),
    (lambda d: d.meeting_redecode_coverage_guard_check, SettingsKey.MEETING_REDECODE_COVERAGE_GUARD, True, True),
]


@pytest.fixture
def isolated_dialog(app):
    with tempfile.TemporaryDirectory() as temp_dir:
        manager = SettingsManager(os.path.join(temp_dir, "settings.json"))
        stored = {key: value for _, key, value, _ in _BOUND_CHECKS}
        stored[SettingsKey.MEETING_INSIGHT_REVIEW_SENSITIVITY] = "thorough"
        stored[SettingsKey.MEETING_SERVER_PORT] = 8123
        manager.save_all_settings(stored)
        with patch.object(settings_dialog_module, "settings_manager", manager), patch.object(
            settings_dialog_module.history_manager, "set_retention"
        ):
            dialog = settings_dialog_module.SettingsDialog(
                get_loaded_model=lambda: None, background_cache_scan=False
            )
            yield dialog, manager, stored
            dialog._loading = True
            dialog.deleteLater()


def test_bound_controls_load_without_saving(isolated_dialog):
    dialog, manager, stored = isolated_dialog
    for control, _key, _value, checked in _BOUND_CHECKS:
        assert control(dialog).isChecked() is checked
    assert dialog.meeting_review_sensitivity.currentData() == "thorough"
    assert dialog.meeting_port_spinbox.value() == 8123
    assert manager.load_all_settings() == stored


def test_bound_checkbox_saves_its_key_immediately(isolated_dialog, subtests):
    dialog, manager, stored = isolated_dialog
    for control, key, _value, checked in _BOUND_CHECKS:
        with subtests.test(key=key):
            # Reset both persistence and the control before every case so a
            # preceding toggle cannot satisfy the next assertion.
            manager.save_all_settings(stored)
            dialog._loading = True
            check = control(dialog)
            check.setChecked(checked)
            dialog._loading = False
            check.setChecked(not checked)
            assert check.isChecked() is not checked
            assert manager.load_all_settings() == {**stored, key: not checked}


def test_bound_combo_and_spin_save_their_keys_immediately(isolated_dialog):
    dialog, manager, _stored = isolated_dialog
    dialog.meeting_review_sensitivity.setCurrentIndex(
        dialog.meeting_review_sensitivity.findData("normal")
    )
    dialog.meeting_port_spinbox.setValue(9000)
    saved = manager.load_all_settings()
    assert saved[SettingsKey.MEETING_INSIGHT_REVIEW_SENSITIVITY] == "normal"
    assert saved[SettingsKey.MEETING_SERVER_PORT] == 9000
