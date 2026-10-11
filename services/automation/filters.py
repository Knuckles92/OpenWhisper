"""Text filters: outside programs and services that change dictated text.

A filter sees the words of a live dictation or action at one stage
(``before_cleanup`` or ``after_cleanup``) and answers with new text, no
change, or "deliver nothing". Saved filters (Settings > Hooks) run first, in
their saved order, then filters leased over the live API, in the order they
were registered.

Request: ``{"v":1,"stage","job_id","mode","action_id","text","language","app"}``
as JSON (POSTed to an ``http`` filter; on stdin, with a newline, for a
``program`` filter). Reply: ``{"text": "…"}`` replaces the text, ``{}``, an
empty body or HTTP 204 keeps it, ``{"cancel": true}`` delivers nothing (so
does an empty ``text``). Anything else (an error, a timeout, a reply that
isn't a JSON object or has the wrong types) keeps the text and publishes
``filter.failed`` without the text.

A filter has its own time limit and a stage has ``STAGE_BUDGET_S`` in all;
filters left when the budget is spent are skipped. Dictated text is never
logged. Contract: docs/design/automation/design.md.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional
from urllib.parse import urlsplit

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

LEASE_TTL_S = 60.0
#: Live leases at most.
MAX_LEASES = 10
#: All the filters of one stage, together.
STAGE_BUDGET_S = 3.0
#: A filter isn't started with less time than this left.
MIN_SLICE_S = 0.05
#: Job modes filters apply to (never Command Mode, transforms or files).
MODES: tuple[str, ...] = ("dictation", "action")
#: Hosts a leased filter may listen on.
LOOPBACK_HOSTS: tuple[str, ...] = ("127.0.0.1", "::1", "localhost")
#: Most of a reply that is read.
MAX_REPLY_BYTES = 1024 * 1024
REQUEST_VERSION = 1


@dataclass(frozen=True)
class FilterOutcome:
    text: str
    canceled: bool = False
    changed: bool = False


@dataclass(frozen=True)
class Lease:
    id: str
    url: str
    stage: str
    timeout_ms: int
    modes: tuple[str, ...]
    #: Wall-clock time (``time.time()``) it lapses unless renewed.
    expires_at: float


# -- leases -----------------------------------------------------------------------

_lease_lock = threading.Lock()
_leases: dict[str, Lease] = {}
#: ``time.monotonic()`` each lease lapses at; immune to clock changes.
_lease_deadlines: dict[str, float] = {}


def _purge_expired() -> None:
    # Caller holds _lease_lock.
    now = time.monotonic()
    for lease_id in [i for i, at in _lease_deadlines.items() if at <= now]:
        del _lease_deadlines[lease_id]
        del _leases[lease_id]


def _lease_timeout(timeout_ms: Any) -> int:
    from services.automation.models import FILTER_TIMEOUT_MAX_MS, FILTER_TIMEOUT_MIN_MS

    if isinstance(timeout_ms, float) and timeout_ms.is_integer():
        timeout_ms = int(timeout_ms)
    if (isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int)
            or not FILTER_TIMEOUT_MIN_MS <= timeout_ms <= FILTER_TIMEOUT_MAX_MS):
        raise ValueError(
            f"Use a timeout_ms from {FILTER_TIMEOUT_MIN_MS} to {FILTER_TIMEOUT_MAX_MS}."
        )
    return timeout_ms


def _lease_modes(modes: Any) -> tuple[str, ...]:
    if isinstance(modes, (str, bytes)) or not isinstance(modes, Sequence):
        raise ValueError("Give modes as a list: dictation, action or both.")
    cleaned = tuple(dict.fromkeys(modes))
    if not cleaned or any(m not in MODES for m in cleaned):
        raise ValueError("Give modes as a list: dictation, action or both.")
    return cleaned


def register_lease(url: str, stage: str, *, timeout_ms: int = 1500,
                   modes: Sequence[str] = MODES) -> Lease:
    """Lease a filter on this computer for ``LEASE_TTL_S`` seconds; thread-safe.

    Raises:
        ValueError: user-facing copy, when the URL is not http(s) on
            127.0.0.1, ::1 or localhost, the stage, modes or timeout is not
            valid, or ``MAX_LEASES`` are already live.
    """
    from services.automation.models import STAGES, validate_url

    url = url.strip() if isinstance(url, str) else ""
    validate_url(url)
    host = (urlsplit(url).hostname or "").lower()
    if host not in LOOPBACK_HOSTS:
        raise ValueError(
            "A filter added over the API must listen on this computer "
            "(127.0.0.1, ::1 or localhost)."
        )
    if stage not in STAGES:
        raise ValueError(f"Use the stage {' or '.join(STAGES)}.")
    timeout = _lease_timeout(timeout_ms)
    cleaned_modes = _lease_modes(modes)
    with _lease_lock:
        _purge_expired()
        if len(_leases) >= MAX_LEASES:
            raise ValueError(
                f"{MAX_LEASES} filters are already registered. Remove one first."
            )
        lease = Lease("flt_" + secrets.token_hex(8), url, stage, timeout, cleaned_modes,
                      time.time() + LEASE_TTL_S)
        _leases[lease.id] = lease
        _lease_deadlines[lease.id] = time.monotonic() + LEASE_TTL_S
    return lease


def renew_lease(lease_id: str) -> Lease:
    """Extend a live lease by ``LEASE_TTL_S`` from now.

    Raises:
        KeyError: it expired, was dropped, or never existed.
    """
    with _lease_lock:
        _purge_expired()
        lease = _leases.get(lease_id)
        if lease is None:
            raise KeyError(lease_id)
        renewed = Lease(lease.id, lease.url, lease.stage, lease.timeout_ms, lease.modes,
                        time.time() + LEASE_TTL_S)
        _leases[lease_id] = renewed
        _lease_deadlines[lease_id] = time.monotonic() + LEASE_TTL_S
    return renewed


def drop_lease(lease_id: str) -> bool:
    """Forget a lease; False when it was not live."""
    with _lease_lock:
        _purge_expired()
        _lease_deadlines.pop(lease_id, None)
        return _leases.pop(lease_id, None) is not None


def drop_all_leases() -> int:
    """Forget every lease (the live API stopped); returns how many were live."""
    with _lease_lock:
        _purge_expired()
        count = len(_leases)
        _leases.clear()
        _lease_deadlines.clear()
    return count


def leases() -> list[Lease]:
    """Live leases, oldest registration first."""
    with _lease_lock:
        _purge_expired()
        return list(_leases.values())


# -- running filters --------------------------------------------------------------

@dataclass(frozen=True)
class _Step:
    id: str
    name: str
    kind: str
    url: str
    argv: tuple[str, ...]
    timeout_s: float


@dataclass(frozen=True)
class _Verdict:
    """What one filter said: keep, replace (with ``text``), cancel, or failed."""

    kind: str
    text: str = ""
    reason: str = ""


_KEEP, _REPLACE, _CANCEL, _FAILED = "keep", "replace", "cancel", "failed"


def _interpret(raw: bytes) -> _Verdict:
    if not raw.strip():
        return _Verdict(_KEEP)
    try:
        reply = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _Verdict(_FAILED, reason="The reply wasn't JSON")
    if not isinstance(reply, dict):
        return _Verdict(_FAILED, reason="The reply wasn't a JSON object")
    cancel = reply.get("cancel", False)
    if not isinstance(cancel, bool):
        return _Verdict(_FAILED, reason='"cancel" wasn\'t true or false')
    if cancel:
        return _Verdict(_CANCEL)
    if "text" not in reply:
        return _Verdict(_KEEP)
    if not isinstance(reply["text"], str):
        return _Verdict(_FAILED, reason='"text" wasn\'t a string')
    return _Verdict(_REPLACE, text=reply["text"])


_clients_lock = threading.Lock()
_clients: dict[bool, Any] = {}


def _client(private: bool) -> httpx.Client:
    """A shared client per kind of address, kept for the app's lifetime."""
    from services.automation.webhooks import _new_client

    with _clients_lock:
        if private not in _clients:
            _clients[private] = _new_client(private)
        return _clients[private]


