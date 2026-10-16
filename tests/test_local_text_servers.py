"""Local inference servers: model discovery, request shape, and detection.

Each fake answers with the fields its upstream server documents (see the
module docstring of ``services.local_text_servers``).
"""
import json
from unittest.mock import patch

import httpx
import pytest
from openai import OpenAI

from services.local_text_servers import detect_running_servers, identify_server
from services.text_generation import generate
from services.text_llm import get_profile, list_chat_models, upsert_custom_profile
from services.text_model_catalog import model_spec
from services.transcript_cleanup import TranscriptCleanup


def _json(body, status=200):
    return httpx.Response(status, json=body)


def _routes(table):
    def handler(request):
        answer = table.get((request.method, request.url.path))
        if answer is None:
            return httpx.Response(404, text="Not Found")
        return answer(request) if callable(answer) else answer
    return handler


VLLM = {
    ("GET", "/v1/models"): _json({"data": [
        {"id": "Qwen/Qwen3-8B", "owned_by": "vllm", "max_model_len": 8192},
    ]}),
    ("GET", "/version"): _json({"version": "0.14.0"}),
}
LLAMA_CPP = {
    ("GET", "/v1/models"): _json({"data": [
        {"id": "qwen3-4b.gguf", "owned_by": "llamacpp",
         "meta": {"n_ctx": 16384, "n_ctx_train": 40960}},
    ]}),
    ("GET", "/props"): _json({"build_info": "b7000-abc", "default_generation_settings": {"n_ctx": 16384}}),
}
LM_STUDIO = {
    ("GET", "/v1/models"): _json({"data": [
        {"id": "qwen/qwen3-4b"}, {"id": "google/gemma-3-4b"},
        {"id": "text-embedding-nomic-embed-text-v1.5"},
    ]}),
    ("GET", "/api/v1/models"): _json({"models": [
        {"key": "qwen/qwen3-4b", "type": "llm", "max_context_length": 40960,
         "loaded_instances": [{"id": "qwen/qwen3-4b", "config": {"context_length": 8192}}],
         "capabilities": {"trained_for_tool_use": True,
                          "reasoning": {"allowed_options": ["off", "low", "medium", "high"], "default": "medium"}}},
        {"key": "google/gemma-3-4b", "type": "llm", "max_context_length": 131072,
         "loaded_instances": [], "capabilities": {"vision": True, "trained_for_tool_use": False}},
        {"key": "text-embedding-nomic-embed-text-v1.5", "type": "embedding",
         "max_context_length": 2048, "loaded_instances": []},
    ]}),
}
LOCALAI = {
    ("GET", "/v1/models"): _json({"data": [
        {"id": "qwen3-8b", "object": "model"}, {"id": "llama-3.2-1b", "object": "model"},
        {"id": "kokoro", "object": "model"}, {"id": "whisper-base", "object": "model"},
        {"id": "bert-embeddings", "object": "model"},
    ]}),
    ("GET", "/v1/models/capabilities"): _json({"data": [
        {"id": "qwen3-8b", "capabilities": ["chat", "completion", "tools", "thinking"], "context_size": 32768},
        {"id": "llama-3.2-1b", "capabilities": ["chat", "completion"], "context_size": 0},
        {"id": "kokoro", "capabilities": ["tts"], "context_size": 0},
        {"id": "whisper-base", "capabilities": ["transcript"], "context_size": 0},
        {"id": "bert-embeddings", "capabilities": ["embeddings"], "context_size": 512},
    ]}),
    ("GET", "/.well-known/localai.json"): _json({"version": "v3.9.0", "endpoints": {"models": "/v1/models"}}),
}
OLLAMA_SHOW = {
    "qwen3": {"capabilities": ["completion", "tools", "thinking"], "parameters": "num_ctx 16384"},
    "llama3.2": {"capabilities": ["completion", "tools"], "parameters": ""},
    "nomic-embed-text": {"capabilities": ["embedding"]},
}
OLLAMA = {
    ("GET", "/v1/models"): _json({"data": [{"id": m} for m in OLLAMA_SHOW]}),
    ("POST", "/api/show"): lambda r: _json(OLLAMA_SHOW[json.loads(r.content)["model"]]),
    ("GET", "/"): httpx.Response(200, text="Ollama is running"),
}


