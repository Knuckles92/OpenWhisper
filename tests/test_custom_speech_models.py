"""Custom models for the local speech engines: registry, formats, cache, Host Mode."""
from pathlib import Path
from unittest.mock import patch

import pytest

from services.local_asr import cache, custom
from services.local_asr.catalog import BUILTIN_MODELS, MODELS, selected_model
from services.settings import SettingsKey, settings_manager


def gguf(path: Path, body: bytes = b"\x03\x00\x00\x00rest") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF" + body)
    return path


def moonshine_folder(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for name in custom.model_format("moonshine").required:
        (path / name).write_bytes(b"x")
    return path


def test_a_registered_gguf_is_a_model_of_its_engine_everywhere(tmp_path):
    source = gguf(tmp_path / "models" / "my-parakeet.q8.gguf")

    key = custom.register("parakeet", source, "My Parakeet")

    assert key == "custom-my-parakeet"
    model = MODELS[key]
    assert (model.backend, model.label, model.custom, model.path) == (
        "parakeet", "My Parakeet", True, str(source.resolve()))
    # Abilities come from the engine's default model; languages from the model itself.
    default = BUILTIN_MODELS["parakeet-v3"]
    assert (model.meeting, model.streaming) == (default.meeting, default.streaming)
    assert model.languages == "Languages depend on the model"
    assert key in list(MODELS) and key not in BUILTIN_MODELS
    assert cache.is_cached(key)
    assert cache.load_path(key) == str(source.resolve())
    assert cache.inventory()[key].size_bytes == source.stat().st_size

    settings_manager.save_setting(SettingsKey.LOCAL_ASR_MODELS, {"parakeet": key})
    assert selected_model("parakeet", settings_manager.load_all_settings()) == key


def test_details_describe_the_file_without_inventing_a_publisher(tmp_path):
    from services.model_catalog import get_model_details

    key = custom.register("nemotron", gguf(tmp_path / "n.gguf"), "Live one")
    details = get_model_details(key)

    assert details.family == "nemotron"
    assert details.download_size_mb == 0 and details.download_hosts == ()
    assert details.runtime_format == "GGUF"
    # Links open the model's folder, never the .gguf itself.
    assert details.origin_url == tmp_path.resolve().as_uri()
    assert any("paired computers" in line for line in details.limitations)
    with pytest.raises(KeyError):
        get_model_details("custom-not-registered")


def test_adding_the_same_file_again_keeps_its_key(tmp_path):
    source = gguf(tmp_path / "a.gguf")
    first = custom.register("parakeet", source, "First")
    again = custom.register("parakeet", source, "Renamed")

    assert first == again
    assert MODELS[first].label == "Renamed"
    assert len(SettingsKey.CUSTOM_SPEECH_MODELS.read(settings_manager.load_all_settings())) == 1
    # The key outlives a rename; another file whose label makes the same key gets its own.
    assert custom.register("parakeet", gguf(tmp_path / "b.gguf"), "First") == "custom-first-2"


@pytest.mark.parametrize("name, body, message", [
    ("model.bin", b"GGUF", "ending in .gguf"),
    ("model.gguf", b"NOPE", "not a GGUF model file"),
])
def test_a_gguf_engine_refuses_other_files(tmp_path, name, body, message):
    path = tmp_path / name
    path.write_bytes(body)
    with pytest.raises(ValueError, match=message):
        custom.register("parakeet", path)
    with pytest.raises(ValueError, match="is not one"):
        custom.register("parakeet", tmp_path)


def test_a_folder_engine_names_the_files_it_is_missing(tmp_path):
    folder = moonshine_folder(tmp_path / "moon")
    (folder / "encoder.ort").unlink()

    with pytest.raises(ValueError, match="encoder.ort"):
        custom.register("moonshine", folder, arch="small")


def test_moonshine_needs_its_size_and_passes_it_to_the_worker(tmp_path):
    folder = moonshine_folder(tmp_path / "moon")
    with pytest.raises(ValueError, match="Small or Medium"):
        custom.register("moonshine", folder)

    key = custom.register("moonshine", folder, "Mine", arch="medium")

    assert MODELS[key].arch == "medium"
    assert {BUILTIN_MODELS["moonshine-small"].arch, BUILTIN_MODELS["moonshine-medium"].arch} == {
        "small", "medium"}


def test_qwen_on_a_mac_takes_a_safetensors_folder(tmp_path):
    with patch("services.components.current_platform_tag", return_value="darwin_arm64"):
        fmt = custom.model_format("qwen_asr")
        assert not fmt.gguf and fmt.weights == "*.safetensors"
        assert "config.json" in fmt.required and "model.safetensors" not in fmt.required
        folder = tmp_path / "fourier"
        folder.mkdir()
        for name in fmt.required:
            (folder / name).write_text("{}")
        with pytest.raises(ValueError, match=r"\*\.safetensors"):
            custom.register("qwen_asr", folder)
        (folder / "model-00001-of-00002.safetensors").write_bytes(b"w")
        assert MODELS[custom.register("qwen_asr", folder)].backend == "qwen_asr"
    with patch("services.components.current_platform_tag", return_value="win_amd64"):
        assert custom.model_format("qwen_asr").gguf


def test_a_moved_file_reads_as_missing_and_is_never_downloaded(tmp_path):
    source = gguf(tmp_path / "a.gguf")
    key = custom.register("parakeet", source, "Gone")
    source.unlink()

    assert not cache.is_cached(key)
    assert key not in cache.inventory()
    with pytest.raises(RuntimeError, match="no longer at"):
        cache.load_path(key)
    with pytest.raises(RuntimeError, match="Add it again"):
        cache.download(key)


def test_removing_forgets_the_model_and_keeps_its_files(tmp_path):
    source = gguf(tmp_path / "a.gguf")
    key = custom.register("parakeet", source, "Mine")
    with pytest.raises(ValueError, match="files are kept"):
        cache.delete(key)

    settings_manager.save_setting(SettingsKey.LOCAL_ASR_MODELS, {"parakeet": key})
    with pytest.raises(ValueError, match="Choose another model"):
        custom.unregister(key)
    settings_manager.save_setting(SettingsKey.LOCAL_ASR_MODELS, {})
    custom.unregister(key)

    assert key not in MODELS
    assert source.exists()
    # A selection left pointing at it falls back to the engine's default.
    settings_manager.save_setting(SettingsKey.LOCAL_ASR_MODELS, {"parakeet": key})
    assert selected_model("parakeet", settings_manager.load_all_settings()) != key


def test_broken_entries_are_skipped():
    models = custom.parse([
        {"key": "custom-ok", "backend": "parakeet", "path": "C:/m.gguf"},
        {"key": "no-prefix", "backend": "parakeet", "path": "C:/m.gguf"},
        {"key": "custom-engine", "backend": "nope", "path": "C:/m.gguf"},
        {"key": "custom-moon", "backend": "moonshine", "path": "C:/m"},
        "not a dict",
    ])
    assert list(models) == ["custom-ok"]
    assert models["custom-ok"].label == "m"


def test_a_host_never_offers_or_accepts_a_custom_model(tmp_path):
    from services.remote_asr import engines
    from services.remote_asr.model_management import model_choices
    from services.runtime.remote import _save_engine_model

    key = custom.register("parakeet", gguf(tmp_path / "a.gguf"), "Mine")

    assert key not in {choice["model"] for choice in model_choices()}
    # Runtime installed and the file present: it would load, yet isn't offered.
    with patch("services.components.is_installed", return_value=True), \
            patch("services.hf_access.scan_cached_models", return_value={}):
        offered = engines.host_models()
    assert cache.is_cached(key)
    assert key not in {entry["model"] for entry in offered}
    assert _save_engine_model("parakeet", key) is None
    assert SettingsKey.LOCAL_ASR_MODELS.read(settings_manager.load_all_settings()).get("parakeet") != key


def test_diagnostics_never_report_a_custom_model(tmp_path):
    from config import data_root
    from services.diagnostics import collect_report

    key = custom.register("parakeet", gguf(tmp_path / "secret-name.gguf"), "Secret name")
    settings_manager.save_setting(SettingsKey.LOCAL_ASR_MODELS, {"parakeet": key})

    report = collect_report(root=Path(data_root()))

    assert key not in repr(report) and "secret" not in repr(report).casefold()


def test_only_engines_with_a_runtime_here_can_take_a_custom_model():
    with patch("services.components.current_platform_tag", return_value="win_amd64"):
        addable = custom.addable_backends()
    assert "parakeet" in addable and "parakeet_mlx" not in addable


def test_a_moved_custom_file_is_a_load_error_not_a_download_request(tmp_path):
    from transcriber.optional_backend import LocalSpeechBackend

    source = gguf(tmp_path / "a.gguf")
    key = custom.register("parakeet", source, "Mine")
    source.unlink()
    backend = LocalSpeechBackend("parakeet", key)

    assert not backend.is_model_missing
    with patch("services.components.is_installed", return_value=True):
        backend.reload_model()
    assert not backend.is_available()
    assert "no longer at" in backend.last_error


def test_downloads_lists_a_custom_model_with_remove_not_download(tmp_path, monkeypatch, qapp):
    from services.hf_access import invalidate_cached_models_snapshot
    from ui_qt.dialogs import settings_downloads as downloads_module

    source = gguf(tmp_path / "mine.gguf")
    key = custom.register("parakeet", source, "Mine")
    invalidate_cached_models_snapshot()
    monkeypatch.setattr(downloads_module, "scan_cached_models",
                        lambda **_: cache.inventory())
    page = downloads_module.DownloadsPage(background_cache_scan=False)
    page.refresh()

    row = page.rows[key]
    assert row.is_cached and row.backend == "parakeet"
    assert not row.download_button.isVisibleTo(page)
    assert row.delete_button.text() == "Remove"
    assert key not in page._missing_names()

    page._on_delete_clicked(key)
    assert "Removed" in page.message_label.text(), page.message_label.text()
    assert key not in MODELS
    assert key not in page.rows
    assert source.exists()


def test_the_dialog_adds_a_model_and_explains_a_wrong_file(tmp_path, qapp):
    from ui_qt.dialogs.custom_speech_dialog import CustomSpeechModelDialog

    wrong = tmp_path / "notes.txt"
    wrong.write_text("hi")
    dialog = CustomSpeechModelDialog("parakeet")
    dialog.path_edit.setText(str(wrong))
    assert dialog.label_edit.text() == "notes"
    dialog._add()
    assert not dialog.status_label.isHidden() and ".gguf" in dialog.status_label.text()
    assert not dialog.added_key

    dialog.path_edit.setText(str(gguf(tmp_path / "good.gguf")))
    dialog.label_edit.setText("Good one")
    dialog._label_edited("Good one")
    dialog._add()
    assert MODELS[dialog.added_key].label == "Good one"


def test_moonshine_dialog_waits_for_a_size(tmp_path, qapp):
    from ui_qt.dialogs.custom_speech_dialog import CustomSpeechModelDialog

    dialog = CustomSpeechModelDialog("moonshine")
    dialog.path_edit.setText(str(moonshine_folder(tmp_path / "moon")))
    assert not dialog.add_button.isEnabled()
    dialog.arch_combo.setCurrentIndex(dialog.arch_combo.findData("small"))
    assert dialog.add_button.isEnabled()
    dialog._add()
    assert MODELS[dialog.added_key].arch == "small"
