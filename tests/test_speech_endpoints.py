"""OpenAI-compatible transcription servers: presets, settings, catalogs, requests."""
import http.server
import json
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from services import credentials, isolated_worker, speech_endpoints
from services.recognition_context import RecognitionContext
from services.settings import SettingsKey, settings_manager
from services.speech_endpoints import (
    OPENAI_ENDPOINT_ID,
    configured_endpoint,
    selected_endpoint,
    selected_model,
)
from tests.test_openai_backend import make_backend
from transcriber.openai_backend import response_format, transcription_hints

CONTEXT = RecognitionContext(language="de-DE", phrases=("Ksenia", "Kubernetes"))


class TestSelection:
    def test_existing_users_stay_on_openai_with_their_model(self):
        settings_manager.save_setting(SettingsKey.API_TRANSCRIPTION_MODEL, "gpt-4o-transcribe")
        assert selected_endpoint().id == OPENAI_ENDPOINT_ID
        assert selected_endpoint().base_url is None
        assert selected_model() == "gpt-4o-transcribe"

    @pytest.mark.parametrize("saved", ["nonsense", 42, None])
    def test_an_unknown_endpoint_is_openai(self, saved):
        settings = {SettingsKey.API_TRANSCRIPTION_ENDPOINT: saved}
        assert selected_endpoint(settings).id == OPENAI_ENDPOINT_ID

    def test_a_custom_slot_without_an_address_is_openai(self):
        speech_endpoints.save_selection("custom")
        assert selected_endpoint().id == OPENAI_ENDPOINT_ID

    def test_each_server_keeps_its_own_model(self):
        speech_endpoints.save_model("openai", "whisper-1")
        speech_endpoints.save_model("speaches", "Systran/faster-distil-whisper-small.en")
        speech_endpoints.save_selection("speaches")
        assert selected_model() == "Systran/faster-distil-whisper-small.en"
        speech_endpoints.save_selection("vllm")
        assert selected_model() == "openai/whisper-large-v3-turbo"
        speech_endpoints.save_selection("openai")
        assert selected_model() == "whisper-1"

    def test_openai_takes_only_its_own_models(self):
        with pytest.raises(ValueError):
            speech_endpoints.save_model("openai", "whisper-large-v3")

    def test_hosted_services_have_fixed_addresses(self):
        with pytest.raises(ValueError):
            speech_endpoints.save_server(
                "groq", base_url="http://127.0.0.1:1/v1", api_key_env="", model="m",
            )


class TestServerSettings:
    def test_saved_address_key_variable_and_model(self):
        speech_endpoints.save_server(
            "speaches", base_url="http://gpu-box:8000/", api_key_env="SPEACHES_API_KEY",
            model="Systran/faster-whisper-large-v3",
        )
        endpoint = configured_endpoint("speaches")
        assert endpoint.base_url == "http://gpu-box:8000/v1"
        assert endpoint.connection.api_key_env == "SPEACHES_API_KEY"
        assert speech_endpoints.endpoint_model(endpoint) == "Systran/faster-whisper-large-v3"
        assert not endpoint.is_local

    @pytest.mark.parametrize("url", [
        "ftp://host/v1", "http://user:pw@host/v1", "http://host/v1?key=1", "",
    ])
    def test_rejected_addresses(self, url):
        with pytest.raises(ValueError):
            speech_endpoints.save_server("custom", base_url=url, api_key_env="", model="m")

    def test_a_damaged_saved_server_falls_back_to_the_preset(self):
        settings = {SettingsKey.API_TRANSCRIPTION_SERVERS: {
            "vllm": {"base_url": "not a url", "api_key_env": "lower"},
        }}
        assert configured_endpoint("vllm", settings) == configured_endpoint("vllm", {})

    @pytest.mark.parametrize("endpoint_id,url,self_hosted", [
        ("openai", None, False),
        ("groq", None, False),
        ("speaches", "http://gpu-box:8000/v1", True),
        ("custom", "http://127.0.0.1:9000/v1", True),
        ("custom", "https://speech.example.com/v1", False),
    ])
    def test_servers_the_user_runs_get_the_long_timeout(self, endpoint_id, url, self_hosted):
        if url:
            speech_endpoints.save_server(endpoint_id, base_url=url, api_key_env="", model="m")
        endpoint = configured_endpoint(endpoint_id)
        assert endpoint.self_hosted is self_hosted
        assert endpoint.timeout_s == (
            speech_endpoints.SELF_HOSTED_TIMEOUT_S if self_hosted else speech_endpoints.HOSTED_TIMEOUT_S
        )

    def test_custom_server_is_usable_once_it_has_an_address(self):
        speech_endpoints.save_server(
            "custom", base_url="http://127.0.0.1:9000/v1", api_key_env="", model="tiny",
        )
        speech_endpoints.save_selection("custom")
        assert selected_endpoint().name == "Custom server"
        assert selected_model() == "tiny"

    def test_only_endpoints_that_send_a_key_are_credential_owners(self):
        speech_endpoints.save_server(
            "localai", base_url="http://127.0.0.1:8080/v1", api_key_env="LOCALAI_API_KEY",
            model="whisper-1",
        )
        assert speech_endpoints.credential_owners() == [
            ("OPENAI_API_KEY", "OpenAI"), ("GROQ_API_KEY", "Groq"),
            ("LOCALAI_API_KEY", "LocalAI"),
        ]


