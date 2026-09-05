"""Tests for API model selection, request formats, and chunked transcription."""
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from transcriber.openai_backend import CHUNK_UPLOAD_CONCURRENCY, OpenAIBackend


class FakeTranscriptions:
    """Stands in for ``client.audio.transcriptions``."""

    def __init__(self, delay=0.0, fail_on=None):
        self.delay = delay
        self.fail_on = fail_on
        self.calls = []
        self.in_flight = 0
        self.peak_in_flight = 0
        self._lock = threading.Lock()

    def create(self, model=None, file=None, response_format=None):
        name = getattr(file, "name", "")
        with self._lock:
            self.calls.append(name)
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail_on and self.fail_on in name:
                raise RuntimeError("upload rejected")
            return f"  text for {Path(name).name}  "
        finally:
            with self._lock:
                self.in_flight -= 1


def make_backend(transcriptions):
    backend = OpenAIBackend.__new__(OpenAIBackend)
    backend.model_type = "api_whisper"
    backend.api_key = "sk-test"
    backend.is_transcribing = False
    backend.should_cancel = False
    backend.client = SimpleNamespace(audio=SimpleNamespace(transcriptions=transcriptions))
    return backend


@pytest.fixture
def chunks(tmp_path):
    paths = []
    for index in range(6):
        path = tmp_path / f"part{index}.wav"
        path.write_bytes(b"RIFF" + bytes([index]) * 32)
        paths.append(str(path))
    return paths


class TestChunkedTranscription:
    def test_transcripts_keep_chunk_order(self, chunks):
        """Out-of-order completion must not reorder the transcript.

        The first chunk is made the slowest so it finishes last; the combined
        text still has to read in submission order.
        """
        class Staggered(FakeTranscriptions):
            def create(self, model=None, file=None, response_format=None):
                if file.name.endswith("part0.wav"):
                    time.sleep(0.15)
                return super().create(
                    model=model, file=file, response_format=response_format
                )

        transcriptions = Staggered()
        backend = make_backend(transcriptions)

        combined = backend.transcribe_chunks(chunks)

        assert combined == " ".join(
            f"text for part{i}.wav" for i in range(6)
        )

    def test_uploads_actually_overlap(self, chunks):
        transcriptions = FakeTranscriptions(delay=0.05)
        backend = make_backend(transcriptions)

        backend.transcribe_chunks(chunks)

        assert transcriptions.peak_in_flight > 1, "uploads ran serially"
        assert transcriptions.peak_in_flight <= CHUNK_UPLOAD_CONCURRENCY

    def test_concurrency_is_capped(self, chunks):
        """No retry path here, so a burst of uploads risks a 429."""
        transcriptions = FakeTranscriptions(delay=0.05)
        backend = make_backend(transcriptions)

        backend.transcribe_chunks(chunks * 4)

        assert transcriptions.peak_in_flight <= CHUNK_UPLOAD_CONCURRENCY

    def test_a_single_chunk_skips_the_pool(self, chunks):
        transcriptions = FakeTranscriptions()
        backend = make_backend(transcriptions)

        combined = backend.transcribe_chunks(chunks[:1])

        assert combined == "text for part0.wav"
        assert transcriptions.peak_in_flight == 1

    def test_cancel_before_start_stops_the_run(self, chunks):
        transcriptions = FakeTranscriptions()
        backend = make_backend(transcriptions)
        backend.should_cancel = True
        # reset_cancel_flag() clears it, so cancel mid-flight instead.
        backend.reset_cancel_flag = lambda: None

        with pytest.raises(Exception, match="canceled"):
            backend.transcribe_chunks(chunks)

        assert transcriptions.calls == []

    def test_cancel_mid_flight_stops_later_chunks(self, chunks):
        """Cancel latency is one in-flight upload, not the whole file."""
        transcriptions = FakeTranscriptions(delay=0.02)
        backend = make_backend(transcriptions)
        original = backend._transcribe_one_chunk
        seen = []

        def cancel_after_two(chunk_file, api_model):
            result = original(chunk_file, api_model)
            seen.append(chunk_file)
            if len(seen) >= 2:
                backend.should_cancel = True
            return result

        backend._transcribe_one_chunk = cancel_after_two

        with pytest.raises(Exception, match="canceled"):
            backend.transcribe_chunks(chunks)

        assert len(transcriptions.calls) < len(chunks)

    def test_a_failed_upload_propagates(self, chunks):
        transcriptions = FakeTranscriptions(fail_on="part3.wav")
        backend = make_backend(transcriptions)

        with pytest.raises(RuntimeError, match="upload rejected"):
            backend.transcribe_chunks(chunks)

    def test_is_transcribing_is_cleared_on_failure(self, chunks):
        transcriptions = FakeTranscriptions(fail_on="part0.wav")
        backend = make_backend(transcriptions)

        with pytest.raises(RuntimeError):
            backend.transcribe_chunks(chunks)

        assert backend.is_transcribing is False

    def test_unavailable_backend_refuses(self, chunks):
        backend = make_backend(FakeTranscriptions())
        backend.client = None

        with pytest.raises(Exception, match="not available"):
            backend.transcribe_chunks(chunks)


