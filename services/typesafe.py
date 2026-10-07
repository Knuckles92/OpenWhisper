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
consent. Nothing in this module reads settings implicitly except the
``*_from_settings`` helpers and :func:`key_present`.

The same pinned model is reachable two ways (:class:`JudgeRoute`): directly
at TypeSafe with a TypeSafe key, or through OpenRouter's System One API with
the OpenRouter key the text features already use. OpenRouter implements
TypeSafe's request and response shapes, so only the endpoint, key, model id
and an attribution header differ.

Measured on September 18, 2026 against AMI human labels: median request
latency 0.14 to 0.17 s, p95 under 0.3 s, roughly 1,300 input tokens per
judgment at $0.042 per million (output tokens are free).
"""
from __future__ import annotations

import http.client
import json
import logging
import select
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from services.credentials import resolve_credential
from services.http_tls import verified_context
from services.settings import (
    TypeSafeProvider,
    resolve_typesafe_enabled,
    resolve_typesafe_provider,
)
from services.text_llm import OPENROUTER_PROFILE_ID, builtin_profile, provider_headers

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
#: Pinned rather than ``jev-latest``: thresholds below were measured on it.
MODEL = "jev-1.13.0"
#: OpenRouter's id for the same 1.13 release. Its versions have no patch
#: digit; responses name the dated snapshot (``typesafe/jev-1.13-20260917``).
#: Prefixing ``MODEL`` would ask for ``typesafe/jev-1.13.0``, which it lacks.
OPENROUTER_MODEL = "typesafe/jev-1.13"
CREDENTIAL_ENV = "TYPESAFE_API_KEY"


@dataclass(frozen=True)
class JudgeRoute:
    """One way to reach the pinned model: endpoint, key, and model id there."""

    id: str
    name: str
    endpoint: str
    model: str
    credential_env: str
    #: Sent with every request on top of auth and content headers.
    headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return urllib.parse.urlsplit(self.endpoint).hostname or self.endpoint

    @property
    def key_label(self) -> str:
        return f"{self.name} API key"

    @property
    def key_phrase(self) -> str:
        """``key_label`` with its article, for "Add …" copy."""
        article = "an" if self.name[:1].lower() in "aeiou" else "a"
        return f"{article} {self.key_label}"


TYPESAFE_ROUTE = JudgeRoute(
    id=TypeSafeProvider.TYPESAFE,
    name="TypeSafe",
    endpoint=ENDPOINT,
    model=MODEL,
    credential_env=CREDENTIAL_ENV,
)


def _openrouter_route() -> JudgeRoute:
    # Built from the OpenRouter text profile so the base URL, key variable and
    # attribution header cannot drift from the ones cleanup and AI insights use.
    profile = builtin_profile(OPENROUTER_PROFILE_ID)
    return JudgeRoute(
        id=TypeSafeProvider.OPENROUTER,
        name="OpenRouter",
        endpoint=profile.base_url.rstrip("/") + "/systemone",
        model=OPENROUTER_MODEL,
        credential_env=profile.api_key_env,
        headers=provider_headers(profile) or {},
    )


OPENROUTER_ROUTE = _openrouter_route()
ROUTES: Dict[str, JudgeRoute] = {route.id: route for route in (TYPESAFE_ROUTE, OPENROUTER_ROUTE)}

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


#: Idle connections kept per origin; meeting mode judges from a few threads.
_POOL_MAX_IDLE = 4
#: How long an idle connection stays eligible for reuse. The server held an
#: idle connection open for over 7 minutes (September 22, 2026), so the risk
#: is a NAT or firewall silently forgetting it: the next judgment would stall
#: until its timeout and then come back as no judgment at all.
#: Browsers keep idle connections for about two to five minutes.
_POOL_IDLE_S = 115.0


def _reused_wait_s(timeout_s: float) -> float:
    """How long a reused connection may stay silent before it is given up.

    A connection a NAT or firewall silently forgot still accepts the request
    and then never answers. Judgments take 0.14-0.17 s median and under 0.3 s
    at p95, so a reused connection gets half the budget (at least a second)
    and the judgment goes out again on a fresh connection with what is left,
    instead of the whole wait ending in no judgment at all.
    """
    return min(timeout_s, max(1.0, timeout_s / 2))


#: A fresh connection's TCP and TLS handshake alone takes about 100 ms here,
#: so a resend with less budget than this could not be answered in time.
_FRESH_ATTEMPT_MIN_S = 0.1

_Origin = Tuple[str, str, int]


def _still_open(conn: http.client.HTTPConnection) -> bool:
    """True when the peer has not closed an idle keep-alive connection.

    Between requests the server has nothing to send, so a readable socket
    means EOF or a reset, never an answer.
    """
    sock = conn.sock
    if sock is None:
        return False
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return False
    return not readable


class _KeepAlivePool:
    """Idle HTTP/1.1 connections per origin, shared by every judge in the process.

    urllib sends ``Connection: close``, so every judgment paid a fresh TCP and
    TLS handshake: 207 ms median against 112 ms over a reused connection (ten
    sequential judgments, September 22, 2026). Several callers build
    short-lived judges, so the pool lives at module level rather than on a
    judge. A connection serves one request at a time; the lock guards the
    idle lists, never a round trip.
    """

    def __init__(self, *, max_idle: int = _POOL_MAX_IDLE, idle_s: float = _POOL_IDLE_S,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._max_idle = max_idle
        self._idle_s = idle_s
        self._monotonic = monotonic
        self._idle: Dict[_Origin, List[Tuple[http.client.HTTPConnection, float]]] = {}
        self._lock = threading.Lock()

    def acquire(self, origin: _Origin, timeout_s: float) -> Tuple[http.client.HTTPConnection, bool]:
        """Return ``(connection, reused)``, preferring the most recent live idle one."""
        while True:
            with self._lock:
                idle = self._idle.get(origin)
                if not idle:
                    break
                conn, released = idle.pop()
            if self._monotonic() - released < self._idle_s and _still_open(conn):
                conn.timeout = timeout_s
                conn.sock.settimeout(timeout_s)
                return conn, True
            conn.close()
        scheme, host, port = origin
        if scheme == "https":
            return http.client.HTTPSConnection(
                host, port, timeout=timeout_s, context=verified_context()), False
        return http.client.HTTPConnection(host, port, timeout=timeout_s), False

    def release(self, origin: _Origin, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            idle = self._idle.setdefault(origin, [])
            if len(idle) < self._max_idle:
                idle.append((conn, self._monotonic()))
                return
        conn.close()

    def discard(self, origin: _Origin) -> None:
        """Close every idle connection to ``origin``, e.g. after the server restarted."""
        with self._lock:
            idle = self._idle.pop(origin, [])
        for conn, _ in idle:
            conn.close()


_POOL = _KeepAlivePool()


def _proxied(parts: urllib.parse.SplitResult) -> bool:
    """True when the system routes this URL through a proxy.

    Checked per request, as urllib did, so a proxy change applies to the next
    judgment; the check costs about 45 us.
    """
    try:
        return (bool(urllib.request.getproxies().get(parts.scheme))
                and not urllib.request.proxy_bypass(parts.hostname or ""))
    except Exception:
        return True


def _urlopen_post(endpoint: str, body: bytes, headers: Dict[str, str],
                  timeout_s: float) -> Tuple[int, str]:
    """One-shot request through urllib, which knows how to tunnel through a proxy."""
    request = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(
            request, timeout=timeout_s, context=verified_context(),
        ) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # The body may echo the request; keep only the status.
        return exc.code, ""


def _http_post(payload: Dict[str, Any], timeout_s: float, *, api_key: str,
               endpoint: str, extra_headers: Optional[Mapping[str, str]] = None,
               ) -> Tuple[int, str]:
    body = json.dumps(payload).encode("utf-8")
    headers = {
        **(extra_headers or {}),
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "OpenWhisper",
    }
    parts = urllib.parse.urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.hostname or _proxied(parts):
        return _urlopen_post(endpoint, body, headers, timeout_s)
    origin = (parts.scheme, parts.hostname,
              parts.port or (443 if parts.scheme == "https" else 80))
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    deadline = time.monotonic() + timeout_s
    budget = timeout_s
    retried = False
    while True:
        conn, reused = _POOL.acquire(origin, budget)
        if reused:
            conn.timeout = _reused_wait_s(budget)
            conn.sock.settimeout(conn.timeout)
        try:
            conn.request("POST", target, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
        except (ConnectionError, ssl.SSLEOFError, TimeoutError):
            conn.close()
            if not reused or retried:
                raise
            # The server dropped an idle connection as this request went out,
            # or a NAT forgot it and it went silent, so nothing was answered.
            # A judgment has no side effects: send it once more on a fresh
            # connection within the remaining budget, and drop idle siblings
            # that probably died with it.
            _POOL.discard(origin)
            budget = deadline - time.monotonic()
            if budget < _FRESH_ATTEMPT_MIN_S:
                raise
            retried = True
            continue
        except BaseException:
            conn.close()
            raise
        if response.will_close:
            conn.close()
        else:
            _POOL.release(origin, conn)
        if not 200 <= response.status < 300:
            # The body may echo the request; keep only the status.
            return response.status, ""
        return response.status, data.decode("utf-8", "replace")


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
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
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


class _Primitives:
    """The convenience questions, built on whatever ``ask`` the class provides."""

    def ask(self, state: Any, questions: Mapping[str, Mapping[str, Any]],
            timeout_s: Optional[float] = None) -> Optional[Dict[str, Dict[str, Any]]]:
        raise NotImplementedError

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


class TypeSafeJudge(_Primitives):
    """Ask narrow typed questions; every failure returns ``None`` and is logged sparsely.

    Instances are safe to share between threads: each call is one blocking
    HTTP round trip with its own timeout on a connection no other thread is
    using, and usage counters are locked.
    """

    def __init__(self, api_key: str, *, route: JudgeRoute = TYPESAFE_ROUTE,
                 model: Optional[str] = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 endpoint: Optional[str] = None,
                 transport: Optional[Transport] = None,
                 monotonic: Optional[Callable[[], float]] = None) -> None:
        if not api_key:
            raise ValueError(f"{route.key_label} is required")
        self._api_key = api_key
        self.route = route
        self.model = model or route.model
        self.timeout_s = float(timeout_s)
        self.endpoint = endpoint or route.endpoint
        self._transport = transport
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._last_warn_mono = -_WARN_SPACING_S
        self._label = "TypeSafe" if route is TYPESAFE_ROUTE else f"TypeSafe via {route.name}"
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
            self._warn("%s state is not JSON-serializable; skipping judgment", self._label)
            return None
        if len(encoded) > MAX_STATE_CHARS:
            self._warn("%s request of %d chars exceeds the %d bound; skipping",
                       self._label, len(encoded), MAX_STATE_CHARS)
            return None
        started = self._monotonic()
        try:
            if self._transport is not None:
                status, text = self._transport(payload, timeout_s or self.timeout_s)
            else:
                status, text = _http_post(payload, timeout_s or self.timeout_s,
                                          api_key=self._api_key, endpoint=self.endpoint,
                                          extra_headers=self.route.headers)
        except Exception as exc:  # network, timeout, TLS
            self._record(started, ok=False)
            self._warn("%s request failed: %s", self._label, type(exc).__name__)
            return None
        if status != 200:
            self._record(started, ok=False)
            self._warn("%s returned HTTP %s", self._label, status)
            return None
        try:
            body = json.loads(text)
            answers = validate_answers(body, questions)
        except (ValueError, TypeSafeError) as exc:
            self._record(started, ok=False)
            self._warn("%s response rejected: %s", self._label, exc)
            return None
        usage = body.get("usage") if isinstance(body, Mapping) else None
        tokens = usage.get("input_tokens", 0) if isinstance(usage, Mapping) else 0
        self._record(started, ok=True, tokens=tokens if isinstance(tokens, int) else 0)
        return answers

    def same_connection(self, other: Any) -> bool:
        """True when ``other`` would send to the same place with the same key."""
        return (isinstance(other, TypeSafeJudge)
                and (self.endpoint, self.model, self._api_key)
                == (other.endpoint, other.model, other._api_key))

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


class FollowingJudge(_Primitives):
    """Ask through whichever judge ``provider`` returns at the moment of asking.

    Meeting workers live as long as the meeting. A worker holding one fixed
    client would keep sending excerpts over the route chosen when it started,
    even after Fast judgments were switched between TypeSafe and OpenRouter;
    this looks the route up per question instead.
    """

    def __init__(self, provider: Callable[[], Optional[TypeSafeJudge]]) -> None:
        self._provider = provider

    def ask(self, state: Any, questions: Mapping[str, Mapping[str, Any]],
            timeout_s: Optional[float] = None) -> Optional[Dict[str, Dict[str, Any]]]:
        judge = self._provider()
        if judge is None:
            return None
        if timeout_s is None:
            return judge.ask(state, questions)
        return judge.ask(state, questions, timeout_s=timeout_s)


def route_from_settings(settings: Optional[Dict[str, Any]] = None) -> JudgeRoute:
    """The route Settings → Fast judgments names, TypeSafe unless OpenRouter was chosen."""
    return ROUTES[resolve_typesafe_provider(settings)]


def key_present(settings: Optional[Dict[str, Any]] = None, *,
                route: Optional[JudgeRoute] = None) -> bool:
    """True when the chosen route's key resolves from the store, the environment or ``.env``.

    Separate from :func:`is_configured` so the UI can tell "switched off" from
    "switched on but unusable" — the two need different copy and a different
    next step. Pass ``route`` to ask about one route regardless of Settings.
    """
    route = route or route_from_settings(settings)
    return bool(resolve_credential(route.credential_env))


def is_configured(settings: Optional[Dict[str, Any]] = None) -> bool:
    """True when the master switch is on and the chosen route's key can be resolved."""
    return resolve_typesafe_enabled(settings) and key_present(settings)


