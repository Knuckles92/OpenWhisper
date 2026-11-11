"""Meetings use the dictation engine unless given one of their own."""
import json

import pytest

from tests.helpers import patch_setting_defaults

from services import settings as module
from services.settings import (
    SettingsKey,
    SettingsManager,
    keep_separate_meeting_speech,
    own_meeting_speech,
    resolve_meeting_asr_source,
    resolve_meeting_speech,
    resolve_meeting_whisper_model,
)


@pytest.fixture
def whisper_fallback(monkeypatch):
    """A platform whose default meeting model is Whisper auto, whatever runs this."""
    patch_setting_defaults(monkeypatch, {
        SettingsKey.MEETING_ASR_MODEL: "",
        SettingsKey.MEETING_WHISPER_MODEL: "auto",
        SettingsKey.SELECTED_MODEL: "parakeet",
    })


def test_a_meeting_follows_a_catalog_engine_and_its_model(whisper_fallback):
    speech = resolve_meeting_speech({
        SettingsKey.SELECTED_MODEL: "parakeet",
        SettingsKey.LOCAL_ASR_MODELS: {"parakeet": "orukeet-v0.1"},
    })
    assert (speech.source, speech.model, speech.separate, speech.note) == (
        "local", "orukeet-v0.1", False, "")


def test_a_meeting_follows_local_whisper_and_its_model(whisper_fallback):
    settings = {SettingsKey.SELECTED_MODEL: "local_whisper", SettingsKey.WHISPER_MODEL: "small.en"}
    assert resolve_meeting_whisper_model(settings) == "small.en"
    assert resolve_meeting_asr_source(settings) == "local"


def test_a_meeting_follows_dictation_to_the_paired_computer(whisper_fallback):
    speech = resolve_meeting_speech({SettingsKey.SELECTED_MODEL: "remote"})
    assert speech.source == "remote"
    assert not speech.separate


def test_saved_meeting_choices_wait_unused_while_following(whisper_fallback):
    settings = {
        SettingsKey.SELECTED_MODEL: "parakeet",
        SettingsKey.MEETING_ASR_MODEL: "nemotron-3.5",
        SettingsKey.MEETING_ASR_SOURCE: "remote",
    }
    assert resolve_meeting_speech(settings).model == "parakeet-v3"
    assert resolve_meeting_speech(settings).source == "local"
    own = own_meeting_speech(settings)
    assert (own.source, own.model, own.separate) == ("remote", "nemotron-3.5", True)


def test_a_separate_meeting_engine_ignores_dictation(whisper_fallback):
    speech = resolve_meeting_speech({
        SettingsKey.SELECTED_MODEL: "remote",
        SettingsKey.MEETING_SPEECH_SEPARATE: True,
        SettingsKey.MEETING_ASR_MODEL: "moonshine-small",
    })
    assert (speech.source, speech.model, speech.separate) == ("local", "moonshine-small", True)


@pytest.mark.parametrize("settings,named", [
    ({SettingsKey.SELECTED_MODEL: "api"}, "The API engine"),
    ({SettingsKey.SELECTED_MODEL: "parakeet_unified"}, "Parakeet Unified EN 0.6B"),
    ({SettingsKey.SELECTED_MODEL: "qwen_asr",
      SettingsKey.LOCAL_ASR_MODELS: {"qwen_asr": "qwen-1.7b"}}, "Qwen3-ASR 1.7B"),
])
def test_an_engine_that_cannot_run_meetings_falls_back_and_says_so(
    whisper_fallback, settings, named
):
    speech = resolve_meeting_speech(settings)
    assert (speech.source, speech.model, speech.separate) == ("local", "auto", False)
    assert speech.note == f"{named} can't transcribe meetings, so meetings use Local Whisper."


def test_a_legacy_api_selection_counts_as_the_api_engine(whisper_fallback):
    legacy = next(iter(module.LEGACY_API_MODELS))
    assert resolve_meeting_speech({SettingsKey.SELECTED_MODEL: legacy}).note.startswith("The API engine")


def _manager(tmp_path, saved):
    path = tmp_path / "openwhisper_settings.json"
    if saved is not None:
        path.write_text(json.dumps(saved), encoding="utf-8")
    return SettingsManager(str(path))


def test_a_new_install_follows_dictation(tmp_path, whisper_fallback):
    manager = _manager(tmp_path, None)
    assert keep_separate_meeting_speech(manager)
    assert manager.get(SettingsKey.MEETING_SPEECH_SEPARATE) is False
    assert not keep_separate_meeting_speech(manager)


@pytest.mark.parametrize("saved", [
    # Dictation on the paired computer, meetings on a local model.
    {SettingsKey.SELECTED_MODEL: "remote", SettingsKey.MEETING_ASR_MODEL: "parakeet-v3"},
    # Meetings on the paired computer, dictation here.
    {SettingsKey.SELECTED_MODEL: "parakeet", SettingsKey.MEETING_ASR_SOURCE: "remote"},
    # Two different local models.
    {SettingsKey.SELECTED_MODEL: "local_whisper", SettingsKey.WHISPER_MODEL: "small",
     SettingsKey.MEETING_WHISPER_MODEL: "turbo"},
])
def test_an_install_with_a_different_meeting_engine_keeps_it(tmp_path, whisper_fallback, saved):
    manager = _manager(tmp_path, saved)
    before = own_meeting_speech(manager.load_all_settings())

    assert keep_separate_meeting_speech(manager)

    after = resolve_meeting_speech(manager.load_all_settings())
    assert after.separate
    assert (after.source, after.model) == (before.source, before.model)


@pytest.mark.parametrize("saved", [
    {},
    {SettingsKey.SELECTED_MODEL: "parakeet"},
    {SettingsKey.SELECTED_MODEL: "parakeet", SettingsKey.MEETING_ASR_MODEL: "parakeet-v3"},
    {SettingsKey.SELECTED_MODEL: "local_whisper", SettingsKey.WHISPER_MODEL: "auto",
     SettingsKey.MEETING_WHISPER_MODEL: "auto"},
    {SettingsKey.SELECTED_MODEL: "remote", SettingsKey.MEETING_ASR_SOURCE: "remote"},
])
def test_an_install_whose_meetings_matched_dictation_follows_it(tmp_path, whisper_fallback, saved):
    manager = _manager(tmp_path, saved)
    assert keep_separate_meeting_speech(manager)
    assert manager.get(SettingsKey.MEETING_SPEECH_SEPARATE) is False


def test_the_choice_is_made_once(tmp_path, whisper_fallback):
    manager = _manager(tmp_path, {SettingsKey.SELECTED_MODEL: "parakeet"})
    assert keep_separate_meeting_speech(manager)
    # A later change of dictation engine must not make the meeting separate.
    manager.save_setting(SettingsKey.SELECTED_MODEL, "nemotron")
    stamp = (tmp_path / "openwhisper_settings.json").stat().st_mtime_ns

    assert not keep_separate_meeting_speech(manager)
    assert (tmp_path / "openwhisper_settings.json").stat().st_mtime_ns == stamp
    speech = resolve_meeting_speech(manager.load_all_settings())
    assert (speech.model, speech.separate) == ("nemotron-3.5", False)


def test_keeping_the_meeting_engine_tolerates_stand_in_managers():
    assert not keep_separate_meeting_speech(object())
