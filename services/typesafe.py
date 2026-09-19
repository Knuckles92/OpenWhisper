"""Shared TypeSafe (System One) client for fast typed judgments.

TypeSafe is a remote decision service, not a text generator. A caller sends
explicit ``state`` plus one or more narrow ``questions`` and receives typed
answers: a Noul probability of yes, a Choice with a probability distribution
and a confidence, or a Score over ordered levels. This module owns the
credential, the pinned model, HTTP transport, response validation and the
failure policy, so that every feature built on it degrades to "no judgment"
instead of raising into capture, scheduler or UI code.

Text sent here leaves the machine. Every caller must be gated by the
``typesafe_enabled`` setting and, inside a meeting, by that meeting's cloud
consent. Nothing in this module reads settings implicitly except
:func:`judge_from_settings`.

Measured on September 18, 2026 against AMI human labels: median request
latency 0.14 to 0.17 s, p95 under 0.3 s, roughly 1,300 input tokens per
judgment at $0.042 per million (output tokens are free).
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from services.credentials import resolve_credential
from services.http_tls import verified_context
from services.settings import resolve_typesafe_enabled

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
#: Pinned rather than ``jev-latest``: thresholds below were measured on it.
MODEL = "jev-1.13.0"
CREDENTIAL_ENV = "TYPESAFE_API_KEY"
#: Long enough for the p95 plus a slow network, short enough that a stalled
#: judgment cannot hold a worker thread for a noticeable stretch.
DEFAULT_TIMEOUT_S = 4.0
#: Bound on the JSON body so a runaway caller cannot exceed the documented
#: 32k-token state budget or ship a whole transcript by accident.
MAX_STATE_CHARS = 24_000
#: Space warnings out so a dead network does not flood the log.
_WARN_SPACING_S = 60.0

#: A transport takes the JSON payload and returns ``(status, body_text)``.
Transport = Callable[[Dict[str, Any], float], Tuple[int, str]]


class TypeSafeError(RuntimeError):
    """Malformed request or response; never carries the API key."""


@dataclass(frozen=True)
class ChoiceAnswer:
    """One Choice answer: the selected option and how concentrated the distribution is."""

    choice: str
    confidence: float
    probabilities: Mapping[str, float] = field(default_factory=dict)


@dataclass
class JudgeUsage:
    requests: int = 0
    failures: int = 0
    input_tokens: int = 0
    last_latency_s: float = 0.0


def _http_post(payload: Dict[str, Any], timeout_s: float, *, api_key: str,
               endpoint: str) -> Tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        endpoint, data=body, method="POST",
        headers={
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "OpenWhisper",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout_s, context=verified_context(),
        ) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # The body may echo the request; keep only the status.
        return exc.code, ""


def validate_answers(body: Any, questions: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Check that the response answers exactly the asked questions with the right types."""
    if not isinstance(body, Mapping) or not isinstance(body.get("answers"), Mapping):
        raise TypeSafeError("response has no answers")
    answers = body["answers"]
    if set(answers) != set(questions):
        raise TypeSafeError("response question ids do not match the request")
    checked: Dict[str, Dict[str, Any]] = {}
    for name, question in questions.items():
        answer = answers[name]
        kind = question.get("type")
        if not isinstance(answer, Mapping) or answer.get("type") != kind:
            raise TypeSafeError(f"answer type mismatch on {name}")
        if kind == "noul":
            value = answer.get("noul")
            if not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
                raise TypeSafeError(f"invalid probability on {name}")
            checked[name] = {"type": "noul", "noul": float(value)}
        elif kind == "choice":
            choice = answer.get("choice")
            if choice not in question.get("criteria", {}):
                raise TypeSafeError(f"unknown choice on {name}")
            confidence = answer.get("confidence", 0.0)
            if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
                raise TypeSafeError(f"invalid confidence on {name}")
            probabilities = answer.get("probabilities") or {}
            if not isinstance(probabilities, Mapping):
                raise TypeSafeError(f"invalid probabilities on {name}")
            checked[name] = {
                "type": "choice", "choice": str(choice), "confidence": float(confidence),
                "probabilities": {str(k): float(v) for k, v in probabilities.items()
                                  if isinstance(v, (int, float))},
            }
        elif kind == "score":
            score = answer.get("score")
            levels = len(question.get("criteria") or [])
            if not isinstance(score, (int, float)) or not 0.0 <= float(score) <= max(0, levels - 1):
                raise TypeSafeError(f"invalid score on {name}")
            confidence = answer.get("confidence", 0.0)
            checked[name] = {
                "type": "score", "score": float(score),
                "confidence": float(confidence) if isinstance(confidence, (int, float)) else 0.0,
            }
        else:
            raise TypeSafeError(f"unsupported question type on {name}")
    return checked