class TestDestination:
    def test_names_where_audio_goes(self):
        assert speech_endpoints.destination(configured_endpoint("openai")) == "OpenAI (api.openai.com)"
        assert speech_endpoints.destination(configured_endpoint("groq")) == "Groq (api.groq.com)"
        local = configured_endpoint("speaches")
        assert local.is_local
        assert speech_endpoints.destination(local) == "your local server at 127.0.0.1:8000"

    def test_privacy_sentence(self):
        assert speech_endpoints.audio_destination_sentence(configured_endpoint("vllm")) == (
            "Audio stays on this computer: it goes to your local server at 127.0.0.1:8000."
        )
        assert speech_endpoints.audio_destination_sentence(configured_endpoint("groq")) == (
            "Audio is uploaded to Groq (api.groq.com)."
        )

    def test_local_servers_need_no_key_but_hosted_ones_do(self):
        assert speech_endpoints.api_key(configured_endpoint("whisper_cpp"))
        assert speech_endpoints.api_key(configured_endpoint("groq")) is None
        credentials.store().set("GROQ_API_KEY", "gsk-test")
        assert speech_endpoints.api_key(configured_endpoint("groq")) == "gsk-test"


class TestRequestFields:
    def test_whisper_servers_take_a_prompt_and_one_language(self):
        speaches = configured_endpoint("speaches")
        # Even a model named like OpenAI's newest takes Whisper fields elsewhere.
        for model in ("Systran/faster-whisper-small", "gpt-transcribe"):
            assert transcription_hints(speaches, model, CONTEXT) == {
                "prompt": "Ksenia, Kubernetes.", "language": "de",
            }
            assert response_format(speaches, model) == "json"

    def test_whisper_cpp_is_asked_to_detect_an_unknown_language(self):
        server = configured_endpoint("whisper_cpp")
        assert transcription_hints(server, "whisper-1", None) == {"language": "auto"}
        assert transcription_hints(server, "whisper-1", CONTEXT)["language"] == "de"

    def test_openai_fields_are_unchanged(self):
        openai = configured_endpoint("openai")
        assert response_format(openai, "gpt-transcribe") == "json"
        assert response_format(openai, "whisper-1") == "text"
        assert transcription_hints(openai, "whisper-1", None) == {}