def test_vllm_reports_its_context_and_takes_the_effort_field(local_server):
    local_server(_routes(VLLM))
    profile = get_profile("vllm", {})
    assert list_chat_models(profile) == ["Qwen/Qwen3-8B"]
    spec = model_spec(profile, "Qwen/Qwen3-8B")
    assert spec.context_window == 8192
    assert spec.max_output_tokens == 4096
    assert spec.reasoning_format == "openai"
    assert spec.thinking_levels == {"off": "none"}


def test_llama_cpp_reports_its_runtime_context(local_server):
    local_server(_routes(LLAMA_CPP))
    profile = get_profile("llama_cpp", {})
    assert list_chat_models(profile) == ["qwen3-4b.gguf"]
    assert model_spec(profile, "qwen3-4b.gguf").context_window == 16384


def test_lm_studio_drops_embeddings_and_reads_reasoning_options(local_server):
    local_server(_routes(LM_STUDIO))
    profile = get_profile("lm_studio", {})
    assert list_chat_models(profile) == ["google/gemma-3-4b", "qwen/qwen3-4b"]
    qwen = model_spec(profile, "qwen/qwen3-4b")
    assert qwen.context_window == 8192  # what is loaded, not the maximum
    assert qwen.reasoning and qwen.reasoning_format == "openai"
    assert qwen.thinking_levels == {"off": None, "low": "low", "medium": "medium", "high": "high"}
    gemma = model_spec(profile, "google/gemma-3-4b")
    assert gemma.context_window == 131072
    assert not gemma.tools
    assert gemma.reasoning_format == ""


def test_lm_studio_before_0_4_uses_the_v0_model_list(local_server):
    table = {key: value for key, value in LM_STUDIO.items() if key[1] != "/api/v1/models"}
    table[("GET", "/api/v0/models")] = _json({"data": [
        {"id": "qwen/qwen3-4b", "type": "llm", "max_context_length": 40960, "capabilities": ["tool_use"]},
        {"id": "google/gemma-3-4b", "type": "vlm", "max_context_length": 131072},
        {"id": "text-embedding-nomic-embed-text-v1.5", "type": "embeddings"},
    ]})
    local_server(_routes(table))
    profile = get_profile("lm_studio", {})
    assert list_chat_models(profile) == ["google/gemma-3-4b", "qwen/qwen3-4b"]
    assert model_spec(profile, "qwen/qwen3-4b").context_window == 40960


def test_localai_lists_only_chat_models(local_server):
    local_server(_routes(LOCALAI))
    profile = get_profile("localai", {})
    assert list_chat_models(profile) == ["llama-3.2-1b", "qwen3-8b"]
    thinking = model_spec(profile, "qwen3-8b")
    assert (thinking.context_window, thinking.tools, thinking.reasoning_format) == (32768, True, "openai")
    plain = model_spec(profile, "llama-3.2-1b")
    assert (plain.context_window, plain.tools, plain.reasoning_format) == (32768, False, "")


def test_older_localai_without_capabilities_falls_back_to_names(local_server):
    table = {key: value for key, value in LOCALAI.items() if key[1] != "/v1/models/capabilities"}
    local_server(_routes(table))
    assert list_chat_models(get_profile("localai", {})) == ["kokoro", "llama-3.2-1b", "qwen3-8b"]


def test_ollama_thinking_models_take_the_effort_field(local_server):
    local_server(_routes(OLLAMA))
    profile = get_profile("ollama", {})
    assert list_chat_models(profile) == ["llama3.2", "qwen3"]
    assert model_spec(profile, "qwen3").thinking_levels == {"off": "none"}
    plain = model_spec(profile, "llama3.2")
    # No num_ctx: Ollama allocates 4096 tokens unless the GPU is very large.
    assert (plain.context_window, plain.reasoning_format) == (4096, "")


