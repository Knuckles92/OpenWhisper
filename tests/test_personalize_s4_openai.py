"""OpenAI transcription hints: per-model fields, retries, chunks and the isolated worker."""
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from config import config
from services import isolated, isolated_worker
from services.recognition_context import RecognitionContext
from tests.test_openai_backend import LARGE_FILE, chunks, make_backend, split  # noqa: F401  (fixtures)
from transcriber.openai_backend import transcription_hints

CONTEXT = RecognitionContext(language="de-DE", phrases=("Ksenia", "Kubernetes"))


class Rejected(RuntimeError):
    def __init__(self, status, code=None):
        super().__init__(f"HTTP {status}")
        self.status_code = status
        self.code = code


class Transcriptions:
    """Records every create() call; ``fail`` decides which raise."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self._lock = threading.Lock()

    def create(self, **kwargs):
        with self._lock:
            self.calls.append(kwargs)
        error = self.fail(kwargs) if self.fail else None
        if error is not None:
            raise error
        return f"text for {Path(kwargs['file'].name).name}"


def hints_of(call):
    return {key: value for key, value in call.items() if key not in ("model", "file", "response_format")}


class TestHintFields:
    def test_gpt_transcribe_takes_keywords_and_languages(self):
        assert transcription_hints("gpt-transcribe", CONTEXT) == {
            "keywords": ["Ksenia", "Kubernetes"], "languages": ["de"],
        }

    @pytest.mark.parametrize("model", ["whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"])
    def test_older_models_take_a_prompt_and_language(self, model):
        assert transcription_hints(model, CONTEXT) == {"prompt": "Ksenia, Kubernetes.", "language": "de"}

    @pytest.mark.parametrize("recognition", [None, RecognitionContext(), RecognitionContext(language="auto")])
    def test_nothing_without_hints(self, recognition):
        assert transcription_hints("gpt-transcribe", recognition) == {}
        assert transcription_hints("whisper-1", recognition) == {}

    def test_keywords_are_capped(self):
        context = RecognitionContext(phrases=tuple(f"w{i}" for i in range(80)) + ("x" * 70,))
        assert len(transcription_hints("gpt-transcribe", context)["keywords"]) == 50


class TestRequests:
    def backend(self, transcriptions, model):
        backend = make_backend(transcriptions)
        backend.model_type = model
        backend.large_file_size_mb = lambda _path: None
        return backend

    @pytest.mark.parametrize("model", ["gpt-transcribe", "whisper-1"])
    def test_hints_go_with_the_request(self, model, tmp_path):
        path = tmp_path / "a.wav"
        path.write_bytes(b"RIFF")
        transcriptions = Transcriptions()
        backend = self.backend(transcriptions, model)
        assert backend.supports_recognition and backend.recognition_support == "model"
        backend.transcribe(str(path), recognition=CONTEXT)
        assert hints_of(transcriptions.calls[0]) == transcription_hints(model, CONTEXT)

    def test_no_context_sends_exactly_what_it_did(self, tmp_path):
        path = tmp_path / "a.wav"
        path.write_bytes(b"RIFF")
        transcriptions = Transcriptions()
        self.backend(transcriptions, "gpt-transcribe").transcribe(str(path))
        self.backend(transcriptions, "gpt-transcribe").transcribe(str(path), recognition=RecognitionContext())
        assert [set(call) for call in transcriptions.calls] == [{"model", "file", "response_format"}] * 2

    @pytest.mark.parametrize("status", [400, 422])
    def test_a_rejected_request_is_retried_once_without_hints(self, status, tmp_path, caplog):
        path = tmp_path / "a.wav"
        path.write_bytes(b"RIFF")
        transcriptions = Transcriptions(fail=lambda call: Rejected(status) if hints_of(call) else None)
        backend = self.backend(transcriptions, "gpt-transcribe")
        with caplog.at_level("WARNING"):
            assert backend.transcribe(str(path), recognition=CONTEXT) == "text for a.wav"
        assert [bool(hints_of(call)) for call in transcriptions.calls] == [True, False]
        assert "2 keywords" in caplog.text
        assert "Ksenia" not in caplog.text and "Kubernetes" not in caplog.text

    @pytest.mark.parametrize("error", [Rejected(401), Rejected(429), Rejected(500), RuntimeError("offline")])
    def test_other_failures_are_not_retried(self, error, tmp_path):
        path = tmp_path / "a.wav"
        path.write_bytes(b"RIFF")
        transcriptions = Transcriptions(fail=lambda call: error)
        with pytest.raises(RuntimeError):
            self.backend(transcriptions, "gpt-transcribe").transcribe(str(path), recognition=CONTEXT)
        assert len(transcriptions.calls) == 1

    def test_a_rejection_without_hints_is_not_retried(self, tmp_path):
        path = tmp_path / "a.wav"
        path.write_bytes(b"RIFF")
        transcriptions = Transcriptions(fail=lambda call: Rejected(400))
        with pytest.raises(RuntimeError):
            self.backend(transcriptions, "gpt-transcribe").transcribe(str(path))
        assert len(transcriptions.calls) == 1

    def test_the_retired_model_fallback_gets_its_own_hints(self, tmp_path):
        path = tmp_path / "a.wav"
        path.write_bytes(b"RIFF")
        transcriptions = Transcriptions(
            fail=lambda call: Rejected(404, "model_not_found") if call["model"] == "whisper-1" else None
        )
        self.backend(transcriptions, "whisper-1").transcribe(str(path), recognition=CONTEXT)
        assert [call["model"] for call in transcriptions.calls] == ["whisper-1", config.DEFAULT_API_MODEL]
        assert hints_of(transcriptions.calls[0]) == {"prompt": "Ksenia, Kubernetes.", "language": "de"}
        assert hints_of(transcriptions.calls[1]) == {"keywords": ["Ksenia", "Kubernetes"], "languages": ["de"]}

    def test_every_chunk_carries_the_hints(self, chunks, split):  # noqa: F811
        transcriptions = Transcriptions()
        backend = make_backend(transcriptions)
        backend.model_type = "gpt-transcribe"
        split(chunks)
        backend.transcribe(LARGE_FILE, recognition=CONTEXT)
        assert len(transcriptions.calls) == len(chunks)
        assert all(hints_of(call) == transcription_hints("gpt-transcribe", CONTEXT)
                   for call in transcriptions.calls)


@pytest.mark.parametrize("model", ["gpt-transcribe", "whisper-1"])
def test_sdk_sends_the_hint_fields_without_network(chunks, model):  # noqa: F811
    import httpx
    from openai import OpenAI

    requests = []

    def respond(request):
        requests.append(request.read().decode("utf-8"))
        if model == "gpt-transcribe":
            return httpx.Response(200, json={"text": "hello"})
        return httpx.Response(200, text="hello")

    with OpenAI(api_key="sk-test", http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        backend = make_backend(None)
        backend.client = client
        backend.model_type = model
        assert backend.transcribe(chunks[0], recognition=CONTEXT) == "hello"
    body = requests[0]
    if model == "gpt-transcribe":
        assert body.count("\r\n\r\nKsenia\r\n") == 1 and "\r\n\r\nKubernetes\r\n" in body
        assert 'name="keywords' in body and 'name="languages' in body
        assert 'name="prompt"' not in body
    else:
        assert 'name="prompt"\r\n\r\nKsenia, Kubernetes.\r\n' in body
        assert 'name="language"\r\n\r\nde\r\n' in body
        assert 'name="keywords' not in body


def test_sdk_rejection_is_retried_without_hints(chunks):  # noqa: F811
    import httpx
    from openai import OpenAI

    bodies = []

    def respond(request):
        body = request.read().decode("utf-8")
        bodies.append(body)
        if 'name="keywords' in body:
            return httpx.Response(400, json={"error": {"message": "bad keywords", "code": "invalid_value"}})
        return httpx.Response(200, json={"text": "plain"})

    with OpenAI(api_key="sk-test", max_retries=0,
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        backend = make_backend(None)
        backend.client = client
        backend.model_type = "gpt-transcribe"
        assert backend.transcribe(chunks[0], recognition=CONTEXT) == "plain"
    assert len(bodies) == 2 and 'name="keywords' not in bodies[1]


class FakeProcess:
    instances = []

    def __init__(self, *args, **kwargs):
        self.requests = []
        self.process = SimpleNamespace(poll=lambda: 0)
        FakeProcess.instances.append(self)

    def request(self, op, **payload):
        self.requests.append((op, payload))
        return {"text": "ok"}

    def close(self):
        pass


def test_isolated_client_forwards_only_the_hints_given(monkeypatch, tmp_path):
    monkeypatch.setattr(isolated, "SpeechProcess", FakeProcess)
    FakeProcess.instances.clear()
    client = isolated.IsolatedOpenAIClient(api_key="sk-test")
    audio = SimpleNamespace(name=str(tmp_path / "a.wav"))
    client.create(model="whisper-1", file=audio, response_format="text")
    client.create(model="gpt-transcribe", file=audio, response_format="json",
                  keywords=["Ksenia"], languages=["de"], prompt="", language=None)
    plain, hinted = (instance.requests[0][1] for instance in FakeProcess.instances)
    assert not {"prompt", "language", "languages", "keywords"} & set(plain)
    assert hinted["keywords"] == ["Ksenia"] and hinted["languages"] == ["de"]
    assert "prompt" not in hinted and "language" not in hinted


def test_worker_op_passes_hints_to_the_sdk(monkeypatch, tmp_path):
    import openai

    created = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.audio = SimpleNamespace(transcriptions=SimpleNamespace(
                create=lambda **call: created.append(call) or SimpleNamespace(text="done")))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    path = tmp_path / "a.wav"
    path.write_bytes(b"RIFF")
    base = {"api_key": "sk", "audio_path": str(path), "model": "whisper-1", "response_format": "text"}
    assert isolated_worker.openai_transcribe(base) == {"text": "done"}
    isolated_worker.openai_transcribe({**base, "prompt": "Ksenia.", "language": "de", "keywords": []})
    assert set(created[0]) == {"file", "model", "response_format"}
    assert created[1]["prompt"] == "Ksenia." and created[1]["language"] == "de"
    assert "keywords" not in created[1]
