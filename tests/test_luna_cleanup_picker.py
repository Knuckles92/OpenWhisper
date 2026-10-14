"""OpenAI's draft model is shared by both desktop appearances."""
import pytest

from ui_qt.widgets.text_model_picker import TextModelPicker


@pytest.mark.parametrize("appearance", ["classic", "omarchy"])
def test_openai_picker_uses_luna_and_retains_explicit_model(monkeypatch, appearance):
    monkeypatch.setenv("OPENWHISPER_UI", appearance)
    picker = TextModelPicker()
    picker.set_provider("openai")
    assert picker.model_combo.currentText() == "gpt-6-luna"
    picker.set_provider("openrouter")
    assert picker.model_combo.currentText() == "openrouter/free"
    picker.set_provider("openai", "gpt-4.1-mini")
    picker.set_provider("openrouter")
    picker.set_provider("openai")
    assert picker.model_combo.currentText() == "gpt-4.1-mini"


def test_saved_legacy_cleanup_selection_shows_luna_without_changing_provider():
    from services.settings import (
        SettingsKey, resolve_transcript_cleanup_model, resolve_transcript_cleanup_provider,
    )
    settings = {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openai",
                SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "gpt-4o-mini"}
    picker = TextModelPicker()
    provider = resolve_transcript_cleanup_provider(settings)
    model = resolve_transcript_cleanup_model(settings)
    picker.set_provider(provider, model)
    picker.set_active_selection(provider, model)
    assert picker.provider == "openai"
    assert picker.model_combo.currentText() == "gpt-6-luna"
    assert settings[SettingsKey.TRANSCRIPT_CLEANUP_MODEL] == "gpt-4o-mini"
