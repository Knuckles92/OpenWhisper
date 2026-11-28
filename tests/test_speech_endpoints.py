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
        ("assemblyai", None, False),
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
            ("ASSEMBLYAI_API_KEY", "AssemblyAI"),
            ("LOCALAI_API_KEY", "LocalAI"),
        ]

    def test_hosted_endpoints_save_known_models_and_reject_unknown_ones(self):
        speech_endpoints.save_model("groq", "whisper-large-v3")
        assert speech_endpoints.endpoint_model(configured_endpoint("groq")) == "whisper-large-v3"
        speech_endpoints.save_model("assemblyai", "verbatim")
        assert speech_endpoints.endpoint_model(configured_endpoint("assemblyai")) == "verbatim"
        with pytest.raises(ValueError):
            speech_endpoints.save_model("assemblyai", "invalid-mode")
        with pytest.raises(ValueError):
            speech_endpoints.save_server(
                "assemblyai", base_url="http://127.0.0.1:1/v1", api_key_env="", model="cleaned",
            )


class TestDestination:
    def test_names_where_audio_goes(self):
        assert speech_endpoints.destination(configured_endpoint("openai")) == "OpenAI (api.openai.com)"
        assert speech_endpoints.destination(configured_endpoint("groq")) == "Groq (api.groq.com)"
        assert speech_endpoints.destination(configured_endpoint("assemblyai")) == (
            "AssemblyAI (dictation.assemblyai.com)"
        )
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
        assert speech_endpoints.audio_destination_sentence(configured_endpoint("assemblyai")) == (
            "Audio is uploaded to AssemblyAI (dictation.assemblyai.com)."
        )

    def test_local_servers_need_no_key_but_hosted_ones_do(self):
        assert speech_endpoints.api_key(configured_endpoint("whisper_cpp"))
        assert speech_endpoints.api_key(configured_endpoint("groq")) is None
        assert speech_endpoints.api_key(configured_endpoint("assemblyai")) is None
        credentials.store().set("GROQ_API_KEY", "gsk-test")
        credentials.store().set("ASSEMBLYAI_API_KEY", "aai-test")
        assert speech_endpoints.api_key(configured_endpoint("groq")) == "gsk-test"
        assert speech_endpoints.api_key(configured_endpoint("assemblyai")) == "aai-test"


class TestRequestFields:
    def test_whisper_servers_take_a_prompt_and_one_language(self):
        speaches = configured_endpoint("speaches")
        # Even a model named like OpenAI's newest takes Whisper fields elsewhere.
        for model in ("Systran/faster-whisper-small", "gpt-transcribe"):
            assert transcription_hints(speaches, model, CONTEXT) == {
                "prompt": "Ksenia, Kubernetes.", "language": "de",
            }
            assert response_format(speaches, model) == "json"

    def test_assemblyai_takes_keyterms_prompt_and_language_codes(self):
        assemblyai = configured_endpoint("assemblyai")
        assert transcription_hints(assemblyai, "cleaned", CONTEXT) == {
            "keyterms_prompt": ["Ksenia", "Kubernetes"],
            "language_codes": ["de"],
        }
        assert transcription_hints(assemblyai, "verbatim", None) == {}

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


