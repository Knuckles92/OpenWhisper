"""Early windows decode with the dictation's RecognitionContext, or not at all."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from config import config
from services.incremental_dictation import DictationSession, IncrementalDictation
from services.local_asr import nvidia
from services.recognition_context import RecognitionContext
from services.recorder import AudioRecorder
from services.recording_journal import RecordingJournal
from tests.test_incremental_dictation import RATE, Engine, capture, speech_like, stop
from transcriber.optional_backend import LocalSpeechBackend

FRENCH = RecognitionContext(language="fr", phrases=("Ksenia",))


class RecordingEngine(Engine):
    """Engine that also notes the language and phrases of every window."""

    def __init__(self, backend):
        super().__init__(backend)
        self.inputs = []

    def __call__(self, audio, language=None, phrases=()):
        self.inputs.append((language, tuple(phrases)))
        return super().__call__(audio, language)


@pytest.fixture(params=["parakeet", "nemotron"])
def backend(request, monkeypatch):
    if request.param == "nemotron":
        monkeypatch.setattr(nvidia, "WORD_BOOSTING", True)
        monkeypatch.setattr(config, "INCREMENTAL_DICTATION_BACKENDS",
                            tuple(config.INCREMENTAL_DICTATION_BACKENDS) + ("nemotron",))
    engine_backend = LocalSpeechBackend(request.param)
    engine_backend.is_available = lambda: True
    engine_backend._recognize = RecordingEngine(engine_backend)
    return engine_backend


@pytest.fixture
def recorder():
    capture_ = AudioRecorder(output_file=config.RECORDED_AUDIO_FILE)
    capture_._audio_spool = RecordingJournal(
        capture_.output_file, capture_.rate, capture_.channels, 2, capture_._fail_capture
    )
    yield capture_
    capture_.cleanup()


@pytest.fixture
def controller(backend, recorder):
    return SimpleNamespace(current_backend=backend, recorder=recorder, is_meeting_active=lambda: False)


def expected_phrases(backend, recognition):
    return tuple(recognition.phrases) if backend.backend_id == "nemotron" else ()


def record(controller, recorder, backend, recognition, seconds=64):
    slot = IncrementalDictation()
    session = DictationSession(controller, backend, recorder, recognition=recognition)
    slot._session = session
    capture(recorder, speech_like(seconds, seed=21), session)
    path = stop(recorder, session)
    return slot, session, path


def test_early_windows_decode_with_the_context(controller, recorder, backend):
    slot, session, path = record(controller, recorder, backend, FRENCH)
    assert session.early_windows >= 1
    early = list(backend._recognize.inputs)
    assert set(early) == {("fr", expected_phrases(backend, FRENCH))}
    with patch.object(backend, "transcribe", wraps=backend.transcribe) as transcribe:
        text = slot.transcribe(backend, path, recognition=FRENCH)
    if transcribe.called:
        assert "saved file" in session._invalid  # this platform's resampler cut the file differently
    else:
        assert session._invalid is None
    assert set(backend._recognize.inputs) == {("fr", expected_phrases(backend, FRENCH))}
    backend._recognize.inputs.clear()
    assert text == backend.transcribe(path, recognition=FRENCH)


@pytest.mark.parametrize("final", [
    RecognitionContext(language="de", phrases=("Ksenia",)),
    RecognitionContext(language="fr", phrases=("Ksenia", "Olu")),
    None,
])
def test_a_different_final_context_decodes_the_file(controller, recorder, backend, final):
    slot, session, path = record(controller, recorder, backend, FRENCH)
    assert session.early_windows >= 1
    with patch.object(backend, "transcribe", return_value="file text") as transcribe:
        assert slot.transcribe(backend, path, recognition=final) == "file text"
    assert session._invalid == "the dictation's language or vocabulary changed"
    if final:
        transcribe.assert_called_once_with(path, recognition=final)
    else:
        transcribe.assert_called_once_with(path)


def test_an_empty_context_matches_none(controller, recorder, backend):
    slot, session, path = record(controller, recorder, backend, RecognitionContext())
    with patch.object(backend, "transcribe", wraps=backend.transcribe) as transcribe:
        slot.transcribe(backend, path)
    if not transcribe.called:
        assert session._invalid is None
    assert all(phrases == () for _language, phrases in backend._recognize.inputs)


def test_a_settings_language_change_still_invalidates(controller, recorder, backend):
    from services.settings import settings_manager

    session = DictationSession(controller, backend, recorder, recognition=RecognitionContext(phrases=("Ksenia",)))
    pcm = speech_like(66, seed=11)
    capture(recorder, pcm[:35 * RATE], session)
    assert session.early_windows == 1
    settings_manager.update_settings({"local_asr_language": "fr"})
    capture(recorder, pcm[35 * RATE:], session)
    assert session._invalid == "the language or vocabulary changed"
    assert session.finish(stop(recorder, session)) is None


def test_runtime_starts_the_session_with_the_jobs_context(recorder, backend):
    slot = IncrementalDictation()
    controller = SimpleNamespace(current_backend=backend, recorder=recorder, is_meeting_active=lambda: False)
    with patch.object(DictationSession, "start"):
        slot.start(controller, recognition=FRENCH)
    assert slot._session.recognition is FRENCH
    slot.discard()