@pytest.mark.parametrize("model", [
    "gpt-transcribe", "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1",
])
@pytest.mark.parametrize("chunked", [False, True])
def test_selected_api_model_and_response_format(monkeypatch, chunks, model, chunked):
    from unittest.mock import Mock

    from services.settings import SettingsKey
    from transcriber import openai_backend as module

    monkeypatch.setattr(module.settings_manager, "load_all_settings", lambda: {
        SettingsKey.API_TRANSCRIPTION_MODEL: model,
    })
    api = Mock()
    api.create.return_value = SimpleNamespace(text="  transcript  ") if model == "gpt-transcribe" else "  transcript  "
    backend = make_backend(api)
    backend.model_type = "api"
    result = backend.transcribe_chunks(chunks[:2]) if chunked else backend.transcribe(chunks[0])
    assert result == ("transcript transcript" if chunked else "transcript")
    assert api.create.call_count == (2 if chunked else 1)
    for call in api.create.call_args_list:
        assert call.kwargs["model"] == model
        assert call.kwargs["response_format"] == ("json" if model == "gpt-transcribe" else "text")


def test_chunks_keep_model_when_setting_changes_mid_upload(monkeypatch, chunks):
    from unittest.mock import Mock

    from services.settings import SettingsKey
    from transcriber import openai_backend as module

    settings = {SettingsKey.API_TRANSCRIPTION_MODEL: "gpt-transcribe"}
    monkeypatch.setattr(module.settings_manager, "load_all_settings", lambda: dict(settings))
    def respond(**kwargs):
        settings[SettingsKey.API_TRANSCRIPTION_MODEL] = "whisper-1"
        return SimpleNamespace(text="transcript")
    api = Mock()
    api.create.side_effect = respond
    backend = make_backend(api)
    backend.model_type = "api"
    backend.transcribe_chunks(chunks)
    assert all(call.kwargs["model"] == "gpt-transcribe" for call in api.create.call_args_list)


@pytest.mark.parametrize("model", ["gpt-transcribe", "whisper-1"])
def test_sdk_sends_model_and_parses_response_without_network(chunks, model):
    import httpx
    from openai import OpenAI

    requests = []
    def respond(request):
        requests.append(request.read().decode("utf-8"))
        if model == "gpt-transcribe":
            return httpx.Response(200, json={"text": "  hello  ", "languages": [{"code": "en"}]})
        return httpx.Response(200, text="  hello  ")

    with OpenAI(api_key="sk-test", http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        backend = make_backend(None)
        backend.client = client
        backend.model_type = model
        assert backend.transcribe(chunks[0]) == "hello"
    assert len(requests) == 1
    assert f'name="model"\r\n\r\n{model}\r\n' in requests[0]
    response_format = "json" if model == "gpt-transcribe" else "text"
    assert f'name="response_format"\r\n\r\n{response_format}\r\n' in requests[0]