class TestAssemblyAIDictation:
    def test_lists_cleaned_and_verbatim_modes_without_asking(self):
        endpoint = configured_endpoint("assemblyai")
        assert speech_endpoints.list_models(endpoint) == ["cleaned", "verbatim"]
        assert speech_endpoints.model_label(endpoint, "cleaned") == "Cleaned (default)"
        assert speech_endpoints.model_label(endpoint, "verbatim") == "Verbatim"

    def test_worker_sends_raw_auth_config_before_audio_and_picks_mode(self, tmp_path):
        seen = []
        routes = {
            "/v1/transcribe": (200, {
                "text": "Um, send me the Q3 numbers.",
                "llm_response": "Send me the Q3 numbers.",
                "llm_error": None,
            }),
        }
        audio = tmp_path / "clip.wav"
        audio.write_bytes(b"RIFF" + b"0" * 64)
        with serve(routes, seen) as url:
            base_request = {
                "api_key": "aai-secret-key",
                "base_url": url,
                "audio_path": str(audio),
                "read_timeout": 90.0,
                "keyterms_prompt": ["Ksenia", "Kubernetes"],
                "language_codes": ["de"],
            }
            cleaned = isolated_worker.assemblyai_transcribe({**base_request, "model": "cleaned"})
            verbatim = isolated_worker.assemblyai_transcribe({**base_request, "model": "verbatim"})

        assert cleaned == {"text": "Send me the Q3 numbers."}
        assert verbatim == {"text": "Um, send me the Q3 numbers."}
        method, path, headers, body = seen[0]
        assert (method, path) == ("POST", "/v1/transcribe")
        assert headers["Authorization"] == "aai-secret-key"
        config_pos = body.index('name="config"')
        audio_pos = body.index('name="audio"')
        assert config_pos < audio_pos
        assert '"keyterms_prompt": ["Ksenia", "Kubernetes"]' in body
        assert '"language_codes": ["de"]' in body
        assert "Content-Type: audio/wav" in body

    def test_cleaned_mode_falls_back_to_text_when_llm_response_is_null(self, tmp_path):
        routes = {
            "/v1/transcribe": (200, {
                "text": "Fallback verbatim text.",
                "llm_response": None,
                "llm_error": "timeout",
            }),
        }
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(b"ID3" + b"0" * 32)
        seen = []
        with serve(routes, seen) as url:
            result = isolated_worker.assemblyai_transcribe({
                "api_key": "aai-key",
                "base_url": url,
                "audio_path": str(audio),
                "model": "cleaned",
                "read_timeout": 90.0,
            })
        assert result == {"text": "Fallback verbatim text."}
        assert "Content-Type: audio/mpeg" in seen[0][3]
        assert 'name="config"' not in seen[0][3]

    @pytest.mark.parametrize("status,payload,expected_text", [
        (401, {"status": 401, "title": "Unauthorized", "detail": "Invalid API key"}, "Invalid API key"),
        (413, {"error": "Audio too long", "error_code": "audio_too_long"}, "Audio too long"),
    ])
    def test_parses_both_assemblyai_error_shapes(self, tmp_path, status, payload, expected_text):
        audio = tmp_path / "clip.wav"
        audio.write_bytes(b"RIFF" + b"0" * 32)
        with serve({"/v1/transcribe": (status, payload)}) as url:
            with pytest.raises(isolated_worker._AssemblyAIStatusError) as exc_info:
                isolated_worker.assemblyai_transcribe({
                    "api_key": "bad-key",
                    "base_url": url,
                    "audio_path": str(audio),
                    "model": "cleaned",
                    "read_timeout": 90.0,
                })
        assert exc_info.value.status_code == status
        assert expected_text in str(exc_info.value)

    def test_isolated_upload_reaches_assemblyai_and_retries_without_rejected_hints(
        self, monkeypatch, tmp_path,
    ):
        """Real worker process, real httpx, localhost server shaped like AssemblyAI Dictation."""
        from dataclasses import replace
        from transcriber.openai_backend import OpenAIBackend

        seen = []
        calls = {"n": 0}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
                seen.append((self.path, dict(self.headers), body))
                calls["n"] += 1
                if calls["n"] == 1:
                    status, payload = 400, {"status": 400, "title": "Bad Request", "detail": "Unsupported language"}
                else:
                    status, payload = 200, {
                        "text": "Uh hello world",
                        "llm_response": "Hello world.",
                        "llm_error": None,
                    }
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/v1"
            base_endpoint = configured_endpoint("assemblyai")
            local_aai = replace(
                base_endpoint,
                connection=replace(base_endpoint.connection, base_url=url),
            )
            monkeypatch.setitem(speech_endpoints._BY_ID, "assemblyai", local_aai)
            credentials.store().set("ASSEMBLYAI_API_KEY", "aai-live-key")
            speech_endpoints.save_selection("assemblyai")
            audio = tmp_path / "dictation.wav"
            audio.write_bytes(b"RIFF" + b"0" * 64)
            backend = OpenAIBackend("api")
            try:
                assert backend.transcribe(str(audio), recognition=CONTEXT) == "Hello world."
                assert backend.name == "AssemblyAI (cleaned)"
            finally:
                backend.cleanup()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

        assert len(seen) == 2
        assert seen[0][0] == "/v1/transcribe"
        assert seen[0][1]["Authorization"] == "aai-live-key"
        assert 'name="config"' in seen[0][2]
        assert 'name="config"' not in seen[1][2]

    @pytest.mark.parametrize("status,expected_ok,expected_fragment", [
        (400, True, "accepted the key"),
        (415, True, "accepted the key"),
        (401, False, "rejected the key (HTTP 401)"),
        (403, False, "denied access (HTTP 403)"),
        (503, False, "answered HTTP 503"),
    ])
    def test_verify_api_key_checks_assemblyai_with_raw_header(
        self, status, expected_ok, expected_fragment,
    ):
        from dataclasses import replace
        from services.text_llm import verify_api_key

        seen = []
        with serve({"/v1/transcribe": (status, {"detail": "should-not-leak"})}, seen) as url:
            profile = replace(configured_endpoint("assemblyai").connection, base_url=url)
            ok, detail = verify_api_key(profile, "aai-test-key")

        assert ok is expected_ok
        assert expected_fragment in detail
        assert "should-not-leak" not in detail
        assert seen[0][0] == "POST"
        assert seen[0][1] == "/v1/transcribe"
        assert seen[0][2]["Authorization"] == "aai-test-key"
