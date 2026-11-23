"""GUI recommendation labels preserve engine identities and user choices."""
import pytest

from tests.helpers import patch_setting_defaults
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QComboBox

from config import config
from services.settings import SettingsKey, SettingsManager
from ui_qt.widgets import speech_backend_picker as picker


@pytest.mark.parametrize("supported,platform_tag", [
    (True, "darwin_arm64"),
    (False, "win_amd64"),
])
def test_engine_picker_orders_names_and_recommends_only_supported_macs(monkeypatch, supported, platform_tag):
    from services.local_asr.catalog import backend_supported_here

    monkeypatch.setattr(picker, "parakeet_mlx_supported", lambda: supported)
    monkeypatch.setattr("services.components.current_platform_tag", lambda: platform_tag)
    combo = QComboBox()
    picker.populate_backend_combo(combo)
    expected = [
        config.MODEL_VALUE_MAP[name]
        for name in sorted(config.MODEL_CHOICES, key=str.casefold)
        if backend_supported_here(config.MODEL_VALUE_MAP[name], platform_tag)
    ]
    assert [combo.itemData(i) for i in range(combo.count())] == expected
    cpu = combo.findData("parakeet")
    assert combo.itemText(cpu) == ("Parakeet (CPU)" if supported else "Parakeet")
    mlx = combo.findData("parakeet_mlx")
    if supported:
        assert mlx >= 0
        assert combo.itemText(mlx) == "Parakeet MLX (recommended)"
        assert "Apple GPU" in combo.itemData(mlx, Qt.ItemDataRole.ToolTipRole)
        assert combo.findData("parakeet_unified") == -1
    else:
        assert mlx == -1
        assert combo.findData("parakeet_unified") >= 0


@pytest.mark.parametrize("tab_name", ["quick_record_tab", "upload_file_tab"])
def test_annotated_selection_emits_canonical_name_and_syncs_silently(monkeypatch, tab_name):
    from importlib import import_module

    monkeypatch.setattr(picker, "parakeet_mlx_supported", lambda: True)
    module = import_module(f"ui_qt.widgets.{tab_name}")
    tab_type = module.QuickRecordTab if tab_name == "quick_record_tab" else module.UploadFileTab
    tab = tab_type()
    changes = []
    tab.model_changed.connect(changes.append)
    tab.choose_backend("Parakeet MLX")
    assert changes == ["Parakeet MLX"]
    assert tab.current_backend() == "Parakeet MLX"
    assert tab.model_combo.currentText() == "Parakeet MLX (recommended)"
    assert tab.local_engine.model_combo.currentData() == "parakeet-v3-mlx"
    tab.set_model_selection("parakeet")
    assert tab.model_combo.currentText() == "Parakeet (CPU)"
    assert tab.current_backend() == "Parakeet"
    assert changes == ["Parakeet MLX"]


def test_fresh_mac_settings_select_mlx_for_dictation_and_meetings(monkeypatch, tmp_path):
    from tests.test_settings_models import _Host, _isolated_settings

    monkeypatch.setattr(picker, "parakeet_mlx_supported", lambda: True)
    monkeypatch.setattr(config, "DEFAULT_BACKEND", "parakeet_mlx")
    patch_setting_defaults(monkeypatch, {
        SettingsKey.SELECTED_MODEL: "parakeet_mlx",
        SettingsKey.MEETING_ASR_MODEL: "parakeet-v3-mlx",
    })
    manager = SettingsManager(str(tmp_path / "settings.json"))
    with _isolated_settings(manager):
        host = _Host(lambda: None)
        host.models.refresh()
        assert host.models.engine_combo.currentData() == "parakeet_mlx"
        assert "Apple GPU" in host.models.engine_caption.text()
        meeting = host.models.meeting_whisper_picker
        assert meeting.current_model() == "parakeet-v3-mlx"
        assert "recommended" in meeting.model_combo.currentText()
        assert "Apple GPU" in meeting.caption_label.text()
        assert manager.load_all_settings() == {}

        manager.save_model_selection("local_whisper")
        manager.save_setting(SettingsKey.MEETING_WHISPER_MODEL, "auto")
        host.models.refresh()
        assert host.models.engine_combo.currentData() == "local_whisper"
        assert meeting.current_model() == "auto"
        assert "Parakeet MLX" in host.models.engine_caption.text()
