"""Retired direct API agent, retained for historical regression tests.

The application factory and public agent exports never select this core.
Meeting intelligence uses Pi, OpenCode SDK, or an installed coding agent.

Implements the ``AgentCore`` protocol with one chat-completions call per
checkpoint using function tools that mirror the state-patch vocabulary
(``patch_state``, ``ask_question``, ``resolve_question``). Tool results are
fed back for one extra round-trip so the model can self-correct rejections.
Providers/models without tool support fall back to a JSON-object mode with a
single repair retry.
"""
from __future__ import annotations

import json
import time
import logging
import threading
from typing import Any, Dict, List, Optional

from config import config
from meeting.agent.base import merge_usage
from meeting.agent.prompts import (
    JSON_FALLBACK_INSTRUCTIONS,
    build_checkpoint_user_prompt,
    build_note_taker_system_prompt,
    build_notes_user_prompt,
)
from meeting.agent.tool_policy import (
    PASS_REJECTIONS,
    ToolScope,
    apply_patch_ops,
    op_results_payload,
    run_tool,
    tool_result_text,
)
from meeting.context_folder import context_folder_enabled
from meeting.finalization import POLISH_TIMEOUT_S
from meeting.interfaces import (
    AgentConfig,
    AgentResult,
    AgentToolHost,
    CheckpointPayload,
    OpResult,
)
from meeting.recall import past_recall_enabled
from meeting.agent.tool_specs import MEETING_TOOLS as _TOOLS, NOOP_TOOL as _NOOP_TOOL

from services.text_generation import TextGenerationError, generate
from services.text_llm import (
    profile_from_agent_config,
    provider_headers,
    resolve_api_key,
)
from services.text_model_catalog import model_spec

logger = logging.getLogger(__name__)

# The openai SDK loads with the first agent that needs it rather than with
# this module, which the dashboard and re-insight paths import: 0.7 s that a
# meeting without a direct agent never needs. Tests replace this.
OpenAI = None


def _openai_class():
    """The SDK client class, imported on first use; None if it is missing."""
    global OpenAI
    if OpenAI is None:
        try:
            from openai import OpenAI as client_class
        except ImportError:  # pragma: no cover - openai is an app dependency
            return None
        OpenAI = client_class
    return OpenAI


_CHECKPOINT_TIMEOUT_S = 60.0
_CONSOLIDATION_TIMEOUT_S = 300.0
_PROBE_TIMEOUT_S = 10.0
#: Consolidation often needs an extra round to add timeline/questions after
#: the first patch_state batch; rolling checkpoints stay at two rounds.
_MAX_TOOL_ROUNDS = 2
_MAX_CONSOLIDATION_TOOL_ROUNDS = 3
#: Recall spends a round searching before the model can write; give it one
#: extra hop when past-meeting recall or knowledge-folder search is enabled.
_MAX_TOOL_ROUNDS_WITH_RECALL = 3
#: Low temperature keeps dashboard ops stable across checkpoints; consolidation
#: especially benefits from less paraphrase drift between runs.
_TEMPERATURE = 0.0
#: JSON mode makes one request plus up to two repair rounds.
_JSON_MODE_ATTEMPTS = 3
#: Rejections a JSON-mode repair round cannot fix: a person owns the item, or
#: the agent may no longer write.
_UNREPAIRABLE_REJECTIONS = frozenset({
    "human_edited", "human_named", "agent_writes_revoked",
})
#: Poll interval while waiting on the network thread.
_GENERATION_POLL_S = 0.05

_CANCELED = "canceled"
_DEADLINE_EXCEEDED = "meeting text deadline exceeded"

_DEFAULT_MODELS = {
    "openrouter": config.MEETING_LLM_MODEL,
    "openai": "gpt-4o-mini",
}