class TypeSafeJudge:
    """Ask narrow typed questions; every failure returns ``None`` and is logged sparsely.

    Instances are safe to share between threads: each call is one blocking
    HTTP round trip with its own timeout, and usage counters are locked.
    """

    def __init__(self, api_key: str, *, model: str = MODEL,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 endpoint: str = ENDPOINT,
                 transport: Optional[Transport] = None,
                 monotonic: Optional[Callable[[], float]] = None) -> None:
        if not api_key:
            raise ValueError("TypeSafe API key is required")
        self._api_key = api_key
        self.model = model
        self.timeout_s = float(timeout_s)
        self.endpoint = endpoint
        self._transport = transport
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._last_warn_mono = -_WARN_SPACING_S
        self.usage = JudgeUsage()

    # -- public API ---------------------------------------------------------

    def ask(self, state: Any, questions: Mapping[str, Mapping[str, Any]],
            timeout_s: Optional[float] = None) -> Optional[Dict[str, Dict[str, Any]]]:
        """Return validated answers keyed by question id, or ``None`` on any failure."""
        if not questions:
            return None
        payload = {"model": self.model, "state": state, "questions": dict(questions)}
        try:
            encoded = json.dumps(payload)
        except (TypeError, ValueError):
            self._warn("TypeSafe state is not JSON-serializable; skipping judgment")
            return None
        if len(encoded) > MAX_STATE_CHARS:
            self._warn("TypeSafe request of %d chars exceeds the %d bound; skipping",
                       len(encoded), MAX_STATE_CHARS)
            return None
        started = self._monotonic()
        try:
            if self._transport is not None:
                status, text = self._transport(payload, timeout_s or self.timeout_s)
            else:
                status, text = _http_post(payload, timeout_s or self.timeout_s,
                                          api_key=self._api_key, endpoint=self.endpoint)
        except Exception as exc:  # network, timeout, TLS
            self._record(started, ok=False)
            self._warn("TypeSafe request failed: %s", type(exc).__name__)
            return None
        if status != 200:
            self._record(started, ok=False)
            self._warn("TypeSafe returned HTTP %s", status)
            return None
        try:
            body = json.loads(text)
            answers = validate_answers(body, questions)
        except (ValueError, TypeSafeError) as exc:
            self._record(started, ok=False)
            self._warn("TypeSafe response rejected: %s", exc)
            return None
        usage = body.get("usage") if isinstance(body, Mapping) else None
        tokens = usage.get("input_tokens", 0) if isinstance(usage, Mapping) else 0
        self._record(started, ok=True, tokens=tokens if isinstance(tokens, int) else 0)
        return answers

    def noul(self, state: Any, instructions: Any,
             criteria: Optional[Mapping[str, str]] = None) -> Optional[float]:
        """Probability that the yes/no ``instructions`` hold for ``state``."""
        question: Dict[str, Any] = {"type": "noul", "instructions": instructions}
        if criteria:
            question["criteria"] = dict(criteria)
        answers = self.ask(state, {"q": question})
        return None if answers is None else answers["q"]["noul"]

    def choice(self, state: Any, instructions: Any,
               criteria: Mapping[str, str]) -> Optional[ChoiceAnswer]:
        """Select one option of ``criteria`` for ``state``."""
        answers = self.ask(state, {"q": {"type": "choice", "instructions": instructions,
                                         "criteria": dict(criteria)}})
        if answers is None:
            return None
        answer = answers["q"]
        return ChoiceAnswer(answer["choice"], answer["confidence"], answer["probabilities"])

    # -- internals ------------------------------------------------------------

    def _record(self, started: float, *, ok: bool, tokens: int = 0) -> None:
        with self._lock:
            self.usage.requests += 1
            if not ok:
                self.usage.failures += 1
            self.usage.input_tokens += tokens
            self.usage.last_latency_s = self._monotonic() - started

    def _warn(self, message: str, *args: Any) -> None:
        now = self._monotonic()
        with self._lock:
            if now - self._last_warn_mono < _WARN_SPACING_S:
                logger.debug(message, *args)
                return
            self._last_warn_mono = now
        logger.warning(message, *args)


def is_configured(settings: Optional[Dict[str, Any]] = None) -> bool:
    """True when the master switch is on and a key can be resolved."""
    return resolve_typesafe_enabled(settings) and bool(resolve_credential(CREDENTIAL_ENV))


def judge_from_settings(settings: Optional[Dict[str, Any]] = None,
                        **kwargs: Any) -> Optional[TypeSafeJudge]:
    """Build a judge when TypeSafe is enabled and a key exists, else ``None``."""
    if not resolve_typesafe_enabled(settings):
        return None
    key = resolve_credential(CREDENTIAL_ENV)
    if not key:
        logger.debug("TypeSafe enabled but %s is not set", CREDENTIAL_ENV)
        return None
    return TypeSafeJudge(key, **kwargs)


# -- dictation-side question -----------------------------------------------------

#: Benchmarked September 18, 2026 on 884 real meeting segments plus authored
#: positives: AUROC 0.9995, recall 0.96 and a 0.11% false-positive rate at 0.8.
SENSITIVE_CONTENT_INSTRUCTIONS = (
    "Does `text` contain information that should not be sent to a third-party "
    "cloud service without review: passwords, keys or credentials; government, "
    "bank or card numbers; health, disciplinary, salary or home-address details "
    "about an identifiable person; or an explicit statement that the content is "
    "confidential or privileged? Ordinary technical or business discussion is "
    "not sensitive."
)
SENSITIVE_CONTENT_THRESHOLD = 0.8
#: Dictation can be long; the judgment needs the content, not every word.
_SENSITIVE_MAX_CHARS = 12_000


def sensitive_content_probability(judge: TypeSafeJudge, text: str) -> Optional[float]:
    """Probability that ``text`` holds credentials, identifiers or personal details."""
    excerpt = (text or "").strip()
    if not excerpt:
        return None
    if len(excerpt) > _SENSITIVE_MAX_CHARS:
        excerpt = excerpt[:_SENSITIVE_MAX_CHARS]
    return judge.noul({"text": excerpt}, SENSITIVE_CONTENT_INSTRUCTIONS)