def test_custom_localhost_endpoint_is_identified_and_matches_the_preset(local_server):
    requests = local_server(_routes(LM_STUDIO))
    custom = upsert_custom_profile({}, name="Studio", base_url="http://localhost:1234/v1")
    assert list_chat_models(custom) == ["google/gemma-3-4b", "qwen/qwen3-4b"]
    preset = get_profile("lm_studio", {"lm_studio_base_url": "http://localhost:1234/v1"})
    assert model_spec(custom, "qwen/qwen3-4b") == model_spec(preset, "qwen/qwen3-4b")
    assert model_spec(custom, "qwen/qwen3-4b").context_window == 8192
    assert any(r.url.path == "/.well-known/localai.json" for r in requests)


def test_a_preset_with_a_key_sends_it_to_discovery(local_server):
    requests = local_server(_routes(VLLM))
    profile = get_profile("vllm", {"vllm_api_key_env": "VLLM_API_KEY"})
    with patch("services.text_llm.resolve_credential", return_value="sk-local"):
        list_chat_models(profile)
    assert requests[0].headers["authorization"] == "Bearer sk-local"
    with patch("services.text_llm.resolve_credential", return_value=None), pytest.raises(RuntimeError):
        list_chat_models(profile)


@pytest.mark.parametrize("table,server", [
    (VLLM, "vllm"), (LLAMA_CPP, "llama_cpp"), (LM_STUDIO, "lm_studio"),
    (LOCALAI, "localai"), (OLLAMA, "ollama"), ({}, None),
])
def test_identify_names_the_server_that_answers(local_server, table, server):
    local_server(_routes({key: value for key, value in table.items() if key[1] != "/v1/models"}
                         if server != "vllm" else table))
    assert identify_server("http://127.0.0.1:8080/v1") == server


def test_llama_cpp_with_a_key_is_still_identified(local_server):
    local_server(_routes({("GET", "/props"): _json(
        {"error": {"code": 401, "message": "Invalid API Key", "type": "authentication_error"}}, 401)}))
    assert identify_server("http://127.0.0.1:8080/v1") == "llama_cpp"


def test_detection_probes_only_this_computer(local_server):
    requests = local_server(_routes(LOCALAI))
    found = detect_running_servers([
        "http://127.0.0.1:8080/v1", "http://192.168.1.5:8080/v1", "http://127.0.0.1:8080/v1",
    ])
    assert found == {"http://127.0.0.1:8080/v1": "localai"}
    assert {r.url.host for r in requests} == {"127.0.0.1"}


def test_nothing_listening_is_not_running():
    assert detect_running_servers(["http://127.0.0.1:1234/v1"]) == {}