class DirectOpenRouterAgent:
    """``AgentCore`` implementation calling OpenRouter/OpenAI directly."""

    def __init__(self) -> None:
        self._cfg: Optional[AgentConfig] = None
        self._tools: Optional[AgentToolHost] = None
        self._client: Optional[Any] = None
        self._client_lock = threading.Lock()
        self._generation_lock = threading.Lock()
        self._generation_thread: Optional[threading.Thread] = None
        self._api_key: Optional[str] = None
        self._base_url: Optional[str] = None
        self._headers: Optional[Dict[str, str]] = None
        self._model: str = ""
        self._json_mode = False
        self._use_json_response_format = True
        self._profile = None
        self._fatal = False
        self._shut_down = False
        self._cancel_event = threading.Event()

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
        profile = profile_from_agent_config(cfg.provider, cfg.endpoint)
        self._profile = profile
        self._api_key = cfg.api_key or resolve_api_key(profile)
        self._base_url = profile.base_url
        self._headers = provider_headers(profile, cfg.meeting_id)
        self._use_json_response_format = True
        self._model = self._resolve_model(cfg)

        if _openai_class() is None:
            logger.error("openai package unavailable; direct agent core offline")
            self._fatal = True
            return
        if not self._api_key:
            logger.warning(
                "No %s API key found; direct agent core offline", cfg.provider,
            )
            return

        model_spec(profile, self._model)
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
            else POLISH_TIMEOUT_S if payload.is_polish
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
            and _openai_class() is not None
            and self._api_key is not None
        )

    def shutdown(self) -> None:
        """Release the client."""
        self._shut_down = True
        self.cancel()

    @staticmethod
    def _resolve_model(cfg: AgentConfig) -> str:
        if cfg.model:
            return cfg.model
        try:
            from services.settings import default_transcript_cleanup_model

            fallback = default_transcript_cleanup_model(cfg.provider)
            if fallback:
                return fallback
        except Exception:
            pass
        if cfg.provider not in _DEFAULT_MODELS:
            raise ValueError("Choose a meeting text model first.")
        return _DEFAULT_MODELS[cfg.provider]

    def _ensure_client(self) -> Optional[Any]:
        with self._client_lock:
            if self._client is not None:
                return self._client
            client_class = _openai_class()
            if client_class is None or not self._api_key or self._shut_down:
                return None
            try:
                self._client = client_class(
                    api_key=self._api_key,
                    base_url=self._base_url,
                    default_headers=self._headers,
                    timeout=_CHECKPOINT_TIMEOUT_S,
                    max_retries=0,
                )
            except Exception:
                logger.exception("Failed to build agent LLM client")
                self._client = None
            return self._client

    def _probe_tool_support(self, client: Any) -> None:
        # Where the API rejects tools a model cannot call, being accepted is
        # proof enough; elsewhere the model has to actually call the tool.
        must_call = not self._profile.refuses_unsupported_tools
        try:
            result = self._generate_before(
                time.monotonic() + _PROBE_TIMEOUT_S,
                client.with_options(timeout=_PROBE_TIMEOUT_S), self._profile,
                model=self._model,
                messages=[{"role": "user", "content": "Call the noop tool."}],
                tools=[_NOOP_TOOL],
                tool_choice="auto",
                max_tokens=512 if must_call else 16,
            )
            self._json_mode = must_call and not any(
                call.function.name == "noop" for call in result.tool_calls
            )
        except Exception as exc:
            logger.warning(
                "Tool-call capability probe failed (%s); using JSON-mode "
                "fallback", exc,
            )
            self._json_mode = True
            self._note_error(exc)

    def _generate_before(self, deadline: float, client: Any, profile: Any, **kwargs) -> Any:
        """Bound total HTTP wall time, including responses that trickle forever.

        Only this waiting thread may use a result to call meeting tools. A
        late result is dropped, and another request cannot start while the
        abandoned network call is still draining.
        """
        done = threading.Event()
        outcome: Dict[str, Any] = {}

        def request() -> None:
            try:
                outcome["value"] = generate(client, profile, **kwargs)
            except Exception as exc:
                outcome["error"] = exc
            finally:
                done.set()

        with self._generation_lock:
            self._raise_if_unwanted(deadline)
            if self._generation_thread is not None and self._generation_thread.is_alive():
                raise TextGenerationError("previous meeting text request is still ending")
            thread = threading.Thread(target=request, name="meeting-text-request", daemon=True)
            self._generation_thread = thread
            thread.start()
        self._wait_for_generation(deadline, done)
        self._raise_if_unwanted(deadline)
        if "error" in outcome:
            raise outcome["error"]
        # done is set immediately before return; join makes the next tool
        # round safe even when it starts without any scheduler delay.
        thread.join()
        return outcome["value"]

    def _raise_if_unwanted(self, deadline: float) -> None:
        """Raise when the pass was canceled or its deadline has passed."""
        if self._cancel_event.is_set():
            raise TextGenerationError(_CANCELED)
        if time.monotonic() >= deadline:
            raise TextGenerationError(_DEADLINE_EXCEEDED)

    def _wait_for_generation(self, deadline: float, done: threading.Event) -> None:
        while True:
            if self._cancel_event.is_set():
                raise TextGenerationError(_CANCELED)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Avoid racing a synchronous socket close against a read.
                # No tool runs on the network thread, so dropping its output
                # also revokes every possible late write.
                raise TextGenerationError(_DEADLINE_EXCEEDED)
            if done.wait(min(_GENERATION_POLL_S, remaining)):
                return

    def _note_error(self, exc: Exception) -> None:
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
        elif self._looks_like_response_format_error(exc):
            self._use_json_response_format = False

    @staticmethod
    def _looks_like_response_format_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return "response_format" in text or "json_object" in text

    def _run_pass(self, payload: CheckpointPayload, timeout_s: float) -> AgentResult:
        if not self.is_healthy():
            return AgentResult(ok=False, error="agent_unavailable")
        if self._tools is None or self._cfg is None:
            return AgentResult(ok=False, error="not_initialized")
        self._cancel_event.clear()
        client = self._ensure_client()
        if client is None:
            return AgentResult(ok=False, error="client_unavailable")

        if payload.is_notes:
            system_prompt = build_note_taker_system_prompt()
            user_prompt = build_notes_user_prompt(
                payload.state_snapshot,
                payload.new_segments,
            )
        else:
            system_prompt = self._cfg.system_prompt or ""
            user_prompt = build_checkpoint_user_prompt(
                payload.state_snapshot,
                payload.new_segments,
                payload.is_consolidation,
                is_polish=payload.is_polish,
            )
        max_rounds = (
            _MAX_CONSOLIDATION_TOOL_ROUNDS if payload.is_consolidation
            else _MAX_TOOL_ROUNDS
        )
        if (
            (past_recall_enabled() or context_folder_enabled())
            and max_rounds < _MAX_TOOL_ROUNDS_WITH_RECALL
        ):
            max_rounds = _MAX_TOOL_ROUNDS_WITH_RECALL
        scope = ToolScope.for_payload(payload)
        self._pass_deadline = time.monotonic() + timeout_s
        if self._json_mode:
            return self._run_json_mode(
                client, system_prompt, user_prompt, timeout_s, scope=scope,
            )
        return self._run_tool_mode(
            client, system_prompt, user_prompt, timeout_s,
            max_rounds=max_rounds, scope=scope,
        )

    def _run_tool_mode(self, client: Any, system_prompt: str, user_prompt: str,
                       timeout_s: float, max_rounds: int = _MAX_TOOL_ROUNDS,
                       scope: ToolScope = ToolScope(),
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
                    ok=False, op_results=op_results, error=_CANCELED, usage=usage,
                )
            remaining = self._pass_remaining(timeout_s)
            if remaining <= 0:
                return AgentResult(ok=False, op_results=op_results, error=_DEADLINE_EXCEEDED, usage=usage)
            try:
                response = self._generate_before(time.monotonic() + remaining,
                    client.with_options(timeout=remaining), self._profile,
                    cancel_event=self._cancel_event,
                    model=self._model,
                    messages=messages,
                    tools=_TOOLS,
                    tool_choice="auto",
                    temperature=_TEMPERATURE,
                )
            except Exception as exc:
                self._note_error(exc)
                return AgentResult(
                    ok=False, op_results=op_results, error=self._failure_text(exc), usage=usage,
                )
            merge_usage(usage, getattr(response, "usage", None))

            tool_calls = response.tool_calls
            if not tool_calls:
                if not response.text:
                    return AgentResult(ok=False, op_results=op_results,
                                       error="empty response from model", usage=usage)
                break

            messages.append(response.assistant_message)
            for call in tool_calls:
                if self._cancel_event.is_set():
                    return AgentResult(ok=False, op_results=op_results, error=_CANCELED, usage=usage)
                content = self._run_tool_call(call, scope, op_results)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": content,
                })
            # Round 2 feeds the results back so the model can self-correct
            # rejections; after the final round no further call is made.

        return AgentResult(ok=True, op_results=op_results, usage=usage)

    def _pass_remaining(self, timeout_s: float) -> float:
        """Seconds the next request may take: its own timeout, capped by the pass deadline."""
        pass_deadline = getattr(self, "_pass_deadline", time.monotonic() + timeout_s)
        return min(timeout_s, pass_deadline - time.monotonic())

    def _failure_text(self, exc: Exception) -> str:
        return _CANCELED if self._cancel_event.is_set() else str(exc)

    def _run_tool_call(self, call: Any, scope: ToolScope,
                       op_results: List[OpResult]) -> str:
        """Run one model tool call; returns the tool message content for the model."""
        try:
            args = json.loads(call.function.arguments or "{}")
            if not isinstance(args, dict):
                raise ValueError("tool arguments must be a JSON object")
            result, results = run_tool(
                self._tools, call.function.name, args, scope,
            )
            op_results.extend(results)
            return tool_result_text(call.function.name, result)
        except json.JSONDecodeError as exc:
            logger.warning(
                "Tool call %s produced malformed JSON: %s",
                call.function.name, exc,
            )
            return json.dumps({
                "error": (
                    f"Your tool call was not valid JSON ({exc}). "
                    "Re-emit the SAME operations as one valid JSON "
                    "object, splitting into several smaller tool "
                    "calls if the batch is long."
                ),
            })
        except Exception as exc:
            logger.warning(
                "Tool call %s failed: %s", call.function.name, exc,
            )
            return json.dumps({"error": str(exc)})

    def _run_json_mode(self, client: Any, system_prompt: str, user_prompt: str,
                       timeout_s: float, scope: ToolScope = ToolScope(),
                       ) -> AgentResult:
        """Fallback for models without tool support: one ops-JSON object."""
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": f"{user_prompt}\n\n{JSON_FALLBACK_INSTRUCTIONS}",
            },
        ]
        usage: Dict[str, Any] = {}
        all_results: List[OpResult] = []

        for attempt in range(_JSON_MODE_ATTEMPTS):
            can_repair = attempt < _JSON_MODE_ATTEMPTS - 1
            if self._cancel_event.is_set():
                return AgentResult(ok=False, op_results=all_results, error=_CANCELED, usage=usage)
            kwargs = self._json_request_kwargs(messages)
            remaining = self._pass_remaining(timeout_s)
            if remaining <= 0:
                return AgentResult(ok=False, op_results=all_results, error=_DEADLINE_EXCEEDED, usage=usage)
            try:
                response = self._generate_before(time.monotonic() + remaining,
                                    client.with_options(timeout=remaining), self._profile,
                                    cancel_event=self._cancel_event, **kwargs)
            except Exception as exc:
                if self._drop_response_format_after(exc):
                    continue
                return AgentResult(ok=False, error=self._failure_text(exc), usage=usage)
            merge_usage(usage, getattr(response, "usage", None))

            content = response.text
            try:
                ops = _parse_ops(content)
            except ValueError as exc:  # includes json.JSONDecodeError
                if can_repair:
                    # One repair retry with the parse error in context.
                    messages.append(response.assistant_message)
                    messages.append(_invalid_json_feedback(exc))
                    continue
                return AgentResult(
                    ok=False, op_results=all_results, error=f"invalid JSON from model: {exc}",
                    usage=usage,
                )

            assert self._tools is not None
            op_results = apply_patch_ops(self._tools, ops, scope)
            all_results.extend(op_results)
            # Ops outside the pass's job are reported, but neither worth a
            # repair round nor a failed pass on their own.
            rejected = [
                r for r in op_results
                if not r.ok and r.reason not in PASS_REJECTIONS
            ]
            repairable = [r for r in rejected if r.reason not in _UNREPAIRABLE_REJECTIONS]
            if repairable and can_repair:
                messages.append(response.assistant_message)
                messages.append(_rejected_ops_feedback(op_results))
                continue
            return _json_pass_result(rejected, repairable, all_results, usage)

        return AgentResult(ok=False, error="json_mode_exhausted", usage=usage)

    def _json_request_kwargs(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": _TEMPERATURE,
        }
        if self._use_json_response_format:
            kwargs["response_format"] = {"type": "json_object"}
        return kwargs

    def _drop_response_format_after(self, exc: Exception) -> bool:
        """Record a failed JSON-mode request.

        Returns True, having turned ``response_format`` off, when the provider
        rejected it and the request should simply be retried without it.
        """
        retry_without_format = (
            self._use_json_response_format
            and self._looks_like_response_format_error(exc)
        )
        self._note_error(exc)
        if retry_without_format:
            logger.warning(
                "Provider rejected response_format=json_object; "
                "retrying without it"
            )
            self._use_json_response_format = False
        return retry_without_format