def _post(url: str, body: bytes, timeout_s: float) -> _Verdict:
    from services.automation.models import url_is_private
    from services.automation.webhooks import CONTENT_TYPE, _exchange

    result = _exchange(
        _client(url_is_private(url)), url, body, {"Content-Type": CONTENT_TYPE},
        timeout_s=timeout_s, read_limit=MAX_REPLY_BYTES,
        deadline=time.monotonic() + timeout_s,
    )
    if not result.ok:
        return _Verdict(_FAILED, reason=result.error)
    if result.status == 204:
        return _Verdict(_KEEP)
    return _interpret(result.body)


def _run_http(url: str, body: bytes, timeout_s: float) -> _Verdict:
    """POST on a helper thread, waiting no longer than ``timeout_s``."""
    future: Future = Future()

    def work() -> None:
        try:
            future.set_result(_post(url, body, timeout_s))
        except Exception as exc:
            future.set_result(_Verdict(_FAILED, reason=f"The request failed ({type(exc).__name__})"))

    threading.Thread(target=work, name="text-filter", daemon=True).start()
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeout:
        from services.automation.webhooks import no_answer_copy

        return _Verdict(_FAILED, reason=no_answer_copy(timeout_s))


def _run_program(argv: Sequence[str], body: bytes, timeout_s: float) -> _Verdict:
    from services.automation.programs import run_once

    code, stdout, error = run_once(argv, body + b"\n", timeout_s=timeout_s)
    if error:
        return _Verdict(_FAILED, reason=error)
    if code != 0:
        return _Verdict(_FAILED, reason=f"Exited with code {code}")
    return _interpret(stdout)


