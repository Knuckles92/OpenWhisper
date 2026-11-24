"""Tests for speech model/runtime separation, unified engine card, family rail, and custom dialog."""
from pathlib import Path
from unittest.mock import patch


from services import dictation_language
from services.local_asr import catalog, custom, languages
from services.settings import SettingsKey, settings_manager


def _gguf(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"GGUF\x03\x00\x00\x00data")
    return path


class TestPerModelCapabilitiesAndPlatforms:
    def test_parakeet_variants_share_one_family_and_filter_by_os(self):
        assert catalog.family_of("parakeet_unified") == "parakeet"
        assert catalog.family_of("parakeet") == "parakeet"
        assert catalog.family_of("parakeet_mlx") == "parakeet"
        assert catalog.family_of("local_whisper") == "whisper"
        assert catalog.family_of("qwen_asr") == "qwen"

        assert catalog.model_supported_here("parakeet-unified-en", "win_amd64")
        assert not catalog.model_supported_here("parakeet-v3-mlx", "win_amd64")
        assert catalog.model_supported_here("parakeet-v3-mlx", "darwin_arm64")
        assert not catalog.model_supported_here("parakeet-unified-en", "darwin_arm64")
        assert "moonshine" not in catalog.supported_families("linux_x86_64")
        assert "moonshine" in catalog.supported_families("win_amd64")

    def test_per_model_vad_gate_and_languages_work_independently_of_backend(self, tmp_path):
        from transcriber.optional_backend import LocalSpeechBackend

        # Custom transcribe.cpp GGUF with speech-gated VAD turned off vs on.
        key_gated = custom.register("qwen_asr", _gguf(tmp_path / "fourier.gguf"), "Fourier 2B", vad_gate=True)
        key_ungated = custom.register("qwen_asr", _gguf(tmp_path / "plain.gguf"), "Plain", vad_gate=False)
        assert LocalSpeechBackend("qwen_asr", key_gated)._uses_vad_gate() is True
        assert LocalSpeechBackend("qwen_asr", key_ungated)._uses_vad_gate() is False

        # Per-model English-only language contract inside a multilingual backend.
        english_qwen = catalog.SpeechModel(
            "fourier-test", "qwen_asr", "Fourier 2B", "English dictation",
            "CC-BY-NC-4.0", "English", family="qwen",
            platforms=("win_amd64", "linux_x86_64"),
            language_codes=("en",), vad_gate=True,
        )
        with patch.dict(catalog.BUILTIN_MODELS, {"fourier-test": english_qwen}):
            assert languages.model_language_choices("fourier-test") == ("en",)
            settings = {
                SettingsKey.SELECTED_MODEL: "qwen_asr",
                SettingsKey.LOCAL_ASR_MODELS: {"qwen_asr": "fourier-test"},
            }
            assert dictation_language.accepted_languages(settings) == ("en",)
            assert dictation_language.is_english_only_engine(settings) is True
            assert "Fourier 2B" in dictation_language.single_language_reason(settings)


class TestUnifiedMainEngineCard:
    def test_source_bar_and_unified_local_picker_switch_cleanly(self, qapp):
        from ui_qt.widgets.quick_record_tab import QuickRecordTab

        settings_manager.update_settings({
            SettingsKey.SELECTED_MODEL: "parakeet_unified",
            SettingsKey.LOCAL_ASR_MODELS: {"parakeet_unified": "parakeet-unified-en", "qwen_asr": "qwen-0.6b"},
        })
        tab = QuickRecordTab()
        try:
            tab.sync_engine_selection()
            announced = []
            tab.model_changed.connect(announced.append)

            # Starts on Local source with transcribe.cpp runtime badge.
            assert tab.source_bar.currentIndex() == 0
            assert tab.local_engine.model_combo.currentData() == "parakeet-unified-en"
            assert tab.runtime_badge.text() == "transcribe.cpp"

            # Switching to a model from another backend (Qwen3-ASR) inside the unified picker
            # emits model_changed for that backend and updates the saved model.
            qwen_idx = tab.local_engine.model_combo.findData("qwen-1.7b")
            assert qwen_idx >= 0
            tab.local_engine.model_combo.setCurrentIndex(qwen_idx)
            assert announced[-1] == "Qwen3-ASR"
            assert tab.current_backend() == "Qwen3-ASR"
            assert SettingsKey.LOCAL_ASR_MODELS.read(settings_manager.load_all_settings())["qwen_asr"] == "qwen-1.7b"

            # Switching to a Whisper model inside the same unified picker works too.
            small_idx = tab.local_engine.model_combo.findData("small")
            assert small_idx >= 0
            tab.local_engine.model_combo.setCurrentIndex(small_idx)
            assert announced[-1] == "Local Whisper"
            assert tab.runtime_badge.text() == "faster-whisper"
            assert not tab.local_engine.compute_combo.parentWidget().isHidden()

            # Source bar switches to Cloud API, then back to Local restoring last local backend.
            tab.source_bar.buttons[1].click()
            assert announced[-1] == "API"
            assert tab.local_engine.isHidden()
            assert not tab.api_server.isHidden()
            assert tab.runtime_badge.isHidden()

            tab.source_bar.buttons[0].click()
            assert announced[-1] == "Local Whisper"
            assert not tab.local_engine.isHidden()
        finally:
            tab.close()


class TestRuntimeFormatCustomSpeechDialog:
    def test_transcribe_gguf_format_toggles_vad_gate_and_backend(self, tmp_path, qapp):
        from ui_qt.dialogs.custom_speech_dialog import CustomSpeechModelDialog

        with patch("services.components.current_platform_tag", return_value="win_amd64"):
            dialog = CustomSpeechModelDialog()
            assert dialog.format_combo.isVisibleTo(dialog) or dialog.format_combo.count() >= 2
            assert dialog.format_combo.currentData().id == "transcribe_gguf"
            assert dialog.mode_checkbox.isChecked() is True
            assert dialog.backend == "qwen_asr"

            fourier = _gguf(tmp_path / "fourier-2b-q8.gguf")
            dialog.path_edit.setText(str(fourier))
            dialog._add()
            assert dialog.added_key
            model = catalog.MODELS[dialog.added_key]
            assert model.backend == "qwen_asr" and model.vad_gate is True

            # Unchecking Silero VAD registers under parakeet_unified with vad_gate=False.
            dialog2 = CustomSpeechModelDialog()
            dialog2.mode_checkbox.setChecked(False)
            assert dialog2.backend == "parakeet_unified"
            parakeet_file = _gguf(tmp_path / "custom-parakeet.gguf")
            dialog2.path_edit.setText(str(parakeet_file))
            dialog2._add()
            model2 = catalog.MODELS[dialog2.added_key]
            assert model2.backend == "parakeet_unified" and model2.vad_gate is False