def _parse_ops(content: Any) -> List[Any]:
    """The ``ops`` list of a JSON-mode response; ValueError when malformed."""
    data = json.loads(content)
    if not isinstance(data, dict):
        raise ValueError("response is not a JSON object")
    ops = data.get("ops")
    if not isinstance(ops, list):
        raise ValueError('missing "ops" list')
    return ops


def _invalid_json_feedback(exc: Exception) -> Dict[str, Any]:
    return {
        "role": "user",
        "content": (
            f"Your previous response was invalid ({exc}). "
            'Respond ONLY with a valid JSON object of the '
            'form {"ops": [...]}.'
        ),
    }


def _rejected_ops_feedback(op_results: List[OpResult]) -> Dict[str, Any]:
    return {"role": "user", "content": (
        "Operation results: " + json.dumps(op_results_payload(op_results))
        + "\nCorrect the rejected operations using these reasons and current "
        "revisions. Do not repeat successful operations or retry protected "
        "human items. Emit only the remaining warranted changes as "
        'a JSON object of the form {"ops": [...]}.'
    )}


def _json_pass_result(rejected: List[OpResult], repairable: List[OpResult],
                      all_results: List[OpResult], usage: Dict[str, Any]) -> AgentResult:
    failed = bool(repairable) or (
        bool(rejected) and not any(r.ok for r in all_results)
    )
    error = None
    if failed:
        reasons = sorted({str(r.reason) for r in rejected})
        error = "Unapplied operations: " + ", ".join(reasons)
    return AgentResult(ok=not failed, op_results=all_results, usage=usage, error=error)
