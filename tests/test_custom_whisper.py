"""Custom sources use the chosen folder, with discovery and explicit UI opt-in."""
from pathlib import Path
from unittest.mock import patch

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QDialog

from services import hf_access
from services.settings import SettingsKey, resolve_meeting_whisper_model
from services.whisper_sources import (
    cached_model_path,
    custom_models,
    discover_cached_models,
    discover_hub_models_in_process,
    discover_local_models,
    hub_source,
    parse_source,
    validate_model_folder,
)
from tests.fakes.settings import InMemorySettings
from ui_qt.dialogs import custom_whisper_dialog as dialog_module
from ui_qt.dialogs import settings_downloads as downloads_module
from ui_qt.widgets.local_model_picker import LocalModelPicker


def model_folder(path):
    path.mkdir(parents=True, exist_ok=True)
    for name, content in (("model.bin", b"weights"), ("config.json", b"{}"),
                          ("tokenizer.json", b"{}")):
        (path / name).write_bytes(content)
    return str(path.resolve())


def test_source_parses_nested_hub_folder_and_local_path(tmp_path):
    source = parse_source("owner/model/exports/ct2_int8")
    assert (source.repo_id, source.subfolder) == ("owner/model", "exports/ct2_int8")
    assert all(pattern.startswith("exports/ct2_int8/") for pattern in source.patterns)
    path = model_folder(tmp_path / "local model")
    assert parse_source(path).local_path == path
    assert parse_source("base").repo_id == "Systran/faster-whisper-base"


@pytest.mark.parametrize("folder", ["../outside", "/absolute", "a/../b", "a//b", "a/*", "C:/models"])
def test_hub_subfolder_rejects_traversal_and_globs(folder):
    with pytest.raises(ValueError):
        hub_source("owner/model", folder)


def test_local_discovery_reports_all_ready_folders_and_skips_partial(tmp_path):
    first = model_folder(tmp_path / "ct2_int8")
    second = model_folder(tmp_path / "exports" / "ct2_float16")
    partial = tmp_path / "incomplete"
    partial.mkdir()
    (partial / "model.bin").write_bytes(b"weights")
    assert discover_local_models(tmp_path) == sorted([first, second])
    assert not hf_access.is_model_cached(str(partial))
    with pytest.raises(ValueError, match="missing model files"):
        validate_model_folder(partial)


def test_local_discovery_skips_invalid_config(tmp_path):
    folder = Path(model_folder(tmp_path / "bad"))
    (folder / "config.json").write_text("not json")
    assert discover_local_models(tmp_path) == []


def test_hub_discovery_lists_root_and_nested_variants_without_downloading():
    files = ["model.bin", "config.json", "tokenizer.json", "README.md"]
    files += [f"{folder}/{name}" for folder in ("ct2_int8", "exports/ct2_float16")
              for name in ("model.bin", "config.json", "tokenizer.json")]
    files += ["incomplete/model.bin"]
    with patch("huggingface_hub.HfApi.list_repo_files", return_value=files) as listing, \
         patch("huggingface_hub.snapshot_download") as download:
        assert discover_hub_models_in_process("owner/model") == [
            "owner/model", "owner/model/ct2_int8", "owner/model/exports/ct2_float16"]
        assert discover_hub_models_in_process("owner/model", "exports") == [
            "owner/model/exports/ct2_float16"]
    assert listing.call_count == 2
    download.assert_not_called()


def test_cached_nested_source_is_offline_and_does_not_use_sibling(tmp_path):
    expected = model_folder(tmp_path / "ct2_int8")
    with patch("huggingface_hub.snapshot_download", return_value=str(tmp_path)) as snapshot:
        assert cached_model_path("owner/model/ct2_int8") == expected
        assert hf_access.is_model_cached("owner/model/ct2_int8")
        assert not hf_access.is_model_cached("owner/model/ct2_float16")
    assert all(call.kwargs["local_files_only"] for call in snapshot.call_args_list)
    assert hf_access.resolve_model_repo("owner/model/ct2_int8") == "owner/model"


def test_download_filters_selected_subfolder_and_returns_loadable_directory(tmp_path):
    expected = model_folder(tmp_path / "ct2_int8_float16")
    with patch("huggingface_hub.snapshot_download", return_value=str(tmp_path)) as snapshot:
        actual = hf_access._download_model_files_in_process("owner/model/ct2_int8_float16")
    assert actual == expected
    assert snapshot.call_args.args == ("owner/model",)
    assert snapshot.call_args.kwargs["local_files_only"] is False
    assert all(pattern.startswith("ct2_int8_float16/")
               for pattern in snapshot.call_args.kwargs["allow_patterns"])


def test_download_does_not_accept_an_empty_or_wrong_folder(tmp_path):
    model_folder(tmp_path / "other")
    with patch("huggingface_hub.snapshot_download", return_value=str(tmp_path)):
        with pytest.raises(ValueError, match="missing model files"):
            hf_access._download_model_files_in_process("owner/model/chosen")


def test_local_source_never_downloads_and_cannot_be_deleted_as_hub_cache(tmp_path):
    folder = model_folder(tmp_path / "mine")
    with patch("huggingface_hub.snapshot_download") as download:
        assert hf_access._download_model_files_in_process(folder) == folder
        assert hf_access.is_model_cached(folder)
        with pytest.raises(ValueError, match="source files are retained"):
            hf_access.delete_model_from_cache(folder)
    download.assert_not_called()


def test_cache_discovery_preserves_repo_subfolder_identity(tmp_path):
    repo = tmp_path / "models--owner--model"
    model_folder(repo / "snapshots" / "abc" / "ct2_int8")
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("abc")
    info = hf_access.CachedModelInfo("owner/model", 10, str(repo), ("abc",))
    assert discover_cached_models({"owner/model": info}) == ["owner/model/ct2_int8"]


