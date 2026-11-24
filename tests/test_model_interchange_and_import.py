"""Tests for GGUF metadata inspection, capability decoupling, external linking, and model import."""
from __future__ import annotations

import hashlib
import json
import os
import struct
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from services.local_asr import cache, custom, discovery, gguf_meta
from services.local_asr.catalog import MODELS, is_english_only_model
from services.settings import SettingsKey, settings_manager
from tests.fakes.settings import InMemorySettings


def _write_gguf(path: Path, kv: dict[str, object], *, extra_tail: bytes = b"") -> Path:
    """Write a minimal valid GGUF v3 file with the given metadata key-value pairs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = bytearray()
    buf.extend(b"GGUF")
    buf.extend(struct.pack("<IQQ", 3, 0, len(kv)))

    def pack_str(text: str) -> bytes:
        raw = text.encode("utf-8")
        return struct.pack("<Q", len(raw)) + raw

    for key, value in kv.items():
        buf.extend(pack_str(key))
        if isinstance(value, bool):
            buf.extend(struct.pack("<I?", 7, value))
        elif isinstance(value, int):
            buf.extend(struct.pack("<II", 4, value))
        elif isinstance(value, str):
            buf.extend(struct.pack("<I", 8))
            buf.extend(pack_str(value))
        elif isinstance(value, list):
            # Array of strings (type 8)
            buf.extend(struct.pack("<IIQ", 9, 8, len(value)))
            for item in value:
                buf.extend(pack_str(str(item)))
        else:
            raise TypeError(f"Unsupported test value: {value!r}")

    buf.extend(extra_tail)
    path.write_bytes(bytes(buf))
    return path


def _fake_builtin_spec(key: str, filename: str, data: bytes) -> dict:
    return {
        "repo": f"test-owner/{key}",
        "revision": "rev-test-1",
        "files": [
            {
                "name": filename,
                "url": f"https://example.invalid/{filename}",
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        ],
    }


# ---- GGUF metadata inspection & rejection ----


def test_gguf_inspector_rejects_non_speech_llm_and_routes_speech_engines(tmp_path):
    llm = _write_gguf(tmp_path / "llama-3-8b.Q4_K_M.gguf", {
        "general.architecture": "llama",
        "general.name": "Llama 3 8B",
        "tokenizer.ggml.tokens": ["<s>", "hello", "world"],
        "llama.block_count": 32,
    })
    info = gguf_meta.inspect_gguf(llm)
    assert info.valid_magic and info.kind == "non_speech" and info.architecture == "llama"
    assert "text/LLM" in info.rejection_reason("parakeet", llm.name)
    with pytest.raises(ValueError, match="text/LLM"):
        custom.register("parakeet", llm)

    # Qwen3 text LLM without audio keys is also rejected.
    qwen_llm = _write_gguf(tmp_path / "qwen3-8b.gguf", {
        "general.architecture": "qwen3",
        "qwen3.block_count": 36,
    })
    assert gguf_meta.inspect_gguf(qwen_llm).kind == "non_speech"
    with pytest.raises(ValueError, match="text/LLM"):
        custom.register("qwen_asr", qwen_llm)

    # Qwen3-ASR GGUF routes to qwen_asr (transcribe.cpp) and rejects NeMo/parakeet_unified.
    qwen_asr = _write_gguf(tmp_path / "qwen3-asr-0.6b.gguf", {
        "general.architecture": "qwen3_asr",
        "general.name": "Qwen3-ASR 0.6B Custom",
        "transcribe.family": "qwen3_asr",
    })
    q_info = gguf_meta.inspect_gguf(qwen_asr)
    assert q_info.kind == "speech" and q_info.runtime == "transcribe_cpp"
    assert q_info.suggested_backend == "qwen_asr"
    assert q_info.rejection_reason("qwen_asr") == ""
    assert "Qwen3-ASR" in q_info.rejection_reason("parakeet_unified", qwen_asr.name)
    assert "transcribe.cpp" in q_info.rejection_reason("parakeet", qwen_asr.name)

    # NeMo-Speech.cpp ASR GGUF routes to parakeet and rejects transcribe.cpp engines.
    nemo_parakeet = _write_gguf(tmp_path / "parakeet-tdt.gguf", {
        "general.architecture": "asr",
        "general.name": "Parakeet TDT v3 Fine-tune",
        "asr.head_type": "tdt",
        "asr.encoder.d_model": 1024,
    })
    n_info = gguf_meta.inspect_gguf(nemo_parakeet)
    assert n_info.kind == "speech" and n_info.runtime == "nemo"
    assert n_info.suggested_backend == "parakeet"
    assert n_info.rejection_reason("parakeet") == ""
    assert n_info.rejection_reason("nemotron") == ""
    assert "NeMo-Speech.cpp" in n_info.rejection_reason("parakeet_unified", nemo_parakeet.name)
    assert "NeMo-Speech.cpp" in n_info.rejection_reason("qwen_asr", nemo_parakeet.name)

    # Nemotron streaming GGUF routes to nemotron with streaming=True.
    nemotron = _write_gguf(tmp_path / "nemotron-stream.gguf", {
        "general.architecture": "nemotron",
        "nemotron.att_context_style": "chunked_limited",
    })
    nem_info = gguf_meta.inspect_gguf(nemotron)
    assert nem_info.runtime == "nemo" and nem_info.suggested_backend == "nemotron"
    assert nem_info.streaming is True

    # transcribe.cpp Parakeet GGUF routes to parakeet_unified and rejects NeMo/Qwen.
    tc_parakeet = _write_gguf(tmp_path / "parakeet-tdt-ctc-110m-custom.gguf", {
        "general.architecture": "parakeet",
        "parakeet.arch": "hybrid_tdt_ctc",
        "general.name": "Parakeet TDT-CTC 110M Custom",
    })
    tc_info = gguf_meta.inspect_gguf(tc_parakeet)
    assert tc_info.runtime == "transcribe_cpp" and tc_info.suggested_backend == "parakeet_unified"
    assert tc_info.streaming is False
    assert tc_info.rejection_reason("parakeet_unified") == ""
    assert "transcribe.cpp" in tc_info.rejection_reason("parakeet", tc_parakeet.name)
    assert "Parakeet Unified" in tc_info.rejection_reason("qwen_asr", tc_parakeet.name)


# ---- Capability decoupling for custom models ----


def test_custom_parakeet_unified_capabilities_decouple_from_default(tmp_path):
    from services import dictation_language

    source = _write_gguf(tmp_path / "custom-multi.gguf", {
        "general.architecture": "parakeet",
        "parakeet.arch": "hybrid_tdt_ctc",
    })
    key = custom.register(
        "parakeet_unified", source, "Custom Multi",
        streaming=False, meeting=True, multilingual=True,
    )
    model = MODELS[key]
    assert model.streaming is False
    assert model.stream_final is False
    assert model.meeting is True
    assert model.multilingual is True
    assert not is_english_only_model(model)

    settings = {
        SettingsKey.SELECTED_MODEL: "parakeet_unified",
        SettingsKey.LOCAL_ASR_MODELS: {"parakeet_unified": key},
    }
    assert not dictation_language.is_english_only_engine(settings)
    assert len(dictation_language.accepted_languages(settings)) > 1


def test_transcribe_recognizer_respects_english_only_false():
    from services.local_asr.transcribe_engine import TranscribeRecognizer

    rec = object.__new__(TranscribeRecognizer)
    rec._family = "parakeet_unified"
    rec._english_only = False
    rec._stream = None
    rec._stream_key = None
    rec._interrupted = set()
    captured = {}

    class FakeSession:
        def run(self, samples, language=None):
            captured["language"] = language
            return type("R", (), {"text": "hola", "segments": []})()

    rec.session = FakeSession()
    rec._tc = type("TC", (), {"InputTooShort": RuntimeError})()
    result = rec.transcribe([0.1, 0.2], language="es")
    assert result["text"] == "hola"
    assert captured["language"] == "es"


# ---- Zero-copy external linking for built-in models ----


def test_link_external_reuses_builtin_gguf_without_copying_or_deleting_source(tmp_path, monkeypatch):
    external = _write_gguf(tmp_path / "hf_cache" / "parakeet-tdt-0.6b-v3.q8_0.gguf", {
        "general.architecture": "asr",
        "asr.head_type": "tdt",
    }, extra_tail=b"weights-payload")
    spec = _fake_builtin_spec("parakeet-v3", external.name, external.read_bytes())

    real_artifacts = cache.artifacts
    monkeypatch.setattr(cache, "local_app_dir", lambda: str(tmp_path / "app"))
    monkeypatch.setattr(cache, "artifacts", lambda key: spec if key == "parakeet-v3" else real_artifacts(key))

    assert not cache.is_cached("parakeet-v3")
    linked_path = cache.link_external("parakeet-v3", external, verify_hash=True)
    assert linked_path == external.resolve()
    assert cache.is_cached("parakeet-v3")
    assert cache.is_linked("parakeet-v3")
    assert cache.load_path("parakeet-v3") == str(external.resolve())

    # No duplicate copy of the weights was written inside speech-models.
    model_folder = cache.model_dir("parakeet-v3")
    assert (model_folder / cache.LINKED_MANIFEST_NAME).is_file()
    assert not (model_folder / external.name).exists()

    inv = cache.inventory()
    assert inv[spec["repo"]].linked is True
    assert inv[spec["repo"]].path == str(external.resolve())
    assert inv[spec["repo"]].size_bytes == external.stat().st_size

    # Unlinking deletes only the tiny manifest directory, leaving external intact.
    cache.delete("parakeet-v3")
    assert not cache.is_cached("parakeet-v3")
    assert not cache.is_linked("parakeet-v3")
    assert external.is_file()


def test_link_external_detects_missing_target_and_rejects_hash_mismatch(tmp_path, monkeypatch):
    external = _write_gguf(tmp_path / "ext" / "parakeet.gguf", {"general.architecture": "asr"})
    spec = _fake_builtin_spec("parakeet-v3", external.name, external.read_bytes())

    real_artifacts = cache.artifacts
    monkeypatch.setattr(cache, "local_app_dir", lambda: str(tmp_path / "app"))
    monkeypatch.setattr(cache, "artifacts", lambda key: spec if key == "parakeet-v3" else real_artifacts(key))

    # Wrong content of same byte length fails SHA-256 verification.
    wrong = tmp_path / "ext" / "wrong.gguf"
    wrong.write_bytes(b"X" * external.stat().st_size)
    with pytest.raises(ValueError, match="SHA-256"):
        cache.link_external("parakeet-v3", wrong, verify_hash=True)

    # Linking valid file then deleting it makes is_cached cleanly return False.
    cache.link_external("parakeet-v3", external, verify_hash=True)
    assert cache.is_cached("parakeet-v3")
    external.unlink()
    assert not cache.is_cached("parakeet-v3")


# ---- Discovery & Import engine ----


def test_discovery_finds_builtin_and_custom_models_and_skips_llms(tmp_path, monkeypatch):
    hf_root = tmp_path / "hf_hub"
    handy_root = tmp_path / "handy_models"
    app_root = tmp_path / "app"

    # 1. Built-in Parakeet v3 GGUF inside HF Hub cache snapshot.
    snap_p3 = hf_root / "models--nvidia--parakeet-tdt-0.6b-v3" / "snapshots" / "rev-p3"
    p3_file = _write_gguf(snap_p3 / "parakeet-tdt-0.6b-v3.q8_0.gguf", {
        "general.architecture": "asr",
        "asr.head_type": "tdt",
    }, extra_tail=b"p3-bytes-12345")
    (hf_root / "models--nvidia--parakeet-tdt-0.6b-v3" / "refs").mkdir(parents=True)
    (hf_root / "models--nvidia--parakeet-tdt-0.6b-v3" / "refs" / "main").write_text("rev-p3")
    p3_spec = _fake_builtin_spec("parakeet-v3", p3_file.name, p3_file.read_bytes())

    # 2. Non-speech LLM GGUF in HF Hub cache â€” must be ignored!
    snap_llm = hf_root / "models--meta--llama-3" / "snapshots" / "rev-llm"
    _write_gguf(snap_llm / "llama-3.gguf", {
        "general.architecture": "llama",
        "llama.block_count": 32,
    })

    # 3. Custom CTranslate2 Whisper model in HF Hub cache.
    snap_wh = hf_root / "models--community--whisper-custom-ct2" / "snapshots" / "rev-wh"
    snap_wh.mkdir(parents=True)
    (snap_wh / "model.bin").write_bytes(b"whisper-weights")
    (snap_wh / "config.json").write_text(json.dumps({"model_type": "whisper"}))
    (snap_wh / "tokenizer.json").write_text("{}")
    (hf_root / "models--community--whisper-custom-ct2" / "refs").mkdir(parents=True)
    (hf_root / "models--community--whisper-custom-ct2" / "refs" / "main").write_text("rev-wh")

    # 4. Community speech GGUF in Handy folder.
    _write_gguf(handy_root / "custom-qwen-asr.gguf", {
        "general.architecture": "qwen3_asr",
        "general.name": "Community Qwen3 ASR",
    }, extra_tail=b"qwen-bytes")

    real_artifacts = cache.artifacts
    monkeypatch.setattr(cache, "local_app_dir", lambda: str(app_root))
    monkeypatch.setattr(discovery, "local_app_dir", lambda: str(app_root))
    monkeypatch.setattr(discovery, "get_hf_cache_dir", lambda: str(hf_root))
    monkeypatch.setattr(cache, "artifacts", lambda key: p3_spec if key == "parakeet-v3" else real_artifacts(key))
    monkeypatch.setattr(discovery, "artifacts", lambda key: p3_spec if key == "parakeet-v3" else real_artifacts(key))
    monkeypatch.setattr(
        gguf_meta, "_builtin_gguf_specs",
        lambda: {p3_file.stat().st_size: (("parakeet-v3", p3_spec["files"][0]),)},
    )
    monkeypatch.setattr(custom, "addable_backends", lambda: ["parakeet", "nemotron", "parakeet_unified", "qwen_asr", "moonshine"])

    found = discovery.discover_existing_models(
        locations=[(hf_root, "Hugging Face cache"), (handy_root, "Handy models")],
        verify_builtin_hashes=True,
    )

    by_kind = {(item.kind, item.backend): item for item in found}
    assert ("builtin_speech", "parakeet") in by_kind
    assert by_kind[("builtin_speech", "parakeet")].builtin_key == "parakeet-v3"
    assert ("custom_whisper", "local_whisper") in by_kind
    assert by_kind[("custom_whisper", "local_whisper")].path == "community/whisper-custom-ct2"
    assert ("custom_speech", "qwen_asr") in by_kind
    assert by_kind[("custom_speech", "qwen_asr")].label == "Community Qwen3 ASR"
    # Llama LLM was never suggested.
    assert not any("llama" in item.label.lower() or "llama" in item.path.lower() for item in found)

    # Import all discovered models in place.
    result = discovery.import_discovered_models(found)
    assert len(result.imported) == 3 and not result.failed
    assert cache.is_cached("parakeet-v3") and cache.is_linked("parakeet-v3")
    assert "community/whisper-custom-ct2" in SettingsKey.CUSTOM_WHISPER_MODELS.read(
        settings_manager.load_all_settings()
    )
    assert any(m.label == "Community Qwen3 ASR" and m.backend == "qwen_asr" for m in custom.registered().values())

    # Re-scanning now finds nothing left to import.
    again = discovery.discover_existing_models(
        locations=[(hf_root, "Hugging Face cache"), (handy_root, "Handy models")],
        verify_builtin_hashes=True,
    )
    assert again == []


def test_dismissal_hides_seen_discoveries_without_hiding_new_ones():
    first = discovery.DiscoveredModel(
        id="builtin:parakeet-v3", kind="builtin_speech", backend="parakeet",
        label="Parakeet TDT 0.6B v3", path="/a.gguf", source_label="Hugging Face cache",
        size_bytes=100, builtin_key="parakeet-v3",
    )
    second = discovery.DiscoveredModel(
        id="path:/b.gguf", kind="custom_speech", backend="qwen_asr",
        label="Qwen Custom", path="/b.gguf", source_label="Handy models",
        size_bytes=200,
    )

    assert discovery.unseen_discoveries([first, second]) == [first, second]
    discovery.dismiss_discoveries([first.id])
    assert discovery.unseen_discoveries([first, second]) == [second]


# ---- UI tests: CustomSpeechModelDialog (auto-detect), ImportModelsDialog, DownloadsPage banner ----


def test_custom_speech_dialog_auto_detects_engine_and_capabilities(tmp_path, qapp, monkeypatch):
    from ui_qt.dialogs.custom_speech_dialog import CustomSpeechModelDialog

    monkeypatch.setattr(custom, "addable_backends", lambda: ["parakeet", "nemotron", "parakeet_unified", "qwen_asr"])
    source = _write_gguf(tmp_path / "my-parakeet-110m.gguf", {
        "general.architecture": "parakeet",
        "parakeet.arch": "hybrid_tdt_ctc",
        "general.name": "My Parakeet 110M",
    })

    dialog = CustomSpeechModelDialog()
    assert dialog.backend == "qwen_asr"
    dialog.path_edit.setText(str(source))
    assert dialog.format_combo.currentData().id == "transcribe_gguf"
    assert dialog.backend == "parakeet_unified"
    assert dialog.mode_checkbox.isChecked() is False
    assert dialog.label_edit.text() == "My Parakeet 110M"
    assert not dialog.detected_label.isHidden()
    assert dialog.meeting_checkbox.isChecked()
    assert not dialog.streaming_checkbox.isChecked()

    dialog.multilingual_checkbox.setChecked(True)
    dialog._add()
    assert dialog.added_key
    model = MODELS[dialog.added_key]
    assert model.backend == "parakeet_unified"
    assert model.meeting is True
    assert model.streaming is False
    assert model.multilingual is True


def test_import_models_dialog_allows_partial_selection_and_import(tmp_path, qapp, monkeypatch):
    from ui_qt.dialogs.import_models_dialog import ImportModelsDialog

    monkeypatch.setattr(custom, "addable_backends", lambda: ["parakeet", "qwen_asr"])
    g1 = _write_gguf(tmp_path / "one.gguf", {"general.architecture": "asr", "asr.head_type": "tdt"})
    g2 = _write_gguf(tmp_path / "two.gguf", {"general.architecture": "qwen3_asr"})

    items = [
        discovery.DiscoveredModel(
            id=f"path:{str(g1.resolve()).casefold()}", kind="custom_speech", backend="parakeet",
            label="One Parakeet", path=str(g1.resolve()), source_label="Handy models",
            size_bytes=g1.stat().st_size,
        ),
        discovery.DiscoveredModel(
            id=f"path:{str(g2.resolve()).casefold()}", kind="custom_speech", backend="qwen_asr",
            label="Two Qwen", path=str(g2.resolve()), source_label="Handy models",
            size_bytes=g2.stat().st_size,
        ),
    ]

    dialog = ImportModelsDialog(discovered=items, run_async=False)
    assert len(dialog.selected_items()) == 2
    assert dialog.import_button.text() == "Import all (2)"

    # Uncheck the second item to import only a subset.
    dialog._rows[1].set_checked(False)
    assert len(dialog.selected_items()) == 1
    assert dialog.import_button.text() == "Import 1 model"

    dialog._on_import_clicked()
    assert dialog.import_result is not None
    assert len(dialog.import_result.imported) == 1
    assert dialog.import_result.imported[0].label == "One Parakeet"
    labels = {m.label for m in custom.registered().values()}
    assert labels == {"One Parakeet"}


def test_downloads_page_shows_first_time_import_banner_and_respects_dismiss(tmp_path, qapp, monkeypatch):
    from ui_qt.dialogs import settings_downloads as downloads_module

    values = {
        SettingsKey.WHISPER_MODEL: "base",
        SettingsKey.SELECTED_MODEL: "local_whisper",
    }
    mem = InMemorySettings(values)
    item = discovery.DiscoveredModel(
        id="builtin:parakeet-v3", kind="builtin_speech", backend="parakeet",
        label="Parakeet TDT 0.6B v3", path=str(tmp_path / "p3.gguf"),
        source_label="Hugging Face cache", size_bytes=714_000_000,
        builtin_key="parakeet-v3",
    )

    monkeypatch.setattr(downloads_module, "settings_manager", mem)
    monkeypatch.setattr(discovery, "settings_manager", mem)
    monkeypatch.setattr(downloads_module, "scan_cached_models", lambda **_: {})
    monkeypatch.setattr("services.local_asr.cache.inventory", lambda: {})
    monkeypatch.setattr(downloads_module, "discover_existing_models", lambda: [item])

    page = downloads_module.DownloadsPage(background_cache_scan=False, auto_discover=True)
    page.refresh()

    assert not page.import_banner.isHidden()
    assert "Found 1 existing speech model" in page.import_banner_title.text()
    assert "Hugging Face cache" in page.import_banner_note.text()

    # Dismissing hides the banner and persists so a fresh DownloadsPage stays quiet.
    page._dismiss_import_banner()
    assert page.import_banner.isHidden()

    page2 = downloads_module.DownloadsPage(background_cache_scan=False, auto_discover=True)
    page2.refresh()
    assert page2.import_banner.isHidden()


def test_downloads_row_shows_linked_badge_and_unlink_button(tmp_path, qapp, monkeypatch):
    from services.hf_access import CachedModelInfo
    from ui_qt.dialogs import settings_downloads as downloads_module

    repo = "nvidia/parakeet-tdt-0.6b-v3"
    linked_info = CachedModelInfo(
        repo_id=repo, size_bytes=713_975_456, path=str(tmp_path / "p3.gguf"),
        revision_hashes=("541d1f99c6b0c3cd0b11a95167540bb8edefd82b",),
        bundled=False, linked=True,
    )
    monkeypatch.setattr(downloads_module, "scan_cached_models", lambda **_: {})
    monkeypatch.setattr("services.local_asr.cache.inventory", lambda: {repo: linked_info})
    page = downloads_module.DownloadsPage(background_cache_scan=False)
    page.refresh()

    row = page.rows["parakeet-v3"]
    assert row.is_cached
    assert row.badge.text() == "Linked"
    assert row.delete_button.text() == "Unlink"
    assert "keeps the original file" in row.delete_button.toolTip()


def test_batch_download_dialog_shows_speech_models_destination(qapp):
    from ui_qt.dialogs.settings_downloads import BatchDownloadDialog

    speech_dialog = BatchDownloadDialog(["parakeet-v3"])
    assert "speech-models" in speech_dialog.destination_path_label.text()

    whisper_dialog = BatchDownloadDialog(["tiny"])
    assert "speech-models" not in whisper_dialog.destination_path_label.text()
