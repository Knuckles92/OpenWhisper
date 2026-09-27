"""OpenAI's 2027-02-26 shutdown of whisper-1, gpt-4o(-mini)-transcribe and
gpt-4o-transcribe-diarize: date gating, saved-setting resolution, and the
fallbacks for a model that disappears before the local clock says so."""
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

from services import openai_retirement
from services.settings import (
    MeetingSpeakerIdBackend,
    SettingsKey,
    api_model_choices,
    api_model_label,
    resolve_api_transcription_model,
    resolve_meeting_speaker_id_backend,
)

BEFORE = date(2027, 2, 25)
AFTER = openai_retirement.SHUTDOWN_DATE
RETIRING = ("gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1")


class _Response:
    """What ``openai.APIStatusError`` reads from an HTTP response."""

    def __init__(self, status_code):
        self.status_code = status_code
        self.request = None
        self.headers = {}


def _status_error(cls, status_code, code):
    return cls(
        "error", response=_Response(status_code),
        body={"message": "error", "code": code},
    )


@pytest.fixture
def after_shutdown(monkeypatch):
    monkeypatch.setattr(openai_retirement, "_today", lambda: AFTER)


class TestShutdownDate:
    def test_boundary_is_the_shutdown_day(self):
        assert openai_retirement.retired(BEFORE) is False
        assert openai_retirement.retired(AFTER) is True

    def test_default_reads_the_pinned_clock(self, after_shutdown):
        assert openai_retirement.retired() is True


class TestApiModelChoices:
    def test_retiring_models_stay_listed_with_a_label_until_shutdown(self):
        assert api_model_choices(BEFORE) == (
            "gpt-transcribe", "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1",
        )
        assert api_model_label("whisper-1", BEFORE) == "whisper-1 (retiring Feb 26, 2027)"
        assert api_model_label("gpt-transcribe", BEFORE) == "gpt-transcribe"

    def test_only_gpt_transcribe_remains_after_shutdown(self):
        assert api_model_choices(AFTER) == ("gpt-transcribe",)

    @pytest.mark.parametrize("model", RETIRING)
    def test_saved_retiring_model_is_kept_until_shutdown(self, model):
        settings = {SettingsKey.API_TRANSCRIPTION_MODEL: model}
        assert resolve_api_transcription_model(settings, BEFORE) == model
        assert resolve_api_transcription_model(settings, AFTER) == "gpt-transcribe"

    @pytest.mark.parametrize("legacy", ["api_whisper", "api_gpt4o", "api_gpt4o_mini"])
    def test_legacy_selected_model_resolves_to_gpt_transcribe_after_shutdown(self, legacy):
        settings = {SettingsKey.SELECTED_MODEL: legacy}
        assert resolve_api_transcription_model(settings, AFTER) == "gpt-transcribe"


class TestSpeakerIdBackend:
    def test_openai_is_kept_until_shutdown(self):
        settings = {SettingsKey.MEETING_SPEAKER_ID_BACKEND: MeetingSpeakerIdBackend.OPENAI}
        assert resolve_meeting_speaker_id_backend(settings, BEFORE) == MeetingSpeakerIdBackend.OPENAI

    def test_openai_becomes_on_device_after_shutdown(self):
        settings = {SettingsKey.MEETING_SPEAKER_ID_BACKEND: MeetingSpeakerIdBackend.OPENAI}
        assert resolve_meeting_speaker_id_backend(settings, AFTER) == MeetingSpeakerIdBackend.LOCAL

    def test_off_stays_off_after_shutdown(self):
        settings = {SettingsKey.MEETING_SPEAKER_ID_BACKEND: MeetingSpeakerIdBackend.OFF}
        assert resolve_meeting_speaker_id_backend(settings, AFTER) == MeetingSpeakerIdBackend.OFF


class TestModelGoneError:
    def test_not_found_counts(self):
        import openai

        assert openai_retirement.is_model_gone_error(
            _status_error(openai.NotFoundError, 404, None)
        )

    def test_model_not_found_code_counts_on_any_status(self):
        import openai

        assert openai_retirement.is_model_gone_error(
            _status_error(openai.BadRequestError, 400, "model_not_found")
        )

    def test_unsupported_format_does_not_count(self):
        # The shape gpt-transcribe returns for response_format="diarized_json".
        import openai

        assert not openai_retirement.is_model_gone_error(
            _status_error(openai.BadRequestError, 400, "unsupported_value")
        )
        assert not openai_retirement.is_model_gone_error(RuntimeError("down"))


