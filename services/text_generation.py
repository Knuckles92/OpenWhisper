"""Protocol adapters for cleanup and meeting text generation.

Assistant messages carry opaque provider data back into the next tool round.
This preserves signed thinking blocks and Responses reasoning items without
exposing either as transcript text or reconstructing them from plain text.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

from services.text_llm import NEW_PROFILE_IDS, provider_headers
from services.text_model_catalog import TextModelSpec, model_spec


@dataclass
class ToolCall:
    id: str
    function: Any


@dataclass
class TextResult:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    assistant_message: dict = field(default_factory=dict)
    usage: Any = None
    finish_reason: str = ""


class TextGenerationError(RuntimeError):
    pass


def _mapping(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    result = dump(exclude_none=True) if callable(dump) else {}
    return result if isinstance(result, dict) else {}


def _call(call_id: str, name: str, arguments: Any) -> ToolCall:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments)
    return ToolCall(call_id, SimpleNamespace(name=name, arguments=arguments))


def _usage(raw: dict, google: bool = False) -> Any:
    if not raw:
        return None
    incoming = raw.get("promptTokenCount" if google else "input_tokens", 0) or 0
    outgoing = raw.get("candidatesTokenCount" if google else "output_tokens", 0) or 0
    return SimpleNamespace(prompt_tokens=incoming, completion_tokens=outgoing,
                           total_tokens=incoming + outgoing)


def _assistant(text: str, calls: list[ToolCall], **opaque) -> dict:
    result = {"role": "assistant", "content": text}
    if calls:
        result["tool_calls"] = [
            {"id": c.id, "type": "function",
             "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in calls
        ]
    result.update(opaque)
    return result


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TextGenerationError("canceled")


def _chat(client, model, messages, options) -> TextResult:
    # Internal conversation metadata must never leak into another protocol.
    clean = [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]
    response = client.chat.completions.create(model=model, messages=clean, **options)
    choices = getattr(response, "choices", None) or []
    if not choices:
        return TextResult()
    choice = choices[0]
    reason = getattr(choice, "finish_reason", "")
    if isinstance(reason, str) and reason in ("length", "content_filter"):
        raise TextGenerationError(f"Model output was incomplete ({reason}).")
    message = choice.message
    refusal = getattr(message, "refusal", None)
    if isinstance(refusal, str) and refusal:
        raise TextGenerationError("Model refused the request.")
    content = message.content or ""
    if not isinstance(content, str):
        raise TextGenerationError("Model returned non-text content.")
    calls = [
        _call(c.id, c.function.name, c.function.arguments)
        for c in (getattr(message, "tool_calls", None) or [])
    ]
    assistant = _assistant(content, calls)
    raw = _mapping(message)
    # DeepSeek, GLM and some gateways require these on subsequent tool turns.
    for key in ("reasoning_content", "reasoning", "reasoning_details"):
        if key in raw:
            assistant[key] = raw[key]
    return TextResult(content, calls, assistant, getattr(response, "usage", None),
                      reason if isinstance(reason, str) else "")


def _responses(client, model, messages, tools, limit, json_mode, options) -> TextResult:
    items = []
    for message in messages:
        role = message["role"]
        if role == "assistant" and "_responses" in message:
            items.extend(message["_responses"])
        elif role == "tool":
            items.append({"type": "function_call_output",
                          "call_id": message["tool_call_id"], "output": message["content"]})
        else:
            items.append({"role": role, "content": message.get("content") or ""})
    body = dict(model=model, input=items, store=False, max_output_tokens=limit)
    if tools:
        body["tools"] = [{"type": "function", **t["function"]} for t in tools]
        body["tool_choice"] = "auto"
    if json_mode:
        body["text"] = {"format": {"type": "json_object"}}
    # Only OpenAI reasoning models expose this control through the gateway.
    if model.startswith("gpt-"):
        body["include"] = ["reasoning.encrypted_content"]
    raw = _mapping(client.responses.create(**body, **options))
    if raw.get("status") in ("incomplete", "failed", "cancelled") or raw.get("error"):
        raise TextGenerationError("Model output was incomplete or failed.")
    output = raw.get("output") or []
    texts, calls = [], []
    for item in output:
        if item.get("type") == "function_call":
            calls.append(_call(item["call_id"], item["name"], item["arguments"]))
        elif item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "refusal":
                    raise TextGenerationError("Model refused the request.")
                if part.get("type") == "output_text":
                    texts.append(part["text"])
    text = "".join(texts)
    return TextResult(text, calls, _assistant(text, calls, _responses=output),
                      _usage(raw.get("usage") or {}), raw.get("status", ""))


def _anthropic(client, model, messages, tools, limit, json_mode, options) -> TextResult:
    system, conversation = [], []
    for message in messages:
        role = message["role"]
        if role in ("system", "developer"):
            system.append(message.get("content") or "")
            continue
        if role == "assistant":
            content = message.get("_anthropic")
            if content is None:
                content = [{"type": "text", "text": message.get("content") or ""}]
        elif role == "tool":
            role = "user"
            content = [{"type": "tool_result", "tool_use_id": message["tool_call_id"],
                        "content": message["content"]}]
        else:
            content = [{"type": "text", "text": message.get("content") or ""}]
        if conversation and conversation[-1]["role"] == role:
            conversation[-1]["content"].extend(content)
        else:
            conversation.append({"role": role, "content": list(content)})
    body = dict(model=model, system="\n\n".join(system), messages=conversation,
                max_tokens=limit)
    if tools:
        body["tools"] = [
            {"name": t["function"]["name"], "description": t["function"].get("description", ""),
             "input_schema": t["function"]["parameters"]} for t in tools
        ]
        body["tool_choice"] = {"type": "auto"}
    # JSON fallback instructions live in the shared meeting prompt. Do not
    # require structured-output extensions unsupported by some Messages models.
    headers = {**options.pop("extra_headers", {}), "anthropic-version": "2023-06-01"}
    raw = client.post("/messages", cast_to=dict[str, Any], body=body,
                      options={**options, "headers": headers})
    reason = raw.get("stop_reason", "")
    if reason in ("max_tokens", "refusal", "pause_turn"):
        raise TextGenerationError(f"Model output was incomplete ({reason}).")
    content = raw.get("content") or []
    text = "".join(p.get("text", "") for p in content if p.get("type") == "text")
    calls = [_call(p["id"], p["name"], p["input"])
             for p in content if p.get("type") == "tool_use"]
    return TextResult(text, calls, _assistant(text, calls, _anthropic=content),
                      _usage(raw.get("usage") or {}), reason)


def _google(client, model, messages, tools, limit, json_mode, options) -> TextResult:
    system, contents, call_names = [], [], {}
    for message in messages:
        role = message["role"]
        if role in ("system", "developer"):
            system.append(message.get("content") or "")
            continue
        if role == "assistant":
            for call in message.get("tool_calls", []):
                call_names[call["id"]] = call["function"]["name"]
            role = "model"
            parts = message.get("_google") or [{"text": message.get("content") or ""}]
        elif role == "tool":
            role = "user"
            call_id = message["tool_call_id"]
            parts = [{"functionResponse": {
                "id": call_id, "name": call_names[call_id],
                "response": {"result": message["content"]},
            }}]
        else:
            parts = [{"text": message.get("content") or ""}]
        if contents and contents[-1]["role"] == role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": role, "parts": list(parts)})
    body = {"contents": contents, "generationConfig": {"maxOutputTokens": limit}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system)}]}
    if tools:
        body["tools"] = [{"functionDeclarations": [
            {"name": t["function"]["name"],
             "description": t["function"].get("description", ""),
             "parametersJsonSchema": t["function"]["parameters"]} for t in tools
        ]}]
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    headers = options.pop("extra_headers", {})
    raw = client.post(f"/models/{quote(model, safe='')}:generateContent",
                      cast_to=dict[str, Any], body=body, options={**options, "headers": headers})
    candidates = raw.get("candidates") or []
    if not candidates:
        raise TextGenerationError("Model returned no candidate (possibly blocked).")
    candidate = candidates[0]
    reason = candidate.get("finishReason", "")
    if reason not in ("STOP", ""):
        raise TextGenerationError(f"Model output was incomplete ({reason}).")
    parts = candidate.get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought", False))
    calls = []
    for index, part in enumerate(parts):
        call = part.get("functionCall")
        if call:
            calls.append(_call(call.get("id") or f"call_{index}",
                               call["name"], call.get("args", {})))
    return TextResult(text, calls, _assistant(text, calls, _google=parts),
                      _usage(raw.get("usageMetadata") or {}, google=True), reason)


def generate(client, profile, *, model: str, messages: list[dict],
             tools: list[dict] | None = None, cancel_event=None,
             session_id: str = "", reasoning_level: str = "", **options) -> TextResult:
    """Generate one turn; callers replay assistant_message unchanged for tools.

    The caller owns client lifetime and cancellation of in-flight HTTP work.
    Cancellation is checked again after receiving data, before any tool executes.
    """
    _check_cancel(cancel_event)
    spec = model_spec(profile, model) if profile is not None else TextModelSpec()
    if tools and not spec.tools:
        raise TextGenerationError("This model does not support tool calls.")
    if session_id and profile is not None and profile.kind in ("opencode_go", "opencode_zen"):
        options["extra_headers"] = provider_headers(profile, session_id)
    if profile is not None and profile.kind in NEW_PROFILE_IDS:
        options.pop("temperature", None)
        options.pop("reasoning_effort", None)
        options.pop("extra_body", None)
        level = (spec.thinking_levels or {}).get(reasoning_level, reasoning_level)
        if level and level != "off" and spec.reasoning_format == "openai":
            if spec.protocol == "responses":
                options["reasoning"] = {"effort": level}
            elif spec.protocol == "chat":
                options["reasoning_effort"] = level
        elif level and level != "off" and spec.reasoning_format == "deepseek":
            options["extra_body"] = {"thinking": {"type": "enabled"}}
            if level in ("high", "max"):
                options["extra_body"]["reasoning_effort"] = level
    if spec.protocol == "chat":
        if profile is not None and profile.kind in NEW_PROFILE_IDS:
            options.setdefault("max_tokens", spec.max_output_tokens)
        if tools:
            options["tools"] = tools
        result = _chat(client, model, messages, options)
    else:
        limit = options.pop("max_tokens", spec.max_output_tokens)
        json_mode = options.pop("response_format", None) is not None
        options.pop("tool_choice", None)
        options.pop("temperature", None)
        options.pop("reasoning_effort", None)
        options.pop("extra_body", None)
        adapter = {"responses": _responses, "anthropic": _anthropic, "google": _google}[spec.protocol]
        result = adapter(client, model, messages, tools, limit, json_mode, options)
    _check_cancel(cancel_event)
    return result