def judge_from_settings(settings: Optional[Dict[str, Any]] = None,
                        **kwargs: Any) -> Optional[TypeSafeJudge]:
    """Build a judge on the chosen route when TypeSafe is enabled and its key exists, else ``None``."""
    if not resolve_typesafe_enabled(settings):
        return None
    route = route_from_settings(settings)
    key = resolve_credential(route.credential_env)
    if not key:
        logger.debug("TypeSafe enabled but %s is not set", route.credential_env)
        return None
    return TypeSafeJudge(key, route=route, **kwargs)


#: Host shown in verification copy, so the user knows who answered.
VERIFY_HOST = TYPESAFE_ROUTE.host
#: A manual test is a one-off, so it can wait longer than a live judgment.
VERIFY_TIMEOUT_S = 10.0
#: The smallest real judgment there is. It proves the key and the pinned model
#: in one round trip and carries nothing about the user.
_VERIFY_STATE = {"text": "ok"}
_VERIFY_QUESTIONS: Dict[str, Dict[str, Any]] = {
    "q": {"type": "noul", "instructions": "Is `text` non-empty?"}
}


def verify_key(api_key: str, *, route: JudgeRoute = TYPESAFE_ROUTE,
               timeout_s: float = VERIFY_TIMEOUT_S,
               endpoint: Optional[str] = None,
               transport: Optional[Transport] = None) -> Tuple[bool, str]:
    """Make one authenticated judgment with ``api_key`` over ``route`` and report the outcome.

    Unlike :meth:`TypeSafeJudge.ask`, this reports failures instead of
    swallowing them: it exists precisely so somebody can find out why nothing
    is happening. Only the status class reaches the caller — an error body may
    echo the credential back.
    """
    key = (api_key or "").strip()
    if not key:
        return False, "Paste a key first."
    endpoint = endpoint or route.endpoint
    host = urllib.parse.urlsplit(endpoint).hostname or route.host
    payload = {"model": route.model, "state": _VERIFY_STATE, "questions": dict(_VERIFY_QUESTIONS)}
    post = transport or (lambda body, seconds: _http_post(
        body, seconds, api_key=key, endpoint=endpoint, extra_headers=route.headers))
    started = time.monotonic()
    try:
        status, text = post(payload, timeout_s)
    except Exception as exc:  # network, timeout, TLS
        logger.debug("TypeSafe key verification failed: %s", type(exc).__name__)
        return False, f"Couldn't reach {host} ({type(exc).__name__})."
    elapsed = time.monotonic() - started
    if status == 401:
        return False, f"{host} rejected the key (HTTP 401)."
    if status == 402:
        # OpenRouter's answer for an account with no credits left.
        return False, f"{host} accepted the key, but the account is out of credits (HTTP 402)."
    if status == 403:
        return False, f"{host} accepted the key but denied access (HTTP 403)."
    if status == 404 and route is OPENROUTER_ROUTE:
        # For chat models OpenRouter answers 404 when the account's privacy
        # settings exclude every provider of the model; assume the same here.
        return False, (f"{host} has no route to {route.model} for this key (HTTP 404). "
                       "Check the provider and privacy settings on your OpenRouter account.")
    if status == 429:
        # Throttling only happens after authentication, so the key is good.
        return True, f"{host} accepted the key but is rate limiting it (HTTP 429)."
    if status != 200:
        return False, f"{host} answered HTTP {status}."
    try:
        validate_answers(json.loads(text), _VERIFY_QUESTIONS)
    except (ValueError, TypeSafeError) as exc:
        return False, f"{host} accepted the key but answered oddly: {exc}."
    return True, f"{host} accepted the key and {route.model} answered in {elapsed:.2f} s."