@contextmanager
def serve(routes, seen=None):
    """A localhost HTTP server answering ``routes[path] -> (status, body)``."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _answer(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            if seen is not None:
                seen.append((self.command, self.path, dict(self.headers), body))
            status, payload = routes.get(self.path, (404, {"detail": "Not Found"}))
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _answer

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def _point(endpoint_id, url, model="m"):
    speech_endpoints.save_server(endpoint_id, base_url=url, api_key_env="", model=model)
    return configured_endpoint(endpoint_id)


class TestCatalogs:
    def test_speaches_lists_only_its_speech_models(self):
        catalog = {"data": [
            {"id": "Systran/faster-whisper-small", "task": "automatic-speech-recognition"},
            {"id": "speaches-ai/Kokoro-82M-v1.0-ONNX", "task": "text-to-speech"},
            {"id": "Systran/faster-distil-whisper-large-v3", "task": "automatic-speech-recognition"},
        ]}
        seen = []
        with serve({"/v1/models": (200, catalog)}, seen) as url:
            models = speech_endpoints.list_models(_point("speaches", url))
        assert models == ["Systran/faster-distil-whisper-large-v3", "Systran/faster-whisper-small"]
        # No key configured: the placeholder, which an open server ignores.
        assert seen[0][2]["Authorization"] == f"Bearer {speech_endpoints.api_key(_point('speaches', url))}"

    def test_localai_lists_models_that_transcribe(self):
        catalog = {"object": "list", "data": [
            {"id": "whisper-1", "capabilities": ["transcript"]},
            {"id": "qwen3-8b", "capabilities": ["chat", "completion"]},
        ]}
        with serve({"/v1/models/capabilities": (200, catalog)}) as url:
            assert speech_endpoints.list_models(_point("localai", url)) == ["whisper-1"]

    def test_groq_offers_its_whisper_models_without_asking(self):
        assert speech_endpoints.list_models(configured_endpoint("groq")) == [
            "whisper-large-v3-turbo", "whisper-large-v3",
        ]

    def test_a_server_without_a_catalog_lists_nothing(self):
        with serve({}) as url:
            assert speech_endpoints.list_models(_point("vllm", url)) == []
        assert speech_endpoints.list_models(configured_endpoint("whisper_cpp")) == []

    def test_openai_lists_its_curated_models(self):
        assert speech_endpoints.list_models(configured_endpoint("openai"))[0] == "gpt-transcribe"


class TestBackend:
    def test_a_selected_server_gets_its_model_and_json(self, tmp_path):
        calls = []

        class Transcriptions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(text=" hallo ")

        backend = make_backend(Transcriptions(), "api")
        speech_endpoints.save_server(
            "speaches", base_url="http://127.0.0.1:8000/v1", api_key_env="",
            model="Systran/faster-whisper-small",
        )
        speech_endpoints.save_selection("speaches")
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        assert backend.transcribe(str(audio), recognition=CONTEXT) == "hallo"
        assert calls[0]["model"] == "Systran/faster-whisper-small"
        assert calls[0]["response_format"] == "json"
        assert calls[0]["prompt"] == "Ksenia, Kubernetes." and calls[0]["language"] == "de"
        assert backend.name == "Speaches (Systran/faster-whisper-small)"

    def test_a_missing_model_on_another_server_is_not_swapped_for_openais(self, tmp_path):
        class Gone(RuntimeError):
            status_code = 404

        calls = []

        class Transcriptions:
            def create(self, **kwargs):
                calls.append(kwargs)
                raise Gone("Model 'whisper-1' is not installed locally.")

        backend = make_backend(Transcriptions(), "api")
        _point("speaches", "http://127.0.0.1:8000/v1", model="whisper-1")
        speech_endpoints.save_selection("speaches")
        audio = tmp_path / "a.wav"
        audio.write_bytes(b"RIFF")
        with pytest.raises(Gone):
            backend.transcribe(str(audio))
        assert [call["model"] for call in calls] == ["whisper-1"]

    def test_a_server_needing_an_unset_key_is_unavailable(self):
        backend = make_backend(None, "api")
        speech_endpoints.save_server(
            "vllm", base_url="http://127.0.0.1:8000/v1", api_key_env="VLLM_API_KEY", model="m",
        )
        speech_endpoints.save_selection("vllm")
        assert not backend.is_available()
        credentials.store().set("VLLM_API_KEY", "token")
        assert backend.is_available()


def test_worker_sends_to_the_chosen_server(monkeypatch, tmp_path):
    import openai

    clients = []

    class FakeOpenAI:
        def __init__(self, **kwargs):
            clients.append(kwargs)
            self.audio = SimpleNamespace(transcriptions=SimpleNamespace(
                create=lambda **call: SimpleNamespace(text="done")))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    path = tmp_path / "a.wav"
    path.write_bytes(b"RIFF")
    request = {"api_key": "k", "audio_path": str(path), "model": "m",
               "response_format": "json", "read_timeout": 600.}
    isolated_worker.openai_transcribe({**request, "base_url": "http://127.0.0.1:8000/v1"})
    isolated_worker.openai_transcribe({**request, "base_url": None})
    assert [c["base_url"] for c in clients] == ["http://127.0.0.1:8000/v1", None]
    assert clients[0]["timeout"].read == 600.


def test_isolated_upload_reaches_a_local_server(tmp_path):
    """Real worker process, real SDK, a localhost server shaped like Speaches."""
    from transcriber.openai_backend import OpenAIBackend

    seen = []
    routes = {"/v1/audio/transcriptions": (200, {"text": " from the local server "})}
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF" + b"0" * 64)
    with serve(routes, seen) as url:
        _point("speaches", url, model="Systran/faster-whisper-small")
        speech_endpoints.save_selection("speaches")
        backend = OpenAIBackend("api")
        try:
            assert backend.transcribe(str(audio)) == "from the local server"
        finally:
            backend.cleanup()
    method, path, _headers, body = seen[0]
    assert (method, path) == ("POST", "/v1/audio/transcriptions")
    assert 'name="model"\r\n\r\nSystran/faster-whisper-small\r\n' in body
    assert 'name="response_format"\r\n\r\njson\r\n' in body
