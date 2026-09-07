"""Exercise real SDK serialization against in-memory HTTP providers."""
import json
import threading
from unittest.mock import MagicMock, patch

import httpx
import pytest
from openai import OpenAI

from services.text_generation import TextGenerationError, generate
from services.text_llm import (
    get_profile,
    list_profiles,
    provider_headers,
    save_ollama_url,
    snapshot_from_mapping,
    snapshot_from_profile,
)
from services.text_model_catalog import model_spec
from services.transcript_cleanup import TranscriptCleanup

CASES = [
    ("ollama", "llama3.2", "chat", "/v1/chat/completions"),
    ("groq", "llama-3.3-70b-versatile", "chat", "/openai/v1/chat/completions"),
    ("opencode_go", "glm-5.2", "chat", "/zen/go/v1/chat/completions"),
    ("opencode_go", "gpt-5.6-luna", "responses", "/zen/go/v1/responses"),
    ("opencode_go", "minimax-m3", "anthropic", "/zen/go/v1/messages"),
    ("opencode_zen", "glm-5.2", "chat", "/zen/v1/chat/completions"),
    ("opencode_zen", "gpt-5.6-luna", "responses", "/zen/v1/responses"),
    ("opencode_zen", "claude-sonnet-4-6", "anthropic", "/zen/v1/messages"),
    ("opencode_zen", "gemini-3.1-pro", "google", "/zen/v1/models/gemini-3.1-pro:generateContent"),
]
TOOL = {"type": "function", "function": {
    "name": "search_context_files", "description": "Find evidence",
    "parameters": {"type": "object", "properties": {"query": {"type": "string"}},
                   "required": ["query"]},
}}


def reply(protocol, tool=False):
    if protocol == "chat":
        message = {"role": "assistant", "content": "Clean text." if not tool else "",
                   "reasoning_content": "private reasoning"}
        if tool:
            message["tool_calls"] = [{"id": "call_1", "type": "function", "function": {
                "name": "search_context_files", "arguments": '{"query":"budget"}'}}]
        return {"choices": [{"message": message, "finish_reason": "tool_calls" if tool else "stop"}]}
    if protocol == "responses":
        output = [{"type": "reasoning", "id": "rs_1", "summary": [],
                   "encrypted_content": "signed-reasoning"}]
        output += [{"type": "function_call", "id": "fc_1", "call_id": "call_1",
                    "name": "search_context_files", "arguments": '{"query":"budget"}'}] if tool else [
            {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": "Clean text.", "annotations": []}]}]
        return {"id": "resp_1", "status": "completed", "output": output}
    if protocol == "anthropic":
        content = [{"type": "thinking", "thinking": "private reasoning", "signature": "signed-reasoning"}]
        content += [{"type": "tool_use", "id": "call_1", "name": "search_context_files",
                     "input": {"query": "budget"}}] if tool else [{"type": "text", "text": "Clean text."}]
        return {"content": content, "stop_reason": "tool_use" if tool else "end_turn"}
    parts = [{"text": "private reasoning", "thought": True, "thoughtSignature": "signed-reasoning"}]
    parts += [{"functionCall": {"id": "call_1", "name": "search_context_files",
                               "args": {"query": "budget"}}, "thoughtSignature": "tool-signature"}] if tool else [
        {"text": "Clean text."}]
    return {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}]}


