"""Host-side execution for Phase 1 host-facilitated AI services.

Every operation here runs on a paired OpenWhisper host on behalf of an
authenticated client over the existing TLS WebSocket protocol (v1, additive):

* ``text_generate`` / ``text_cleanup``: runs the host's configured
  :class:`services.transcript_cleanup.TranscriptCleanup` (cloud API,
  Ollama/LM Studio/vLLM/llama.cpp, or installed coding agent).
* ``fast_judge``: runs the host's configured TypeSafe/Jev or
  :class:`services.text_judge.TextModelJudge` route and returns validated
  answers.
* ``walkthrough_writeup_start`` / ``walkthrough_writeup_put`` /
  ``walkthrough_writeup_status`` / ``walkthrough_writeup_cancel``: stages the
  client's walkthrough Markdown and PNG screenshots into an isolated temporary
  directory, executes the host's write-up coding agent asynchronously, returns
  the cleaned Markdown and Look metadata, and always deletes the temporary
  directory.
* ``agent_ask`` / ``agent_ask_cancel``: opt-in headless quick Q&A on an
  installed coding agent (``AgentMode.ASK``), cancellable on disconnect or via
  ``agent_ask_cancel``.

Security invariants:
* Every opt-in setting is rechecked from live settings on every request.
* Host API keys, credential environment names, raw endpoint URLs, and host
  filesystem paths are never returned to clients.
* User prompts, transcripts, and model replies are never logged.
* All staged filenames and byte counts are strictly validated.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from services.remote_asr import protocol
from services.remote_asr import settings as remote_settings
from services.settings import (
    MeetingAgentCore,
    TranscriptCleanupProvider,
    TranscriptCleanupReasoning,
    TypeSafeProvider,
    resolve_transcript_cleanup_model,
    resolve_transcript_cleanup_provider,
    resolve_transcript_cleanup_reasoning,
    resolve_typesafe_provider,
    resolve_walkthrough_write_up_agent,
    settings_manager,
)

logger = logging.getLogger(__name__)

MAX_CONCURRENT_WRITEUPS = 2
MAX_STAGING_WRITEUPS = 4
WRITEUP_JOB_TTL_S = 15 * 60.0
DEFAULT_CLEANUP_TIMEOUT_S = 60.0
MAX_CLEANUP_TIMEOUT_S = 300.0
DEFAULT_FAST_JUDGE_TIMEOUT_S = 30.0
MAX_FAST_JUDGE_TIMEOUT_S = 60.0
DEFAULT_ASK_TIMEOUT_S = 120.0
MAX_ASK_TIMEOUT_S = 300.0
MAX_ASK_CHARS = 50_000
MAX_WALKTHROUGH_NOTES = 200
MAX_NOTE_CHARS = 4_000

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}\.png$", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_NAME_RE = re.compile(r"[\w.:/@\[\]+-]{1,200}")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Reading source prompt for Claude Code when staging walkthrough.md + PNGs
#: directly in a sandboxed temp folder (no Qt PDF rendering needed on host).
_STAGED_MD_SOURCE = (
    "The walkthrough is walkthrough.md in this folder, and its screenshots are "
    "PNG files beside it. Read walkthrough.md and any screenshots you need for "
    "a closer look. Read no other files."
)


def _safe_shot_name(name: Any) -> str:
    if not isinstance(name, str) or not _SHOT_NAME_RE.fullmatch(name):
        raise ValueError("Invalid walkthrough screenshot file name.")
    return name


def _safe_job_id(job_id: Any, label: str = "job_id") -> str:
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id):
        raise ValueError(f"Invalid {label}.")
    return job_id


def _bounded_timeout(raw: Any, default: float, minimum: float, maximum: float) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("Invalid timeout value.")
    val = float(raw)
    if val <= 0 or val != val:  # reject non-positive or NaN
        raise ValueError("Timeout must be positive.")
    return max(minimum, min(val, maximum))


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class _StagedShot:
    name: str
    size: int
    sha256: str
    received: int = 0
    done: bool = False


@dataclass
class _WriteUpJob:
    job_id: str
    device_id: str
    device_name: str
    stage_dir: str
    status: str  # "staging" | "running" | "done" | "failed" | "canceled"
    agent: str
    agent_name: str
    markdown: str
    instructions: str
    follow_notes: bool
    notes: list[tuple[str, int]]
    duration_s: float
    shot_order: list[str]
    shots: dict[str, _StagedShot] = field(default_factory=dict)
    total_bytes: int = 0
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)
    started_at: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    result_markdown: str = ""
    result_look: dict[str, str] = field(default_factory=dict)
    error: str = ""
    host_ms: float = 0.0


class HostAIServices:
    """Executes opt-in Phase 1 AI operations on behalf of paired clients."""

    def __init__(
        self,
        *,
        settings_loader: Callable[[], dict[str, Any]] | None = None,
        cleaner_factory: Callable[..., Any] | None = None,
        judge_factory: Callable[[dict[str, Any]], Any] | None = None,
        write_up_runner: Callable[..., Any] | None = None,
        agent_ask_runner: Callable[..., Any] | None = None,
        host_name: str | None = None,
    ) -> None:
        self._settings_loader = settings_loader
        self._cleaner_factory = cleaner_factory
        self._judge_factory = judge_factory
        self._write_up_runner = write_up_runner
        self._agent_ask_runner = agent_ask_runner
        self._host_name = host_name or socket.gethostname()
        self._lock = threading.Lock()
        self._writeup_jobs: dict[str, _WriteUpJob] = {}
        self._active_asks: dict[str, tuple[str, threading.Event]] = {}
        self._shutdown = False

    def _settings(self) -> dict[str, Any]:
        if self._settings_loader is not None:
            loaded = self._settings_loader()
            return dict(loaded) if isinstance(loaded, dict) else {}
        return settings_manager.load_all_settings()

    # ---- capability & non-secret metadata advertising ----

    def advertised(self) -> dict[str, Any]:
        """Return ``{"capabilities": {...}, "ai": {...}}`` for ``ready``.

        Never blocks on subprocesses/network and never reveals API keys,
        credential names, endpoint URLs, or local paths. Disabled services
        expose only ``{"enabled": False, "available": False}``.
        """
        settings = self._settings()
        cleanup = self._cleanup_metadata(settings)
        judgments = self._fast_judgments_metadata(settings)
        writeup = self._walkthrough_writeup_metadata(settings)
        ask = self._agent_ask_metadata(settings)
        return {
            "capabilities": {
                "text_cleanup": bool(cleanup["enabled"]),
                "fast_judgments": bool(judgments["enabled"]),
                "walkthrough_writeup": bool(writeup["enabled"]),
                "agent_ask": bool(ask["enabled"]),
            },
            "ai": {
                "text_cleanup": cleanup,
                "fast_judgments": judgments,
                "walkthrough_writeup": writeup,
                "agent_ask": ask,
            },
        }

    def _cleanup_metadata(self, settings: dict[str, Any]) -> dict[str, Any]:
        enabled = remote_settings.host_text_cleanup(settings)
        if not enabled:
            return {"enabled": False, "available": False}
        provider = resolve_transcript_cleanup_provider(settings)
        model = resolve_transcript_cleanup_model(settings)
        if provider == TranscriptCleanupProvider.REMOTE_HOST:
            return {
                "enabled": True,
                "available": False,
                "provider": "",
                "model": "",
                "label": "Not configured",
            }
        if provider in TranscriptCleanupProvider.INSTALLED:
            from services import installed_agents

            known, agent = installed_agents.known_agent(provider)
            spec = installed_agents.AGENT_SPECS.get(provider)
            base_label = spec.name if spec is not None else provider
            label = f"{base_label} · {model}" if model else base_label
            available = (agent is not None and agent.usable) if known else True
            return {
                "enabled": True,
                "available": bool(available),
                "provider": provider,
                "model": model,
                "label": label,
            }
        from services.text_llm import get_profile, lookup_env_value

        profile = get_profile(provider, settings)
        if profile is None:
            return {
                "enabled": True,
                "available": False,
                "provider": provider,
                "model": model,
                "label": provider,
            }
        has_auth = not profile.api_key_env or bool(lookup_env_value(profile.api_key_env))
        available = bool(model) and has_auth
        label = f"{profile.name} · {model}" if model else profile.name
        return {
            "enabled": True,
            "available": available,
            "provider": provider,
            "model": model,
            "label": label,
        }

    def _fast_judgments_metadata(self, settings: dict[str, Any]) -> dict[str, Any]:
        enabled = remote_settings.host_fast_judgments(settings)
        if not enabled:
            return {"enabled": False, "available": False}
        provider = resolve_typesafe_provider(settings)
        if provider == TypeSafeProvider.REMOTE_HOST:
            return {
                "enabled": True,
                "available": False,
                "provider": "",
                "model": "",
                "label": "Not configured",
            }
        from services import typesafe

        route = typesafe.route_from_settings(settings)
        available = typesafe.key_present(settings, route=route) and bool(route.model)
        label = f"{route.name} · {route.model}" if route.model else route.name
        return {
            "enabled": True,
            "available": bool(available),
            "provider": provider,
            "model": route.model,
            "label": label,
        }

    def _walkthrough_writeup_metadata(self, settings: dict[str, Any]) -> dict[str, Any]:
        enabled = remote_settings.host_walkthrough_writeup(settings)
        if not enabled:
            return {"enabled": False, "available": False}
        agent_id = resolve_walkthrough_write_up_agent(settings)
        if agent_id not in MeetingAgentCore.INSTALLED:
            return {
                "enabled": True,
                "available": False,
                "agent": "",
                "label": "Not configured",
            }
        from services import installed_agents
        from services.walkthrough.write_up import agent_name

        known, found = installed_agents.known_agent(agent_id)
        available = (found is not None and found.usable) if known else True
        return {
            "enabled": True,
            "available": bool(available),
            "agent": agent_id,
            "label": agent_name(agent_id),
        }

    def _agent_ask_metadata(self, settings: dict[str, Any]) -> dict[str, Any]:
        enabled = remote_settings.host_agent_ask(settings)
        if not enabled:
            return {"enabled": False, "available": False}
        from services import installed_agents

        scan = installed_agents.cached_scan()
        if scan is None:
            return {
                "enabled": True,
                "available": True,
                "agents": [],
                "label": "Installed coding agent",
            }
        usable = [
            agent_id
            for agent_id in MeetingAgentCore.INSTALLED
            if (found := scan.get(agent_id)) is not None and found.usable
        ]
        labels = [installed_agents.AGENT_SPECS[a].name for a in usable]
        return {
            "enabled": True,
            "available": bool(usable),
            "agents": usable,
            "label": ", ".join(labels) if labels else "No coding agent installed",
        }

    # ---- dispatch ----

    def handle(
        self,
        op: str,
        header: dict,
        payload: bytes,
        device: dict,
        cancel_event: threading.Event | None = None,
    ) -> dict:
        if self._shutdown:
            raise protocol.RemoteServiceError(
                "The host is shutting down.", code="unavailable",
            )
        if op in ("text_generate", "text_cleanup"):
            return self._handle_text_cleanup(header, payload, cancel_event)
        if op == "fast_judge":
            return self._handle_fast_judge(header, payload)
        if op == "walkthrough_writeup_start":
            return self._handle_walkthrough_start(header, payload, device)
        if op == "walkthrough_writeup_put":
            return self._handle_walkthrough_put(header, payload, device)
        if op == "walkthrough_writeup_status":
            return self._handle_walkthrough_status(header, payload, device)
        if op == "walkthrough_writeup_cancel":
            return self._handle_walkthrough_cancel(header, payload, device)
        if op == "agent_ask":
            return self._handle_agent_ask(header, payload, device, cancel_event)
        if op == "agent_ask_cancel":
            return self._handle_agent_ask_cancel(header, payload, device)
        raise ValueError(f"Unknown AI operation: {op!r}")

    # ---- text_generate / text_cleanup ----

    def _handle_text_cleanup(
        self,
        header: dict,
        payload: bytes,
        cancel_event: threading.Event | None,
    ) -> dict:
        settings = self._settings()
        if not remote_settings.host_text_cleanup(settings):
            raise PermissionError(
                f"{self._host_name} isn't sharing AI cleanup and text rewrites. "
                "Turn it on in Settings → Remote engine there."
            )
        req = protocol.merge_json_payload(header, payload)
        text = req.get("text")
        if text is None:
            text = req.get("user_prompt")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Text to process must be a non-empty string.")
        if len(text) > protocol.MAX_AI_TEXT_CHARS:
            raise protocol.RemoteServiceError(
                f"Text exceeds the host limit of {protocol.MAX_AI_TEXT_CHARS:,} characters.",
                code="too_large",
            )
        system_prompt = req.get("system_prompt")
        if system_prompt is not None:
            if not isinstance(system_prompt, str):
                raise ValueError("system_prompt must be a string.")
            if len(system_prompt) > protocol.MAX_AI_PROMPT_CHARS:
                raise protocol.RemoteServiceError(
                    "System prompt exceeds the host size limit.", code="too_large",
                )
        raw_reasoning = req.get("reasoning")
        if raw_reasoning is not None and raw_reasoning not in TranscriptCleanupReasoning.ALL:
            raise ValueError("Invalid reasoning level.")
        reasoning = (
            raw_reasoning
            if isinstance(raw_reasoning, str)
            else resolve_transcript_cleanup_reasoning(settings)
        )
        timeout_s = _bounded_timeout(
            req.get("timeout_s"), DEFAULT_CLEANUP_TIMEOUT_S, 1.0, MAX_CLEANUP_TIMEOUT_S,
        )
        deadline_s = _bounded_timeout(
            req.get("deadline_s"), timeout_s, 1.0, MAX_CLEANUP_TIMEOUT_S,
        )

        provider = resolve_transcript_cleanup_provider(settings)
        model = resolve_transcript_cleanup_model(settings)
        if provider == TranscriptCleanupProvider.REMOTE_HOST:
            raise protocol.RemoteServiceError(
                f"{self._host_name}'s AI cleanup is set to another remote host and cannot relay.",
                code="unavailable",
            )

        from services.transcript_cleanup import CANCELED_REASON, TranscriptCleanup

        started = time.perf_counter()
        if self._cleaner_factory is not None:
            cleaner = self._cleaner_factory(
                provider=provider,
                model=model,
                reasoning=reasoning,
                cancel_event=cancel_event,
            )
        else:
            cleaner = TranscriptCleanup(
                provider=provider,
                model=model,
                reasoning=reasoning,
                cancel_event=cancel_event,
            )
        try:
            if not cleaner.is_available():
                raise protocol.RemoteServiceError(
                    f"AI cleanup isn't configured on {self._host_name}.",
                    code="unavailable",
                )
            cleaned = cleaner.cleanup(
                text,
                system_prompt=system_prompt,
                timeout_s=timeout_s,
                deadline_s=deadline_s,
            )
            error = getattr(cleaner, "last_error", None)
        finally:
            close = getattr(cleaner, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.debug("Error closing host cleanup instance", exc_info=True)

        if error == CANCELED_REASON or (cancel_event is not None and cancel_event.is_set()):
            raise protocol.RemoteServiceError("Canceled", code="canceled")
        if error is not None:
            raise protocol.RemoteServiceError(str(error), code="ai_failed")
        host_ms = round((time.perf_counter() - started) * 1000, 1)
        return {
            "text": cleaned,
            "provider": provider,
            "model": model,
            "host_ms": host_ms,
        }

    # ---- fast_judge ----

    def _build_host_judge(self, settings: dict[str, Any]) -> tuple[Any, str]:
        from services import typesafe

        if self._judge_factory is not None:
            judge = self._judge_factory(settings)
            if judge is None:
                raise protocol.RemoteServiceError(
                    f"Fast judgments aren't configured on {self._host_name}.",
                    code="unavailable",
                )
            route = getattr(judge, "route", None)
            route_id = getattr(route, "id", "host")
            return judge, str(route_id)

        provider = resolve_typesafe_provider(settings)
        if provider == TypeSafeProvider.REMOTE_HOST:
            raise protocol.RemoteServiceError(
                f"{self._host_name}'s fast judgments are set to another remote host and cannot relay.",
                code="unavailable",
            )
        route = typesafe.route_from_settings(settings)
        key = typesafe.route_key(route)
        if not key or not route.model:
            raise protocol.RemoteServiceError(
                f"Fast judgments aren't configured on {self._host_name}.",
                code="unavailable",
            )
        if route.profile is None:
            return typesafe.TypeSafeJudge(key, route=route), route.id
        from services.text_judge import TextModelJudge

        return TextModelJudge(key, route=route), route.id

    def _handle_fast_judge(self, header: dict, payload: bytes) -> dict:
        settings = self._settings()
        if not remote_settings.host_fast_judgments(settings):
            raise PermissionError(
                f"{self._host_name} isn't sharing fast judgments. "
                "Turn it on in Settings → Remote engine there."
            )
        from services import typesafe

        req = protocol.merge_json_payload(header, payload)
        if "state" not in req:
            raise ValueError("Fast judgment request requires state.")
        state = req["state"]
        questions = req.get("questions")
        if not isinstance(questions, dict) or not questions:
            raise ValueError("Fast judgment request requires a non-empty questions object.")
        for q_id, spec in questions.items():
            if not isinstance(q_id, str) or not q_id or not isinstance(spec, dict):
                raise ValueError("Invalid fast judgment question entry.")
            if spec.get("type") not in ("noul", "choice", "score"):
                raise ValueError(f"Unsupported question type on {q_id!r}.")
        try:
            encoded = json.dumps({"state": state, "questions": questions})
        except (TypeError, ValueError) as exc:
            raise ValueError("Fast judgment state must be JSON-serializable.") from exc
        if len(encoded) > typesafe.MAX_STATE_CHARS:
            raise protocol.RemoteServiceError(
                f"Fast judgment state exceeds the {typesafe.MAX_STATE_CHARS} character bound.",
                code="too_large",
            )
        timeout_s = _bounded_timeout(
            req.get("timeout_s"), DEFAULT_FAST_JUDGE_TIMEOUT_S, 0.5, MAX_FAST_JUDGE_TIMEOUT_S,
        )

        judge, route_id = self._build_host_judge(settings)
        started = time.perf_counter()
        answers = judge.ask(state, questions, timeout_s=timeout_s)
        if answers is None:
            raise protocol.RemoteServiceError(
                "The host fast-judgment model did not return a valid answer.",
                code="ai_failed",
            )
        try:
            validated = typesafe.validate_answers({"answers": answers}, questions)
        except typesafe.TypeSafeError as exc:
            raise protocol.RemoteServiceError(
                f"Host judgment validation failed: {exc}", code="ai_failed",
            ) from exc
        host_ms = round((time.perf_counter() - started) * 1000, 1)
        return {
            "answers": validated,
            "provider": route_id,
            "model": str(getattr(judge, "model", "")),
            "host_ms": host_ms,
        }

    # ---- walkthrough_writeup_* ----

    def _prune_writeup_jobs_locked(self) -> None:
        cutoff = time.monotonic() - WRITEUP_JOB_TTL_S
        stale_ids = [
            job_id
            for job_id, job in self._writeup_jobs.items()
            if job.status != "running" and job.updated_at < cutoff
        ]
        for job_id in stale_ids:
            job = self._writeup_jobs.pop(job_id)
            if job.stage_dir:
                shutil.rmtree(job.stage_dir, ignore_errors=True)

    def _handle_walkthrough_start(self, header: dict, payload: bytes, device: dict) -> dict:
        settings = self._settings()
        if not remote_settings.host_walkthrough_writeup(settings):
            raise PermissionError(
                f"{self._host_name} isn't sharing walkthrough AI write-ups. "
                "Turn it on in Settings → Remote engine there."
            )
        req = protocol.merge_json_payload(header, payload)
        device_id = str(device.get("id") or "")
        device_name = str(device.get("name") or "")

        # Resume/start a previously staged chunked job.
        if req.get("job_id") is not None:
            job_id = _safe_job_id(req.get("job_id"))
            with self._lock:
                self._prune_writeup_jobs_locked()
                job = self._writeup_jobs.get(job_id)
                if job is None or job.device_id != device_id:
                    raise LookupError("Unknown walkthrough write-up job.")
                if job.status != "staging":
                    return self._writeup_status_dict(job)
                if any(not shot.done for shot in job.shots.values()):
                    raise ValueError("Not all walkthrough screenshots have been uploaded yet.")
                running = sum(1 for j in self._writeup_jobs.values() if j.status == "running")
                if running >= MAX_CONCURRENT_WRITEUPS:
                    raise protocol.RemoteServiceError(
                        "Too many walkthrough write-ups are already running on the host.",
                        code="busy",
                        retry_after_ms=2000,
                    )
                self._launch_writeup_locked(job)
                return self._writeup_status_dict(job)

        agent_id = resolve_walkthrough_write_up_agent(settings)
        if agent_id == MeetingAgentCore.REMOTE_HOST or agent_id not in MeetingAgentCore.INSTALLED:
            raise protocol.RemoteServiceError(
                f"No local write-up agent is configured on {self._host_name}.",
                code="unavailable",
            )
        from services.walkthrough.write_up import agent_name

        markdown = req.get("markdown")
        if not isinstance(markdown, str) or not markdown.strip():
            raise ValueError("Walkthrough markdown must be a non-empty string.")
        markdown_bytes = markdown.encode("utf-8")
        if len(markdown_bytes) > protocol.MAX_WALKTHROUGH_MARKDOWN_BYTES:
            raise protocol.RemoteServiceError(
                "Walkthrough markdown exceeds the host size limit.", code="too_large",
            )
        instructions = req.get("instructions")
        if instructions is None:
            instructions = ""
        if not isinstance(instructions, str):
            raise ValueError("Walkthrough instructions must be a string.")
        if len(instructions) > protocol.MAX_AI_PROMPT_CHARS:
            raise protocol.RemoteServiceError(
                "Walkthrough instructions exceed the host size limit.", code="too_large",
            )
        follow_notes = bool(req.get("follow_notes", False))
        raw_duration = req.get("duration_s", 0.0)
        if isinstance(raw_duration, bool) or not isinstance(raw_duration, (int, float)) or raw_duration < 0:
            raise ValueError("Invalid walkthrough duration.")
        duration_s = float(raw_duration)

        raw_notes = req.get("notes") or []
        if not isinstance(raw_notes, list) or len(raw_notes) > MAX_WALKTHROUGH_NOTES:
            raise ValueError("Invalid walkthrough notes.")
        parsed_notes: list[tuple[str, int]] = []
        for entry in raw_notes:
            if not isinstance(entry, dict):
                raise ValueError("Invalid walkthrough note entry.")
            note_text = entry.get("text")
            section = entry.get("section")
            if (
                not isinstance(note_text, str)
                or len(note_text) > MAX_NOTE_CHARS
                or isinstance(section, bool)
                or not isinstance(section, int)
                or section < 0
            ):
                raise ValueError("Invalid walkthrough note entry.")
            if note_text.strip():
                parsed_notes.append((note_text.strip(), section))

        raw_shots = req.get("shots") or []
        if not isinstance(raw_shots, list) or len(raw_shots) > protocol.MAX_WALKTHROUGH_SHOTS:
            raise ValueError("Invalid or too many walkthrough screenshots.")
        stage_only = bool(req.get("stage_only", False))

        with self._lock:
            self._prune_writeup_jobs_locked()
            running = sum(1 for j in self._writeup_jobs.values() if j.status == "running")
            staging = sum(1 for j in self._writeup_jobs.values() if j.status == "staging")
            if stage_only:
                if staging >= MAX_STAGING_WRITEUPS:
                    raise protocol.RemoteServiceError(
                        "Too many walkthrough uploads are in progress on the host.",
                        code="busy",
                        retry_after_ms=2000,
                    )
            elif running >= MAX_CONCURRENT_WRITEUPS:
                raise protocol.RemoteServiceError(
                    "Too many walkthrough write-ups are already running on the host.",
                    code="busy",
                    retry_after_ms=2000,
                )

        stage_dir = tempfile.mkdtemp(prefix="openwhisper-remote-wu-")
        try:
            with open(os.path.join(stage_dir, "walkthrough.md"), "w", encoding="utf-8", newline="\n") as handle:
                handle.write(markdown)
            shot_order: list[str] = []
            staged_shots: dict[str, _StagedShot] = {}
            total_bytes = len(markdown_bytes)
            for item in raw_shots:
                if not isinstance(item, dict):
                    raise ValueError("Invalid screenshot descriptor.")
                name = _safe_shot_name(item.get("name"))
                if name in staged_shots:
                    raise ValueError(f"Duplicate screenshot name: {name}")
                shot_order.append(name)
                if "data_b64" in item:
                    raw_b64 = item["data_b64"]
                    if not isinstance(raw_b64, str):
                        raise ValueError("Screenshot data must be base64.")
                    try:
                        png_bytes = base64.b64decode(raw_b64, validate=True)
                    except (ValueError, binascii.Error) as exc:
                        raise ValueError(f"Invalid base64 data for {name}.") from exc
                    if (
                        len(png_bytes) < len(_PNG_MAGIC)
                        or len(png_bytes) > protocol.MAX_WALKTHROUGH_SHOT_BYTES
                        or not png_bytes.startswith(_PNG_MAGIC)
                    ):
                        raise ValueError(f"{name} is not a valid bounded PNG screenshot.")
                    total_bytes += len(png_bytes)
                    if total_bytes > protocol.MAX_WALKTHROUGH_TOTAL_BYTES:
                        raise protocol.RemoteServiceError(
                            "Walkthrough screenshots exceed the host size limit.",
                            code="too_large",
                        )
                    shot_path = os.path.join(stage_dir, name)
                    with open(shot_path, "wb") as handle:
                        handle.write(png_bytes)
                    digest = hashlib.sha256(png_bytes).hexdigest()
                    staged_shots[name] = _StagedShot(
                        name=name,
                        size=len(png_bytes),
                        sha256=digest,
                        received=len(png_bytes),
                        done=True,
                    )
                else:
                    if not stage_only:
                        raise ValueError(f"Missing inline PNG data for {name}.")
                    size = item.get("size")
                    sha = item.get("sha256")
                    if (
                        isinstance(size, bool)
                        or not isinstance(size, int)
                        or size < len(_PNG_MAGIC)
                        or size > protocol.MAX_WALKTHROUGH_SHOT_BYTES
                    ):
                        raise ValueError(f"Invalid declared size for {name}.")
                    if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
                        raise ValueError(f"Invalid declared sha256 for {name}.")
                    total_bytes += size
                    if total_bytes > protocol.MAX_WALKTHROUGH_TOTAL_BYTES:
                        raise protocol.RemoteServiceError(
                            "Walkthrough screenshots exceed the host size limit.",
                            code="too_large",
                        )
                    staged_shots[name] = _StagedShot(name=name, size=size, sha256=sha)

            job_id = uuid.uuid4().hex[:16]
            job = _WriteUpJob(
                job_id=job_id,
                device_id=device_id,
                device_name=device_name,
                stage_dir=stage_dir,
                status="staging",
                agent=agent_id,
                agent_name=agent_name(agent_id),
                markdown=markdown,
                instructions=instructions,
                follow_notes=follow_notes,
                notes=parsed_notes,
                duration_s=duration_s,
                shot_order=shot_order,
                shots=staged_shots,
                total_bytes=total_bytes,
            )
            with self._lock:
                self._writeup_jobs[job_id] = job
                if not stage_only or all(shot.done for shot in job.shots.values()):
                    if not stage_only:
                        self._launch_writeup_locked(job)
                return self._writeup_status_dict(job)
        except BaseException:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise

    def _handle_walkthrough_put(self, header: dict, payload: bytes, device: dict) -> dict:
        settings = self._settings()
        if not remote_settings.host_walkthrough_writeup(settings):
            raise PermissionError(
                f"{self._host_name} isn't sharing walkthrough AI write-ups. "
                "Turn it on in Settings → Remote engine there."
            )
        job_id = _safe_job_id(header.get("job_id"))
        name = _safe_shot_name(header.get("name"))
        offset = header.get("offset")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Invalid screenshot chunk offset.")
        if len(payload) > protocol.WALKTHROUGH_CHUNK_BYTES:
            raise ValueError("Screenshot chunk is too large.")
        start_after = bool(header.get("start", False))
        device_id = str(device.get("id") or "")

        with self._lock:
            job = self._writeup_jobs.get(job_id)
            if job is None or job.device_id != device_id:
                raise LookupError("Unknown walkthrough write-up job.")
            if job.status != "staging":
                raise ValueError("Walkthrough job is no longer accepting uploads.")
            shot = job.shots.get(name)
            if shot is None:
                raise ValueError(f"Undeclared screenshot: {name}.")
            if shot.done:
                if start_after and all(s.done for s in job.shots.values()):
                    self._launch_writeup_locked(job)
                return {
                    "job_id": job_id,
                    "name": name,
                    "received": shot.size,
                    "done": True,
                    "status": job.status,
                }
            if offset != shot.received:
                raise ValueError(f"Unexpected chunk offset for {name}: expected {shot.received}.")
            if shot.received + len(payload) > shot.size:
                raise ValueError(f"{name} exceeds its declared size.")
            if offset == 0 and len(payload) >= len(_PNG_MAGIC) and not payload.startswith(_PNG_MAGIC):
                raise ValueError(f"{name} is not a valid PNG screenshot.")

            shot_path = os.path.join(job.stage_dir, name)
            with open(shot_path, "ab") as handle:
                handle.write(payload)
            shot.received += len(payload)
            job.updated_at = time.monotonic()

            if shot.received == shot.size:
                with open(shot_path, "rb") as handle:
                    magic = handle.read(len(_PNG_MAGIC))
                if magic != _PNG_MAGIC:
                    os.remove(shot_path)
                    shot.received = 0
                    raise ValueError(f"{name} is not a valid PNG screenshot.")
                if _file_sha256(shot_path) != shot.sha256:
                    os.remove(shot_path)
                    shot.received = 0
                    raise ValueError(f"{name} arrived damaged; please retry.")
                shot.done = True

            if start_after and all(s.done for s in job.shots.values()):
                running = sum(1 for j in self._writeup_jobs.values() if j.status == "running")
                if running >= MAX_CONCURRENT_WRITEUPS:
                    raise protocol.RemoteServiceError(
                        "Too many walkthrough write-ups are already running on the host.",
                        code="busy",
                        retry_after_ms=2000,
                    )
                self._launch_writeup_locked(job)

            return {
                "job_id": job_id,
                "name": name,
                "received": shot.received,
                "done": shot.done,
                "status": job.status,
            }

    def _launch_writeup_locked(self, job: _WriteUpJob) -> None:
        job.status = "running"
        job.started_at = time.perf_counter()
        job.updated_at = time.monotonic()
        threading.Thread(
            target=self._run_writeup_job,
            args=(job,),
            name=f"remote-writeup-{job.job_id[:8]}",
            daemon=True,
        ).start()

    def _run_writeup_job(self, job: _WriteUpJob) -> None:
        from meeting.agent.installed.drivers import AgentUnavailable
        from services.walkthrough import write_up
        from services.walkthrough.document import clock
        from services.walkthrough.session import Note

        try:
            notes = [Note(text, section) for text, section in job.notes]
            instructions = (job.instructions or "").strip() or write_up.DEFAULT_INSTRUCTIONS
            speech = write_up._SPEECH_WITH_NOTES if job.follow_notes else write_up._SPEECH
            if notes:
                speech += " " + write_up._HELD_NOTES + (
                    "" if job.follow_notes else " " + write_up._NOTE_LIMIT
                )
            common: dict[str, Any] = {
                "model": "",
                "timeout_s": write_up._TIMEOUT_S,
                "stall_s": write_up._STALL_S,
                "cancel_event": job.cancel_event,
            }
            if job.agent == MeetingAgentCore.CLAUDE_CODE:
                runner: Callable[..., Any]
                if self._write_up_runner is not None:
                    runner = self._write_up_runner
                else:
                    from meeting.agent.installed.core import run_reading_task

                    runner = run_reading_task

                lines = [instructions, "", "The walkthrough: walkthrough.md"]
                if job.shot_order:
                    lines.append(f"Its screenshots, beside it: {', '.join(job.shot_order)}")
                lines.append(f"It is {clock(job.duration_s)} of speech.")
                user_prompt = "\n".join(lines) + write_up._notes(notes, job.shot_order)
                outcome = runner(
                    job.agent,
                    system_prompt=write_up._CHARTER.format(
                        source=_STAGED_MD_SOURCE, speech=speech,
                    ),
                    user_prompt=user_prompt,
                    folder=job.stage_dir,
                    **common,
                )
            else:
                if self._write_up_runner is not None:
                    runner = self._write_up_runner
                else:
                    from meeting.agent.installed.core import run_viewing_task

                    runner = run_viewing_task

                lines = [instructions, ""]
                if job.shot_order:
                    lines.append(f"The screenshots attached, in order: {', '.join(job.shot_order)}")
                lines += [
                    f"It is {clock(job.duration_s)} of speech.",
                    "",
                    "<walkthrough>",
                    job.markdown.strip(),
                    "</walkthrough>",
                ]
                user_prompt = "\n".join(lines) + write_up._notes(notes, job.shot_order)
                images = [os.path.realpath(os.path.join(job.stage_dir, name)) for name in job.shot_order]
                outcome = runner(
                    job.agent,
                    system_prompt=write_up._CHARTER.format(
                        source=write_up._VIEW_SOURCE, speech=speech,
                    ),
                    user_prompt=user_prompt,
                    images=images,
                    **common,
                )

            with self._lock:
                job.host_ms = round((time.perf_counter() - job.started_at) * 1000, 1)
                job.updated_at = time.monotonic()
                if job.cancel_event.is_set() or getattr(outcome, "canceled", False):
                    job.status = "canceled"
                    job.error = "canceled"
                elif not getattr(outcome, "ok", False):
                    job.status = "failed"
                    job.error = str(getattr(outcome, "error", "") or f"{job.agent_name} failed")
                else:
                    text, look = write_up.document_text(
                        str(getattr(outcome, "text", "") or ""), job.shot_order,
                    )
                    if not text:
                        job.status = "failed"
                        job.error = f"{job.agent_name} sent back no document"
                    else:
                        job.status = "done"
                        job.result_markdown = text
                        job.result_look = {
                            "text_color": look.text_color,
                            "heading_color": look.heading_color,
                            "font": look.font,
                        }
        except AgentUnavailable as exc:
            with self._lock:
                job.status = "failed"
                job.error = str(exc)
                job.updated_at = time.monotonic()
        except Exception as exc:
            logger.warning("Remote walkthrough write-up failed: %s", type(exc).__name__)
            with self._lock:
                job.status = "failed"
                job.error = f"{job.agent_name} failed ({type(exc).__name__})"
                job.updated_at = time.monotonic()
        finally:
            if job.stage_dir:
                shutil.rmtree(job.stage_dir, ignore_errors=True)
                with self._lock:
                    job.stage_dir = ""

    def _handle_walkthrough_status(self, header: dict, payload: bytes, device: dict) -> dict:
        req = protocol.merge_json_payload(header, payload)
        job_id = _safe_job_id(req.get("job_id"))
        device_id = str(device.get("id") or "")
        with self._lock:
            self._prune_writeup_jobs_locked()
            job = self._writeup_jobs.get(job_id)
            if job is None or job.device_id != device_id:
                raise LookupError("Unknown walkthrough write-up job.")
            job.updated_at = time.monotonic()
            return self._writeup_status_dict(job)

    def _handle_walkthrough_cancel(self, header: dict, payload: bytes, device: dict) -> dict:
        req = protocol.merge_json_payload(header, payload)
        job_id = _safe_job_id(req.get("job_id"))
        device_id = str(device.get("id") or "")
        stage_to_clean = ""
        with self._lock:
            job = self._writeup_jobs.get(job_id)
            if job is None or job.device_id != device_id:
                raise LookupError("Unknown walkthrough write-up job.")
            job.cancel_event.set()
            job.updated_at = time.monotonic()
            if job.status == "staging":
                job.status = "canceled"
                stage_to_clean, job.stage_dir = job.stage_dir, ""
            status = job.status
        if stage_to_clean:
            shutil.rmtree(stage_to_clean, ignore_errors=True)
        return {"job_id": job_id, "canceled": True, "status": status}

    @staticmethod
    def _writeup_status_dict(job: _WriteUpJob) -> dict:
        data: dict[str, Any] = {
            "job_id": job.job_id,
            "status": job.status,
            "agent": job.agent,
            "agent_name": job.agent_name,
        }
        if job.status == "done":
            data["markdown"] = job.result_markdown
            data["look"] = dict(job.result_look)
            data["host_ms"] = job.host_ms
        elif job.status == "failed":
            data["error"] = job.error
        return data

    # ---- agent_ask / agent_ask_cancel ----

    def _choose_ask_agent(self, requested: str, settings: dict[str, Any]) -> str:
        if requested and requested != MeetingAgentCore.REMOTE_HOST:
            if requested not in MeetingAgentCore.INSTALLED:
                raise ValueError("Choose Claude Code, Codex or OpenCode.")
            return requested
        if self._agent_ask_runner is not None:
            preferred = resolve_walkthrough_write_up_agent(settings)
            if preferred in MeetingAgentCore.INSTALLED:
                return preferred
            return MeetingAgentCore.CLAUDE_CODE

        from services import installed_agents

        preferred = resolve_walkthrough_write_up_agent(settings)
        candidates = list(dict.fromkeys([
            *(c for c in (preferred,) if c in MeetingAgentCore.INSTALLED),
            *MeetingAgentCore.INSTALLED,
        ]))
        for candidate in candidates:
            agent = installed_agents.find_agent(candidate)
            if agent is not None and agent.usable:
                return candidate
        raise protocol.RemoteServiceError(
            f"No installed coding agent is available on {self._host_name}.",
            code="unavailable",
        )

    def _handle_agent_ask(
        self,
        header: dict,
        payload: bytes,
        device: dict,
        cancel_event: threading.Event | None,
    ) -> dict:
        settings = self._settings()
        if not remote_settings.host_agent_ask(settings):
            raise PermissionError(
                f"{self._host_name} isn't sharing coding-agent quick answers. "
                "Turn it on in Settings → Remote engine there."
            )
        req = protocol.merge_json_payload(header, payload)
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Question text must be a non-empty string.")
        if len(text) > MAX_ASK_CHARS:
            raise protocol.RemoteServiceError(
                f"Question exceeds the host limit of {MAX_ASK_CHARS:,} characters.",
                code="too_large",
            )
        requested_agent = req.get("agent") or ""
        if not isinstance(requested_agent, str):
            raise ValueError("Invalid agent identifier.")
        model = req.get("model") or ""
        if not isinstance(model, str):
            raise ValueError("Invalid model name.")
        if model and not _MODEL_NAME_RE.fullmatch(model):
            raise ValueError("Invalid model name.")
        timeout_s = _bounded_timeout(
            req.get("timeout_s"), DEFAULT_ASK_TIMEOUT_S, 1.0, MAX_ASK_TIMEOUT_S,
        )
        raw_ask_id = req.get("ask_id")
        ask_id = _safe_job_id(raw_ask_id, "ask_id") if raw_ask_id is not None else uuid.uuid4().hex[:16]
        device_id = str(device.get("id") or "")

        agent_id = self._choose_ask_agent(requested_agent, settings)
        combined_cancel = threading.Event()
        stop_bridge = threading.Event()

        def bridge_cancel() -> None:
            while not stop_bridge.wait(0.05):
                if (cancel_event is not None and cancel_event.is_set()) or self._shutdown:
                    combined_cancel.set()
                    return

        if cancel_event is not None:
            if cancel_event.is_set():
                combined_cancel.set()
            else:
                threading.Thread(
                    target=bridge_cancel, name=f"remote-ask-cancel-{ask_id[:8]}", daemon=True,
                ).start()

        with self._lock:
            self._active_asks[ask_id] = (device_id, combined_cancel)
        started = time.perf_counter()
        try:
            ask_runner: Callable[..., Any]
            if self._agent_ask_runner is not None:
                ask_runner = self._agent_ask_runner
            else:
                from services.actions.agents import ask

                ask_runner = ask

            result = ask_runner(
                agent_id, text, model=model, timeout_s=timeout_s, cancel=combined_cancel,
            )
        finally:
            stop_bridge.set()
            with self._lock:
                self._active_asks.pop(ask_id, None)

        host_ms = round((time.perf_counter() - started) * 1000, 1)
        return {
            "ok": bool(getattr(result, "ok", False)),
            "output": str(getattr(result, "output", "") or ""),
            "message": str(getattr(result, "message", "") or ""),
            "stopped": bool(getattr(result, "stopped", False)),
            "agent": agent_id,
            "ask_id": ask_id,
            "host_ms": host_ms,
        }

    def _handle_agent_ask_cancel(self, header: dict, payload: bytes, device: dict) -> dict:
        req = protocol.merge_json_payload(header, payload)
        ask_id = _safe_job_id(req.get("ask_id"), "ask_id")
        device_id = str(device.get("id") or "")
        with self._lock:
            entry = self._active_asks.get(ask_id)
            if entry is None or entry[0] != device_id:
                return {"ask_id": ask_id, "canceled": False}
            entry[1].set()
        return {"ask_id": ask_id, "canceled": True}

    # ---- shutdown ----

    def shutdown(self) -> None:
        """Cancel all in-flight jobs and remove any staged temp folders."""
        dirs_to_clean: list[str] = []
        with self._lock:
            self._shutdown = True
            for _owner, cancel in self._active_asks.values():
                cancel.set()
            for job in self._writeup_jobs.values():
                job.cancel_event.set()
                if job.status != "running" and job.stage_dir:
                    dirs_to_clean.append(job.stage_dir)
                    job.stage_dir = ""
        for path in dirs_to_clean:
            shutil.rmtree(path, ignore_errors=True)
