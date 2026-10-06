"""History and Stats record the language the final pass decoded with."""

from types import SimpleNamespace

import pytest

from services import dictation_language, dictation_pipeline, focus_context
from services.focus_context import FocusSnapshot
from services.settings import SettingsKey
from tests.test_personalize_foundation_runtime import OUTLOOK, FakeService, h  # noqa: F401


@pytest.fixture
def dictating(h, monkeypatch):  # noqa: F811 (pytest fixture)
    h.settings.values.update({
        SettingsKey.SELECTED_MODEL: "local_whisper",
        SettingsKey.DICTATION_LANGUAGES: ["en", "de"],
        SettingsKey.DICTATION_ACTIVE_LANGUAGE: "en",
    })
    focus_context.set_service(FakeService(FocusSnapshot(OUTLOOK)))
    h.decoded_with = []

    def transcribe(backend, path, recognition=None):
        h.decoded_with.append(recognition.language if recognition else None)
        return "Hallo zusammen"

    h.runtime._incremental.transcribe.side_effect = transcribe
    h.controller.current_backend = SimpleNamespace(
        transcribe=lambda path: "Hallo zusammen", is_available=lambda: True,
        supports_recognition=True,
    )
    h.stats = []
    monkeypatch.setattr(dictation_pipeline, "record_stats",
                        lambda fields, row: h.stats.append(dict(fields)))
    return h


def _stop_and_transcribe(harness):
    harness.runtime.stop_recording()
    harness.submitted.clear()
    harness.controller._pending_audio_path = harness.audio
    harness.runtime.transcribe_audio_file(harness.audio)


def test_a_language_switched_during_the_recording_is_the_one_recorded(dictating):
    harness = dictating
    assert harness.runtime.start_recording()
    harness.settings.mutate_settings(dictation_language.cycle)

    _stop_and_transcribe(harness)

    assert harness.decoded_with == ["de"]
    assert harness.history.entries[0]["language"] == "de"
    assert harness.stats[0]["language"] == "de"


def test_the_starting_language_is_recorded_without_a_switch(dictating):
    harness = dictating
    assert harness.runtime.start_recording()

    _stop_and_transcribe(harness)

    assert harness.decoded_with == ["en"]
    assert harness.history.entries[0]["language"] == "en"


def test_a_slot_released_meanwhile_is_never_given_its_job_back(dictating, monkeypatch):
    harness = dictating
    assert harness.runtime.start_recording()
    harness.settings.mutate_settings(dictation_language.cycle)
    final_pass_recognition = harness.runtime._final_pass_recognition

    def released_meanwhile(job):
        harness.runtime._finish_job()
        return final_pass_recognition(job)

    monkeypatch.setattr(harness.runtime, "_final_pass_recognition", released_meanwhile)
    seen = []
    decode = harness.runtime._incremental.transcribe.side_effect
    harness.runtime._incremental.transcribe.side_effect = (
        lambda *args, **kwargs: seen.append(harness.runtime._active_job) or decode(*args, **kwargs))

    _stop_and_transcribe(harness)

    assert seen == [None]