class TestTranscriptionRetry:
    """A retiring model that is already gone gets one retry on gpt-transcribe."""

    @staticmethod
    def _backend(client, model_type):
        from transcriber.openai_backend import OpenAIBackend

        backend = OpenAIBackend.__new__(OpenAIBackend)
        backend.model_type = model_type
        backend.api_key = "sk-test"
        backend.is_transcribing = False
        backend.should_cancel = False
        backend.client = client
        return backend

    @staticmethod
    def _client(responder):
        import httpx
        from openai import OpenAI

        return OpenAI(
            api_key="sk-test",
            max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(responder)),
        )

    @pytest.fixture
    def audio(self, tmp_path):
        path = tmp_path / "clip.wav"
        path.write_bytes(b"RIFF" + b"\x00" * 32)
        return str(path)

    def test_gone_retiring_model_retries_with_gpt_transcribe(self, audio):
        import httpx

        models = []

        def respond(request):
            body = request.read().decode("utf-8")
            model = "whisper-1" if 'name="model"\r\n\r\nwhisper-1\r\n' in body else "other"
            models.append(model)
            if model == "whisper-1":
                return httpx.Response(404, json={"error": {
                    "message": "The model `whisper-1` does not exist",
                    "type": "invalid_request_error", "code": "model_not_found",
                }})
            assert 'name="model"\r\n\r\ngpt-transcribe\r\n' in body
            assert 'name="response_format"\r\n\r\njson\r\n' in body
            return httpx.Response(200, json={"text": " hello ", "languages": []})

        with self._client(respond) as client:
            assert self._backend(client, "whisper-1").transcribe(audio) == "hello"
        assert models == ["whisper-1", "other"]

    def test_other_errors_are_not_retried(self, audio):
        import httpx
        import openai

        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(400, json={"error": {
                "message": "bad audio", "type": "invalid_request_error",
                "code": "invalid_value",
            }})

        with self._client(respond) as client:
            with pytest.raises(openai.BadRequestError):
                self._backend(client, "whisper-1").transcribe(audio)
        assert len(calls) == 1

    def test_gpt_transcribe_itself_is_not_retried(self, audio):
        import httpx
        import openai

        calls = []

        def respond(request):
            calls.append(request)
            return httpx.Response(404, json={"error": {
                "message": "gone", "type": "invalid_request_error",
                "code": "model_not_found",
            }})

        with self._client(respond) as client:
            with pytest.raises(openai.NotFoundError):
                self._backend(client, "gpt-transcribe").transcribe(audio)
        assert len(calls) == 1

    def test_explicit_retiring_model_type_maps_after_shutdown(self, after_shutdown):
        backend = self._backend(SimpleNamespace(), "whisper-1")
        assert backend._get_api_model_name() == "gpt-transcribe"
        backend.model_type = "api_gpt4o"
        assert backend._get_api_model_name() == "gpt-transcribe"


def test_cloud_speaker_pass_reports_a_gone_model_as_retired(repo, monkeypatch):
    import openai

    from meeting.diarize.cloud_pass import run_cloud_speaker_pass
    from meeting.stored import open_store
    from tests.test_meeting_cloud_speakers import _seed_meeting

    meeting_id = _seed_meeting(repo)
    store = open_store(repo, meeting_id, repo.get_meeting(meeting_id))
    monkeypatch.setattr(
        "meeting.asr.offline.load_channel_session",
        lambda *args: (np.zeros(16000 * 4, dtype=np.int16), 16000, 0.0),
    )
    monkeypatch.setattr("meeting.diarize.cloud_pass.encode_mp3", lambda *args: b"audio")

    def decoder(*args, **kwargs):
        raise _status_error(openai.NotFoundError, 404, "model_not_found")

    result = run_cloud_speaker_pass(
        repo, meeting_id, store, "/tmp/spool",
        api_key="sk-test", transcribe_fn=decoder,
    )
    assert result["ok"] is False
    assert result["retired"] is True
    assert result["error"] == openai_retirement.SPEAKER_MODEL_RETIRED_MESSAGE
    assert repo.get_segment(meeting_id, "sg_lb")["speaker_participant_id"] is None
