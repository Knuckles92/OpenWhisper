"""Direct OpenRouter/OpenAI agent core: in-process, single-call checkpoints.

Implements the ``AgentCore`` protocol with one chat-completions call per
checkpoint using function tools that mirror the state-patch vocabulary
(``patch_state``, ``ask_question``, ``resolve_question``). Tool results are
fed back for one extra round-trip so the model can self-correct rejections.
Providers/models without tool support fall back to a JSON-object mode with a
single repair retry.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Dict, List, Optional

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover - openai is an app dependency
    OpenAI = None  # type: ignore[assignment]

from config import config
from meeting.agent.base import find_provider_api_key
from meeting.agent.prompts import JSON_FALLBACK_INSTRUCTIONS, build_checkpoint_user_prompt
from meeting.interfaces import (
    AgentConfig,
    AgentResult,
    AgentToolHost,
    CheckpointPayload,
    OpResult,
)
from meeting.state.schema import CARD_KEYS

logger = logging.getLogger(__name__)

_CHECKPOINT_TIMEOUT_S = 60.0
_CONSOLIDATION_TIMEOUT_S = 120.0
_PROBE_TIMEOUT_S = 10.0
#: Consolidation often needs an extra round to add timeline/questions after
#: the first patch_state batch; rolling checkpoints stay at two rounds.
_MAX_TOOL_ROUNDS = 2
_MAX_CONSOLIDATION_TOOL_ROUNDS = 3
#: Low temperature keeps dashboard ops stable across checkpoints; consolidation
#: especially benefits from less paraphrase drift between runs.
_TEMPERATURE = 0.0

_DEFAULT_BASE_URLS = {"openrouter": "https://openrouter.ai/api/v1"}
_DEFAULT_MODELS = {
    "openrouter": config.MEETING_LLM_MODEL,
    "openai": "gpt-4o-mini",
}
# OpenRouter attributes traffic to the app via this optional header.
_OPENROUTER_HEADERS = {"X-Title": "OpenWhisper"}

#: Ops the model may put inside patch_state. ask_question/resolve_question
#: have dedicated tools, so they are steered out of this enum (the tool host
#: would accept them regardless — they are part of the agent op vocabulary).
_PATCH_STATE_OPS = (
    "add_item", "update_item", "remove_item",
    "set_topic", "set_rolling_summary",
    "upsert_participant", "suggest_participant_name",
    "revise_segment_text",
)
_POLISH_ONLY_OPS = frozenset({"revise_segment_text"})
_AGENT_CARDS = [key for key in CARD_KEYS if key != "user_notes"]

_EVIDENCE_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Supporting transcript segment ids (sg_...), copied exactly.",
    "minItems": 1,
}

_PATCH_STATE_TOOL = {
    "type": "function",
    "function": {
        "name": "patch_state",
        "description": (
            "Apply one or more state-patch operations to the meeting "
            "dashboard. Each op is validated independently; rejected ops "
            "return a reason."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {"type": "string", "enum": list(_PATCH_STATE_OPS)},
                            "card": {
                                "type": "string",
                                "enum": _AGENT_CARDS,
                                "description": "Card key (add_item).",
                            },
                            "id": {
                                "type": "string",
                                "description": (
                                    "Target item id (update_item/remove_item) "
                                    "or participant id (upsert_participant rename)."
                                ),
                            },
                            "base_revision": {
                                "type": "integer",
                                "description": (
                                    "The item's current revision as shown in "
                                    "the state (update_item/remove_item)."
                                ),
                            },
                            "text": {
                                "type": "string",
                                "description": "Item/topic/summary text.",
                            },
                            "set": {
                                "type": "object",
                                "description": (
                                    "Fields to change (update_item): 'text' "
                                    "and/or 'data'."
                                ),
                            },
                            "data": {
                                "type": "object",
                                "description": (
                                    "Structured item data (add_item). For "
                                    "timeline items, REQUIRED: "
                                    "{\"start_s\": <meeting seconds from the "
                                    "segment t=…s stamp>}. Also used for "
                                    "action_items.owner_participant_id and "
                                    "risks.severity."
                                ),
                            },
                            "display_name": {"type": "string"},
                            "participant_id": {"type": "string"},
                            "segment_id": {
                                "type": "string",
                                "description": (
                                    "Transcript segment id (revise_segment_text)."
                                ),
                            },
                            "kind": {"type": "string", "enum": ["others_cluster"]},
                            "evidence": _EVIDENCE_SCHEMA,
                        },
                        "required": ["op", "evidence"],
                    },
                },
            },
            "required": ["ops"],
        },
    },
}

_ASK_QUESTION_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_question",
        "description": (
            "Add a question to the quiet inbox. Use sparingly; only "
            "decision-relevant, thought-provoking questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The question."},
                "evidence": _EVIDENCE_SCHEMA,
            },
            "required": ["text", "evidence"],
        },
    },
}

_RESOLVE_QUESTION_TOOL = {
    "type": "function",
    "function": {
        "name": "resolve_question",
        "description": (
            "Answer an open inbox question from meeting audio. Confidence >= "
            "0.8 resolves it; 0.4-0.8 stores a greyed suggestion; lower is "
            "rejected. Report confidence honestly."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question_id": {"type": "string"},
                "answer_text": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "evidence": _EVIDENCE_SCHEMA,
            },
            "required": ["question_id", "answer_text", "confidence", "evidence"],
        },
    },
}

_TOOLS = [_PATCH_STATE_TOOL, _ASK_QUESTION_TOOL, _RESOLVE_QUESTION_TOOL]

_NOOP_TOOL = {
    "type": "function",
    "function": {
        "name": "noop",
        "description": "No-op capability probe. Call with no arguments.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _op_results_payload(results: List[OpResult]) -> Dict[str, Any]:
    """Serialize op results for a tool-result message."""
    return {
        "results": [
            {
                "ok": r.ok,
                "reason": r.reason,
                "target_id": r.target_id,
                "seq": r.seq,
                "current_revision": r.current_revision,
            }
            for r in results
        ]
    }


class DirectOpenRouterAgent:
    """``AgentCore`` implementation calling OpenRouter/OpenAI directly."""

    def __init__(self) -> None:
        self._cfg: Optional[AgentConfig] = None
        self._tools: Optional[AgentToolHost] = None
        self._client: Optional[Any] = None
        self._client_lock = threading.Lock()
        self._api_key: Optional[str] = None
        self._base_url: Optional[str] = None
        self._headers: Optional[Dict[str, str]] = None
        self._model: str = ""
        self._json_mode = False
        self._polish_mode = False
        self._fatal = False
        self._shut_down = False
        self._cancel_event = threading.Event()

    # ------------------------------------------------------------------
    # AgentCore lifecycle
    # ------------------------------------------------------------------

    def initialize(self, cfg: AgentConfig, tools: AgentToolHost) -> None:
        """Prepare the core: resolve credentials, build the client, probe tools.

        Args:
            cfg: Meeting-scoped agent configuration (provider, model, key).
            tools: The tool host (``MeetingEngine``) that applies ops.
        """
        self._cfg = cfg
        self._tools = tools
        self._fatal = False
        self._shut_down = False
        self._api_key = cfg.api_key or find_provider_api_key(cfg.provider)
        self._base_url = self._provider_base_url(cfg.provider)
        self._headers = (
            dict(_OPENROUTER_HEADERS) if cfg.provider == "openrouter" else None
        )
        self._model = self._resolve_model(cfg)

        if OpenAI is None:
            logger.error("openai package unavailable; direct agent core offline")
            self._fatal = True
            return
        if not self._api_key:
            logger.warning(
                "No %s API key found; direct agent core offline", cfg.provider,
            )
            return

        client = self._ensure_client()
        if client is not None:
            self._probe_tool_support(client)
        logger.info(
            "Direct agent core initialized (provider=%s model=%s mode=%s)",
            cfg.provider, self._model,
            "json" if self._json_mode else "tools",
        )

    def checkpoint(self, payload: CheckpointPayload) -> AgentResult:
        """Run one rolling checkpoint. Blocking; called from a worker thread."""
        timeout = (
            _CONSOLIDATION_TIMEOUT_S if payload.is_consolidation
            else _CHECKPOINT_TIMEOUT_S
        )
        return self._run_pass(payload, timeout)

    def consolidate(self, payload: CheckpointPayload) -> AgentResult:
        """Run the end-of-meeting full pass. Blocking."""
        return self._run_pass(payload, _CONSOLIDATION_TIMEOUT_S)

    def cancel(self) -> None:
        """Cancel the in-flight request: flag rounds and drop the client."""
        self._cancel_event.set()
        with self._client_lock:
            client = self._client
            self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug("Client close during cancel failed", exc_info=True)

    def is_healthy(self) -> bool:
        """True when the core can accept checkpoints."""
        return (
            not self._shut_down
            and not self._fatal
            and OpenAI is not None
            and self._api_key is not None
        )

    def shutdown(self) -> None:
        """Release the client."""
        self._shut_down = True
        self.cancel()

    # ------------------------------------------------------------------
    # Client plumbing
    # ------------------------------------------------------------------

    @staticmethod
    def _provider_base_url(provider: str) -> Optional[str]:
        """Base URL for a provider (None = OpenAI default)."""
        if provider == "openrouter":
            try:
                from config import config

                return config.OPENROUTER_BASE_URL
            except Exception:
                return _DEFAULT_BASE_URLS["openrouter"]
        return None

    @staticmethod
    def _resolve_model(cfg: AgentConfig) -> str:
        """The configured model, falling back to the cleanup default."""
        if cfg.model:
            return cfg.model
        try:
            from services.settings import default_transcript_cleanup_model

            fallback = default_transcript_cleanup_model(cfg.provider)
            if fallback:
                return fallback
        except Exception:
            pass
        return _DEFAULT_MODELS.get(cfg.provider, _DEFAULT_MODELS["openai"])

    def _ensure_client(self) -> Optional[Any]:
        """Build (or rebuild after cancel) the chat client."""
        with self._client_lock:
            if self._client is not None:
                return self._client
            if OpenAI is None or not self._api_key or self._shut_down:
                return None
            try:
                self._client = OpenAI(
                    api_key=self._api_key,
                    base_url=self._base_url,
                    default_headers=self._headers,
                    timeout=_CHECKPOINT_TIMEOUT_S,
                )
            except Exception:
                logger.exception("Failed to build agent LLM client")
                self._client = None
            return self._client

    def _probe_tool_support(self, client: Any) -> None:
        """One tool-call request; failure selects the JSON-mode fallback."""
        try:
            client.with_options(timeout=_PROBE_TIMEOUT_S).chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": "Call the noop tool."}],
                tools=[_NOOP_TOOL],
                tool_choice="auto",
                max_tokens=16,
            )
            self._json_mode = False
        except Exception as exc:
            logger.warning(
                "Tool-call capability probe failed (%s); using JSON-mode "
                "fallback", exc,
            )
            self._json_mode = True

    def _note_error(self, exc: Exception) -> None:
        """Classify a request failure: auth errors are fatal for the session."""
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            self._fatal = True
            logger.error(
                "Agent LLM authentication failure (%s); direct agent core "
                "offline", status,
            )
        elif status == 400 and not self._json_mode and "tool" in str(exc).lower():
            logger.warning(
                "Provider rejected tool calling; switching to JSON-mode "
                "fallback"
            )
            self._json_mode = True

    @staticmethod
    def _merge_usage(total: Dict[str, Any], usage: Any) -> None:
        if usage is None:
            return
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, key, None)
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
        total["requests"] = total.get("requests", 0) + 1

    # ------------------------------------------------------------------
    # Checkpoint execution
    # ------------------------------------------------------------------

    def _run_pass(self, payload: CheckpointPayload, timeout_s: float) -> AgentResult:
        if not self.is_healthy():
            return AgentResult(ok=False, error="agent_unavailable")
        if self._tools is None or self._cfg is None:
            return AgentResult(ok=False, error="not_initialized")
        self._cancel_event.clear()
        client = self._ensure_client()
        if client is None:
            return AgentResult(ok=False, error="client_unavailable")

        system_prompt = self._cfg.system_prompt or ""
        user_prompt = build_checkpoint_user_prompt(
            payload.state_snapshot,
            payload.new_segments,
            payload.is_consolidation,
            is_polish=bool(getattr(payload, "is_polish", False)),
        )
        max_rounds = (
            _MAX_CONSOLIDATION_TOOL_ROUNDS if payload.is_consolidation
            else _MAX_TOOL_ROUNDS
        )
        self._polish_mode = bool(getattr(payload, "is_polish", False))
        try:
            if self._json_mode:
                return self._run_json_mode(
                    client, system_prompt, user_prompt, timeout_s
                )
            return self._run_tool_mode(
                client, system_prompt, user_prompt, timeout_s, max_rounds=max_rounds,
            )
        finally:
            self._polish_mode = False

    def _dispatch_tool_call(self, name: str, args: Dict[str, Any]) -> List[OpResult]:
        """Route one model tool call to the tool host."""
        tools = self._tools
        assert tools is not None
        if name == "patch_state":
            ops = args.get("ops")
            if not isinstance(ops, list):
                raise ValueError("'ops' must be a list of op objects")
            if self._polish_mode:
                ops = [
                    op for op in ops
                    if isinstance(op, dict) and op.get("op") in _POLISH_ONLY_OPS
                ]
            return tools.apply_agent_ops(ops)
        if self._polish_mode and name in ("ask_question", "resolve_question"):
            return []
        if name == "ask_question":
            return [tools.ask_question(
                str(args.get("text") or ""),
                list(args.get("evidence") or []),
            )]
        if name == "resolve_question":
            return [tools.resolve_question(
                str(args.get("question_id") or ""),
                str(args.get("answer_text") or ""),
                float(args.get("confidence") or 0.0),
                list(args.get("evidence") or []),
            )]
        raise ValueError(f"unknown tool: {name}")

    def _run_tool_mode(self, client: Any, system_prompt: str, user_prompt: str,
                       timeout_s: float, max_rounds: int = _MAX_TOOL_ROUNDS,
                       ) -> AgentResult:
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        op_results: List[OpResult] = []
        usage: Dict[str, Any] = {}

        for round_index in range(max_rounds):
            if self._cancel_event.is_set():
                return AgentResult(
                    ok=False, op_results=op_results, error="canceled", usage=usage,
                )
            try:
                response = client.with_options(
                    timeout=timeout_s
                ).chat.completions.create(
                    model=self._model,
                    messages=messages,
                    tools=_TOOLS,
                    tool_choice="auto",
                    temperature=_TEMPERATURE,
                )
            except Exception as exc:
                self._note_error(exc)
                error = "canceled" if self._cancel_event.is_set() else str(exc)
                return AgentResult(
                    ok=False, op_results=op_results, error=error, usage=usage,
                )
            self._merge_usage(usage, getattr(response, "usage", None))

            choices = getattr(response, "choices", None) or []
            if not choices:
                return AgentResult(
                    ok=False, op_results=op_results,
                    error="empty response from model", usage=usage,
                )
            message = choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                break

            messages.append({
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in tool_calls
                ],
            })
            for call in tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    results = self._dispatch_tool_call(call.function.name, args)
                    op_results.extend(results)
                    content = json.dumps(_op_results_payload(results))
                except Exception as exc:
                    logger.warning(
                        "Tool call %s failed: %s", call.function.name, exc,
                    )
                    content = json.dumps({"error": str(exc)})
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": content,
                })
            # Round 2 feeds the results back so the model can self-correct
            # rejections; after the final round no further call is made.

        return AgentResult(ok=True, op_results=op_results, usage=usage)

    def _run_json_mode(self, client: Any, system_prompt: str, user_prompt: str,
                       timeout_s: float) -> AgentResult:
        """Fallback for models without tool support: one ops-JSON object."""
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"{user_prompt}\n\n{JSON_FALLBACK_INSTRUCTIONS}",
            },
        ]
        usage: Dict[str, Any] = {}

        for attempt in range(2):
            if self._cancel_event.is_set():
                return AgentResult(ok=False, error="canceled", usage=usage)
            try:
                response = client.with_options(
                    timeout=timeout_s
                ).chat.completions.create(
                    model=self._model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=_TEMPERATURE,
                )
            except Exception as exc:
                self._note_error(exc)
                error = "canceled" if self._cancel_event.is_set() else str(exc)
                return AgentResult(ok=False, error=error, usage=usage)
            self._merge_usage(usage, getattr(response, "usage", None))

            choices = getattr(response, "choices", None) or []
            content = (choices[0].message.content or "") if choices else ""
            try:
                data = json.loads(content)
                if not isinstance(data, dict):
                    raise ValueError("response is not a JSON object")
                ops = data.get("ops")
                if not isinstance(ops, list):
                    raise ValueError('missing "ops" list')
            except ValueError as exc:  # includes json.JSONDecodeError
                if attempt == 0:
                    # One repair retry with the parse error in context.
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Your previous response was invalid ({exc}). "
                            'Respond ONLY with a valid JSON object of the '
                            'form {"ops": [...]}.'
                        ),
                    })
                    continue
                return AgentResult(
                    ok=False, error=f"invalid JSON from model: {exc}",
                    usage=usage,
                )

            assert self._tools is not None
            if self._polish_mode:
                ops = [
                    op for op in ops
                    if isinstance(op, dict) and op.get("op") in _POLISH_ONLY_OPS
                ]
            op_results = self._tools.apply_agent_ops(ops) if ops else []
            return AgentResult(ok=True, op_results=op_results, usage=usage)

        return AgentResult(ok=False, error="json_mode_exhausted", usage=usage)