def _run_step(step: _Step, body: bytes, timeout_s: float) -> _Verdict:
    from services.automation.models import FILTER_HTTP

    if step.kind == FILTER_HTTP:
        return _run_http(step.url, body, timeout_s)
    return _run_program(step.argv, body, timeout_s)


def _failed(step: _Step, stage: str, reason: str) -> None:
    from services.automation import catalog, events

    logger.info("Text filter %s at %s kept the text: %s", step.id, stage, reason)
    events.publish(catalog.FILTER_FAILED, catalog.filter_failed(step.id, step.name, stage, reason))


def _steps(stage: str, mode: str, settings: Mapping) -> list[_Step]:
    from services.automation import permissions
    from services.automation.models import FILTER_HTTP, load_filters

    steps = [
        _Step(f.id, f.name, f.kind, f.url, (f.program, *f.args), f.timeout_ms / 1000)
        for f in load_filters(settings) if f.enabled and f.stage == stage
    ]
    if permissions.granted(settings, permissions.TEXT_FILTERS):
        steps.extend(
            _Step(lease.id, "API filter", FILTER_HTTP, lease.url, (), lease.timeout_ms / 1000)
            for lease in leases() if lease.stage == stage and mode in lease.modes
        )
    return steps


def _context(job, settings: Mapping) -> tuple[Optional[str], Optional[dict]]:
    """The job's language and app, from what is known now (never waits)."""
    try:
        from services import dictation_pipeline
        from services.automation.catalog import app_from_fields

        fields = dictation_pipeline.history_fields(job, None, live=True, settings=settings)
    except Exception:
        logger.debug("Filter context unavailable", exc_info=True)
        return None, None
    return fields.get("language") or None, app_from_fields(fields)


def _load_settings() -> Mapping:
    from services import settings

    return settings.settings_manager.load_all_settings() or {}


def apply(stage: str, text: str, job, settings: Optional[Mapping] = None) -> FilterOutcome:
    """Run the filters for ``stage`` on a live dictation or action; worker thread.

    Returns ``FilterOutcome(text)`` at once when none apply: a job of None,
    Command Mode, transforms, no enabled saved filter for the stage and no
    live lease (leases also need the text filters permission). Never raises
    for a filter's sake: a failing filter keeps the text.
    """
    mode = getattr(job, "mode", None) if job is not None else None
    if mode not in MODES or not isinstance(text, str):
        return FilterOutcome(text)
    if settings is None:
        settings = _load_settings()
    steps = _steps(stage, mode, settings)
    if not steps:
        return FilterOutcome(text)
    deadline = time.monotonic() + STAGE_BUDGET_S
    language, app = _context(job, settings)
    request = {
        "v": REQUEST_VERSION,
        "stage": stage,
        "job_id": getattr(job, "id", "") or "",
        "mode": mode,
        "action_id": getattr(job, "action_id", "") or None,
        "text": text,
        "language": language,
        "app": app,
    }
    current = text
    for index, step in enumerate(steps):
        remaining = deadline - time.monotonic()
        if remaining < MIN_SLICE_S:
            for skipped in steps[index:]:
                _failed(skipped, stage,
                        f"Skipped: this stage's {STAGE_BUDGET_S:g} s for filters ran out")
            break
        request["text"] = current
        body = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            verdict = _run_step(step, body, min(step.timeout_s, remaining))
        except Exception as exc:
            verdict = _Verdict(_FAILED, reason=f"The filter couldn't run ({type(exc).__name__})")
        if verdict.kind == _FAILED:
            _failed(step, stage, verdict.reason)
        elif verdict.kind == _CANCEL or (verdict.kind == _REPLACE and not verdict.text.strip()):
            return FilterOutcome(current, canceled=True, changed=current != text)
        elif verdict.kind == _REPLACE:
            current = verdict.text
    return FilterOutcome(current, changed=current != text)