def _chat_body(profile, model, **kwargs):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return _json({"choices": [{"message": {"role": "assistant", "content": "Clean."},
                                   "finish_reason": "stop"}]})

    client = OpenAI(api_key="dummy", base_url=profile.base_url, max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with client:
        generate(client, profile, model=model, messages=[{"role": "user", "content": "raw"}], **kwargs)
    return seen[0]


def test_local_servers_keep_cleanup_options_and_leave_output_to_the_server(local_server):
    local_server(_routes(VLLM))
    profile = get_profile("vllm", {})
    list_chat_models(profile)
    off = _chat_body(profile, "Qwen/Qwen3-8B", temperature=0, reasoning_level="off")
    assert off["temperature"] == 0
    assert off["reasoning_effort"] == "none"
    assert "max_tokens" not in off
    high = _chat_body(profile, "Qwen/Qwen3-8B", temperature=0, reasoning_level="high")
    assert high["reasoning_effort"] == "high"
    assert "temperature" not in high
    # A model the server never described gets no reasoning field at all.
    unknown = _chat_body(profile, "other", temperature=0, reasoning_level="off")
    assert unknown == {"model": "other", "messages": [{"role": "user", "content": "raw"}], "temperature": 0}


def test_hosted_catalogs_still_set_their_output_budget():
    profile = get_profile("groq", {})
    body = _chat_body(profile, "llama-3.3-70b-versatile", temperature=0)
    assert body["max_tokens"] == model_spec(profile, "llama-3.3-70b-versatile").max_output_tokens
    assert "temperature" not in body


def test_first_cleanup_learns_the_server_before_its_request(local_server):
    chat = []

    def completion(request):
        chat.append(json.loads(request.content))
        return _json({"choices": [{"message": {"role": "assistant", "content": "Clean."},
                                   "finish_reason": "stop"}]})

    table = dict(OLLAMA)
    table[("POST", "/v1/chat/completions")] = completion
    handler = _routes(table)
    local_server(handler)
    client = OpenAI(api_key="dummy", base_url="http://localhost:11434/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with client, patch("services.transcript_cleanup.create_openai_client", return_value=client):
        cleaner = TranscriptCleanup("ollama", "qwen3", reasoning="off")
        assert cleaner.cleanup("raw transcript") == "Clean."
    assert chat[0]["reasoning_effort"] == "none"
    assert chat[0]["temperature"] == 0


def _ollama_with_chat(answer):
    """The Ollama fake plus a chat route answering ``answer``; returns (handler, chat bodies)."""
    chat = []

    def completion(request):
        chat.append(json.loads(request.content))
        return _json({"choices": [{"message": {"role": "assistant", "content": answer},
                                   "finish_reason": "stop"}]})

    table = dict(OLLAMA)
    table[("POST", "/v1/chat/completions")] = completion
    return _routes(table), chat


def _ollama_judge(monkeypatch, handler, model="qwen3"):
    from services import text_judge, typesafe
    from services.settings import SettingsKey

    client = OpenAI(api_key="dummy", base_url="http://localhost:11434/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(text_judge, "_CLIENTS", {})
    monkeypatch.setattr(text_judge, "create_openai_client", lambda *a, **k: client)
    route = typesafe.text_model_route({SettingsKey.MEETING_LLM_PROVIDER: "ollama",
                                       SettingsKey.MEETING_LLM_MODEL: model})
    return text_judge.TextModelJudge("dummy", route=route)


def test_first_judgment_learns_the_server_once(local_server, monkeypatch):
    handler, chat = _ollama_with_chat('{"q": 0.9}')
    requests = local_server(handler)
    judge = _ollama_judge(monkeypatch, handler)
    assert judge.noul({"text": "hi"}, "Is it?") == 0.9
    assert judge.noul({"text": "hi"}, "Is it?") == 0.9
    assert model_spec(judge.profile, "qwen3").context_window == 16384
    assert [r.url.path for r in requests].count("/v1/models") == 1
    assert len(chat) == 2


@pytest.mark.parametrize("model,effort", [("qwen3", "none"), ("llama3.2", None)])
def test_judgments_ask_a_thinking_model_not_to_think(local_server, monkeypatch, model, effort):
    handler, chat = _ollama_with_chat('{"q": 0.9}')
    local_server(handler)
    assert _ollama_judge(monkeypatch, handler, model).noul({"text": "hi"}, "Is it?") == 0.9
    assert chat[0].get("reasoning_effort") == effort
    assert chat[0]["temperature"] == 0


@pytest.mark.parametrize("provider,model", [
    ("openrouter", "vendor/small-model"),
    ("groq", "llama-3.3-70b-versatile"),
    ("opencode_go", "glm-5.2"),
])
def test_thinking_off_adds_nothing_where_hosted_models_have_no_off_switch(provider, model):
    body = _chat_body(get_profile(provider, {}), model, temperature=0, reasoning_level="off")
    assert not {"reasoning_effort", "reasoning", "thinking"} & set(body)


def test_a_meeting_snapshot_records_what_the_server_reports(local_server):
    from services.text_llm import snapshot_from_profile

    local_server(_routes(OLLAMA))
    saved = snapshot_from_profile(get_profile("ollama", {}), "qwen3").model_metadata
    assert saved["context_window"] == 16384
    assert saved["thinking_levels"] == {"off": "none"}


def test_hosted_endpoints_are_never_asked_about_their_models(local_server):
    from services.text_llm import learn_server_models

    requests = local_server(lambda request: pytest.fail("hosted endpoint was probed"))
    learn_server_models(get_profile("groq", {}), "key")
    hosted = upsert_custom_profile({}, name="Gateway", base_url="https://gateway.test/v1")
    learn_server_models(hosted, "key")
    assert requests == []