def client_for(provider, responses, requests):
    profile = get_profile(provider, {})
    def handler(request):
        requests.append(request)
        value = responses.pop(0)
        if callable(value):
            value = value()
        return httpx.Response(200, json=value)
    client = OpenAI(api_key="test-secret", base_url=profile.base_url or "https://api.openai.com/v1",
                    max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return profile, client


@pytest.mark.parametrize("provider,model,protocol,path", CASES)
def test_wire_protocol_and_signed_tool_roundtrip(provider, model, protocol, path):
    requests = []
    profile, client = client_for(provider, [reply(protocol, True), reply(protocol)], requests)
    messages = [{"role": "system", "content": "Use evidence."}, {"role": "user", "content": "Budget?"}]
    with client:
        first = generate(client, profile, model=model, messages=messages, tools=[TOOL], session_id="meeting-1")
        assert first.tool_calls[0].id == "call_1"
        messages += [first.assistant_message, {"role": "tool", "tool_call_id": "call_1", "content": '{"hits":[]}'}]
        final = generate(client, profile, model=model, messages=messages, tools=[TOOL], session_id="meeting-1")
    assert final.text == "Clean text."
    for request in requests:
        assert request.url.path == path
        assert request.headers["authorization"] == "Bearer test-secret"
        if provider == "opencode_go":
            assert request.headers["x-opencode-session"] == "meeting-1"
            assert request.headers["user-agent"] == "OpenWhisper"
        else:
            assert "x-opencode-session" not in request.headers
    body = json.loads(requests[1].content)
    encoded = json.dumps(body)
    assert "call_1" in encoded
    assert "hits" in encoded
    assert "_google" not in encoded and "_anthropic" not in encoded and "_responses" not in encoded
    if protocol == "chat":
        assert body["messages"][2]["reasoning_content"] == "private reasoning"
    elif protocol == "responses":
        assert body["input"][2]["encrypted_content"] == "signed-reasoning"
        assert body["input"][-1]["type"] == "function_call_output"
    elif protocol == "anthropic":
        assert body["messages"][1]["content"][0]["signature"] == "signed-reasoning"
        assert body["messages"][-1]["content"][0]["tool_use_id"] == "call_1"
    else:
        assert body["contents"][1]["parts"][1]["thoughtSignature"] == "tool-signature"
        assert body["contents"][-1]["parts"][0]["functionResponse"]["name"] == "search_context_files"


@pytest.mark.parametrize("provider,model,protocol,path", CASES)
def test_cleanup_preserves_raw_on_truncation(provider, model, protocol, path):
    raw = reply(protocol)
    if protocol == "chat":
        raw["choices"][0]["finish_reason"] = "length"
    elif protocol == "responses":
        raw["status"] = "incomplete"
    elif protocol == "anthropic":
        raw["stop_reason"] = "max_tokens"
    else:
        raw["candidates"][0]["finishReason"] = "MAX_TOKENS"
    profile, client = client_for(provider, [raw], [])
    with patch("services.transcript_cleanup.create_openai_client", return_value=client):
        cleanup = TranscriptCleanup(provider, model, api_key="test-secret")
        assert cleanup.cleanup("raw transcript") == "raw transcript"
        assert cleanup.last_error
    client.close()


@pytest.mark.parametrize("provider,model,protocol,path", CASES)
def test_canceled_response_cannot_publish_tools(provider, model, protocol, path):
    canceled = threading.Event()
    def response():
        canceled.set()
        return reply(protocol, True)
    profile, client = client_for(provider, [response], [])
    with client, pytest.raises(TextGenerationError, match="canceled"):
        generate(client, profile, model=model, messages=[{"role": "user", "content": "Budget"}],
                 tools=[TOOL], cancel_event=canceled)


def test_snapshot_retains_ollama_host_and_protocol():
    profile = get_profile("ollama", {"ollama_base_url": "http://host-a:11434/v1"})
    snapshot = snapshot_from_profile(profile, "llama3.2")
    restored = snapshot_from_mapping(snapshot.to_dict()).to_profile()
    assert restored.base_url == "http://host-a:11434/v1"
    assert model_spec(restored, "llama3.2").protocol == "chat"
    assert "api_key" not in snapshot.to_dict()


def test_unknown_opencode_route_is_not_guessed():
    for provider in ("opencode_go", "opencode_zen"):
        with pytest.raises(ValueError, match="no supported API route"):
            model_spec(get_profile(provider, {}), "future-model")


def test_same_model_has_distinct_go_and_zen_protocols():
    assert model_spec(get_profile("opencode_go", {}), "minimax-m3").protocol == "anthropic"
    assert model_spec(get_profile("opencode_zen", {}), "minimax-m3").protocol == "chat"


def test_ollama_url_normalizes_and_never_stores_embedded_secrets():
    with patch("services.settings.settings_manager.save_setting") as save:
        save_ollama_url("http://localhost:11434/")
        save.assert_called_once_with("ollama_base_url", "http://localhost:11434/v1")
        for url in ("http://user:secret@localhost:11434", "http://localhost/?key=secret"):
            with pytest.raises(ValueError):
                save_ollama_url(url)
        assert save.call_count == 1


def test_empty_new_provider_model_does_not_reuse_previous_model():
    with patch("services.transcript_cleanup.create_openai_client"):
        cleaner = TranscriptCleanup("openai", "gpt-test", api_key="test")
        cleaner.configure("ollama", "")
        assert cleaner.model == ""
        assert not cleaner.is_available()


def test_opencode_credentials_and_headers_are_separate():
    profiles = {p.id: p for p in list_profiles({})}
    assert profiles["opencode_go"].api_key_env != profiles["opencode_zen"].api_key_env
    assert "x-opencode-session" not in (provider_headers(profiles["groq"], "meeting") or {})
    assert not profiles["ollama"].requires_api_key


def test_responses_reasoning_preference_is_translated():
    requests = []
    profile, client = client_for("opencode_go", [reply("responses")], requests)
    with client:
        generate(client, profile, model="gpt-5.6-luna", messages=[{"role": "user", "content": "Hi"}],
                 reasoning_level="high")
    assert json.loads(requests[0].content)["reasoning"] == {"effort": "high"}


@pytest.mark.parametrize("provider,model,protocol,path", CASES)
def test_direct_agent_recall_uses_protocol_tool_result(provider, model, protocol, path):
    from meeting.agent.openrouter_direct import DirectOpenRouterAgent
    from meeting.interfaces import AgentConfig
    profile, client = client_for(provider, [reply(protocol, True), reply(protocol)], [])
    agent = DirectOpenRouterAgent()
    agent._profile = profile
    agent._cfg = AgentConfig(meeting_id="meeting-1", provider=provider, model=model, api_key="test-secret", system_prompt="Use evidence.")
    agent._model = model
    agent._tools = MagicMock()
    agent._tools.search_context_files.return_value = {"hits": [], "text": "No matching evidence."}
    with client:
        result = agent._run_tool_mode(client, "Use evidence.", "Budget?", 10.0)
    assert result.ok
    agent._tools.search_context_files.assert_called_once_with(query="budget", relative_path=None, limit=10)


@pytest.mark.parametrize("provider,model,protocol,path", CASES)
def test_pi_endpoint_metadata_and_old_bundle_detection(provider, model, protocol, path):
    from meeting.agent.pi_sidecar import PiSidecarAgent
    from meeting.interfaces import AgentConfig
    profile = get_profile(provider, {})
    agent = PiSidecarAgent(payload_dir="unused-test-payload")
    agent._cfg = AgentConfig(meeting_id="meeting-1", provider=provider, model=model, api_key=None, system_prompt="Use evidence.",
                             endpoint=snapshot_from_profile(profile, model).to_dict())
    fields = agent._endpoint_fields()
    assert fields["model_metadata"]["protocol"] == protocol
    assert "test-secret" not in json.dumps(fields)
    if provider == "opencode_go":
        assert fields["headers"]["x-opencode-session"] == "meeting-1"
    with pytest.raises(RuntimeError, match="Update the Pi"):
        agent._check_sidecar_text_support()
    agent._text_protocols = ["chat", "responses", "anthropic", "google"]
    agent._check_sidecar_text_support()


def test_ollama_catalog_filters_embeddings_and_snapshots_capabilities():
    from services.text_llm import list_chat_models
    profile = get_profile("ollama", {})
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "test-embedding"}, {"id": "test-completion"}]})
        assert request.url.path == "/api/show"
        model = json.loads(request.content)["model"]
        return httpx.Response(200, json={"capabilities": ["embedding"] if "embedding" in model else ["completion"],
                                        "parameters": "num_ctx 8192"})
    client = OpenAI(api_key="dummy", base_url=profile.base_url,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    with patch("services.text_llm.create_openai_client", return_value=client):
        assert list_chat_models(profile) == ["test-completion"]
    saved = snapshot_from_profile(profile, "test-completion")
    assert saved.model_metadata["tools"] is False
    assert saved.model_metadata["context_window"] == 8192
    assert len(requests) == 3


@pytest.mark.parametrize("status", [401, 403, 429, 404, 503])
def test_provider_http_failures_preserve_raw(status):
    profile = get_profile("groq", {})
    client = OpenAI(api_key="synthetic", base_url=profile.base_url, max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(
                        lambda request: httpx.Response(status, json={"error": {"message": "Synthetic failure"}}))))
    with patch("services.transcript_cleanup.create_openai_client", return_value=client):
        cleaner = TranscriptCleanup("groq", "llama-3.3-70b-versatile", api_key="synthetic")
        assert cleaner.cleanup("Keep this transcript") == "Keep this transcript"
        assert cleaner.last_error
    client.close()


@pytest.mark.parametrize("provider,model,protocol,path", CASES)
def test_no_tool_support_uses_validated_json_and_rejects_prose(provider, model, protocol, path):
    from meeting.agent.openrouter_direct import DirectOpenRouterAgent
    requests = []
    profile, client = client_for(provider, [reply(protocol) for _ in range(4)], requests)
    agent = DirectOpenRouterAgent()
    agent._profile = profile
    agent._model = model
    agent._tools = MagicMock()
    with client:
        agent._probe_tool_support(client)
        assert agent._json_mode
        result = agent._run_json_mode(client, "Preserve evidence.", "Budget?", 10)
    assert not result.ok
    assert "invalid JSON" in result.error
    agent._tools.apply_agent_ops.assert_not_called()
    assert len(requests) == 4
    if protocol != "chat":
        assert "signed-reasoning" in requests[-1].content.decode()