def test_cache_discovery_does_not_offer_detached_old_variants(tmp_path):
    repo = tmp_path / "models--owner--model"
    model_folder(repo / "snapshots" / "old" / "ct2_int8")
    (repo / "snapshots" / "new").mkdir()
    (repo / "refs").mkdir()
    (repo / "refs" / "main").write_text("new")
    info = hf_access.CachedModelInfo("owner/model", 10, str(repo), ("old", "new"))
    assert discover_cached_models({"owner/model": info}) == []


def test_custom_choices_survive_refresh_for_both_model_assignments(tmp_path):
    local = model_folder(tmp_path / "mine")
    hub = "owner/model/ct2_int8"
    settings = {SettingsKey.CUSTOM_WHISPER_MODELS: [local, hub],
                SettingsKey.MEETING_WHISPER_MODEL: hub}
    assert resolve_meeting_whisper_model(settings) == hub
    picker = LocalModelPicker()
    signals = []
    picker.model_changed.connect(signals.append)
    picker.set_options({}, local, custom=custom_models(settings))
    assert picker.current_model() == local
    assert picker.model_combo.findData(hub) >= 0
    assert signals == []
    picker.model_combo.setCurrentIndex(picker.model_combo.findData(hub))
    assert signals == [hub]


def test_discovery_results_require_selection_and_adding_does_not_activate(monkeypatch):
    monkeypatch.setattr(dialog_module.QTimer, "singleShot", lambda *_: None)
    dialog = dialog_module.CustomWhisperDialog({}, {})
    dialog._show_found(0, ["owner/model/ct2_int8", "owner/model/ct2_float16"], "")
    assert dialog.results.count() == 2
    assert not dialog.add_button.isEnabled()
    dialog.results.item(0).setCheckState(Qt.CheckState.Checked)
    assert dialog.add_button.isEnabled()
    dialog._add_selected()
    values = {SettingsKey.WHISPER_MODEL: "base", SettingsKey.MEETING_WHISPER_MODEL: "auto"}
    monkeypatch.setattr(dialog_module, "CustomWhisperDialog", lambda *_: dialog)
    monkeypatch.setattr(dialog, "exec", lambda: QDialog.DialogCode.Accepted)
    dialog_module.add_custom_models(None, InMemorySettings(values), {})
    assert values[SettingsKey.CUSTOM_WHISPER_MODELS] == dialog.selected_models
    assert values[SettingsKey.WHISPER_MODEL] == "base"
    assert values[SettingsKey.MEETING_WHISPER_MODEL] == "auto"


def test_discovery_offline_still_allows_local_and_cached_models(monkeypatch):
    monkeypatch.setattr(dialog_module.QTimer, "singleShot", lambda *_: None)
    monkeypatch.setattr(dialog_module, "is_hf_hub_offline_env_set", lambda: True)
    dialog = dialog_module.CustomWhisperDialog({}, {})
    assert not dialog.hub_button.isEnabled()
    assert dialog.local_button.isEnabled()
    assert dialog.cached_button.isEnabled()


def test_downloads_tracks_variants_individually_and_remove_keeps_files(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    folder = model_folder(snapshot / "ct2_int8")
    local = model_folder(tmp_path / "mine")
    first, second = "owner/model/ct2_int8", "owner/model/ct2_float16"
    values = {SettingsKey.CUSTOM_WHISPER_MODELS: [first, second, local]}
    monkeypatch.setattr(downloads_module, "settings_manager", InMemorySettings(values))
    monkeypatch.setattr(downloads_module, "scan_cached_models", lambda **_: {})
    monkeypatch.setattr("services.local_asr.cache.inventory", lambda: {})
    with patch("huggingface_hub.snapshot_download", return_value=str(snapshot)):
        page = downloads_module.DownloadsPage(background_cache_scan=False)
        page.refresh()
        assert page.rows[first].is_cached
        assert not page.rows[second].is_cached
        assert page.rows[local].is_cached
        page.select_model(second)
        page.rows[second].select_checkbox.setChecked(True)
        assert "size unknown" in page.selection_summary.text()
        page._on_delete_clicked(local)
    assert Path(local).is_dir()
    assert local not in page.rows
    assert Path(folder).is_dir()


def test_backend_loads_resolved_folder_but_retains_source_identity(tmp_path, monkeypatch):
    from transcriber import local_backend
    folder = model_folder(tmp_path / "ct2_int8")
    built = []
    monkeypatch.setattr(local_backend, "WhisperModel",
                        lambda name, **kwargs: built.append((name, kwargs)) or object())
    monkeypatch.setattr(local_backend.LocalWhisperBackend, "_detect_hardware",
                        lambda _: ("cpu", "int8", "base"))
    with patch("huggingface_hub.snapshot_download", return_value=str(tmp_path)):
        backend = local_backend.LocalWhisperBackend(model_name="owner/model/ct2_int8")
    assert backend.is_available()
    assert built[0][0] == folder
    assert built[0][1]["local_files_only"] is True
    assert backend.last_loaded_model == "owner/model/ct2_int8"


def test_missing_manual_folder_is_a_load_error_not_a_download_request(tmp_path, monkeypatch):
    from transcriber import local_backend
    monkeypatch.setattr(local_backend.LocalWhisperBackend, "_detect_hardware",
                        lambda _: ("cpu", "int8", "base"))
    backend = local_backend.LocalWhisperBackend(model_name=str(tmp_path / "gone"))
    assert not backend.is_available()
    assert not backend.is_model_missing
