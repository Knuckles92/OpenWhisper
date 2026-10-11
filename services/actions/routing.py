"""Spoken routing: which saved action the words name, decided by Jev (stream F).

Two gates, as with the meeting note taker's voice commands
(meeting/voice_commands.py):

1. **Code decides whether an action could have been addressed.** A plain
   dictation is judged only when it starts with an action's name or one of
   its spoken names, after filler words ("hey", "okay so") and a few lead-in
   words ("ask", "send this to"); a name misheard by speech recognition
   ("cloud" for "Claude") still counts. Words recorded with Route by voice
   skip that requirement: every enabled action is a candidate, and the ones
   whose name was said go first.
2. **Jev decides**, in one request with two questions asked together: which
   action the words are for (or none), and which code-proposed span of the
   words is meant for it. Only a confident choice of an action routes;
   anything else, including a failure or a timeout, leaves the words to be
   pasted as a normal dictation.

Only words that passed the gate, or were recorded with Route by voice, are
sent, to TypeSafe or OpenRouter as Settings → Fast judgments says, along with
each candidate action's name, spoken names, description and kind (never its
link, address, program or folder). Nothing here logs the words.

Contract: docs/design/automation/design.md, decision 4 and
"services/actions/routing.py".
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from services.actions.model import AGENT_NAMES, Action, ActionKind, AgentMode, load_actions
from services.automation import catalog, events

logger = logging.getLogger(__name__)

ROUTE_TIMEOUT_S = 1.5
#: A route needs the judge's confidence in its chosen action at least this
#: high. Measured on 104 synthetic utterances (benchmarks/voice_routing,
#: October 10, 2026, jev-1.13.0): no false route at 0.4 or above in any run,
#: and the words that must be pasted never got above 0.33 for an action, so
#: 0.5 keeps a margin for messier real speech; 0.6 would lose a further tenth
#: of the routes people meant without avoiding any measured false route.
ROUTE_CONFIDENCE = 0.5

#: ``DictationJob.route`` values.
ROUTE_HOTKEY = "hotkey"
ROUTE_AUTO = "auto"

#: The "not for an action" option of the route question, and the "none of
#: these spans" option of the payload question.
NONE = "none"

# -- words -------------------------------------------------------------------

_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


@dataclass(frozen=True)
class _Word:
    norm: str
    start: int
    end: int


def _words(text: str) -> list[_Word]:
    """Words of ``text``, casefolded and without apostrophes, with their offsets."""
    return [
        _Word(m.group().casefold().replace("'", "").replace("’", ""), m.start(), m.end())
        for m in _WORD.finditer(text or "")
    ]


#: Said before addressing anything; skipped at the start of an utterance.
_FILLERS = frozenset({
    "hey", "hi", "hello", "yo", "ok", "okay", "so", "um", "umm", "uh", "uhh", "uhm",
    "er", "erm", "ah", "oh", "hmm", "well", "please", "alright", "right", "now",
    "and", "like", "yeah", "yes",
})
_FILLER_PHRASES = (("all", "right"),)

#: Words that may come between the fillers and an action's name ("can you
#: ask Claude", "send this to my scripts"), and between a request and a name
#: said last ("…, add it to my todo list").
_LEAD_INS = frozenset({
    "ask", "tell", "send", "message", "ping", "use", "open", "start", "add", "put",
    "save", "post", "note", "have", "let", "lets", "can", "could", "would", "will",
    "you", "this", "that", "it", "to", "into", "for", "the", "my", "a", "an", "new",
    "with", "via", "using", "in", "on", "hey", "please", "go", "and",
})
_MAX_LEAD_INS = 4

#: Generic last words of an action name ("Tell my Claude Code session"),
#: dropped so that saying "Claude Code" still names it.
_NAME_TAILS = frozenset({
    "session", "sessions", "chat", "app", "action", "hotkey", "hook", "webhook",
    "list", "window", "tab", "please",
})

#: Words that only link a name to the request ("Claude, um, can you …",
#: "ask Claude to …"); a payload span is also proposed without them.
_LINK_WORDS = _FILLERS | frozenset({"to", "that", "about", "can", "could", "would", "will",
                                    "you"})
_MAX_LINK_WORDS = 4

_POLITE_END = re.compile(r"[\s,;:]*\b(?:please|thanks|thank you)\b\s*([.!?]*)\s*$", re.IGNORECASE)
_LEADING_JUNK = " \t\r\n,.;:!?-–—"
_TRAILING_JUNK = " \t\r\n,;:-–—"


def _skip_fillers(words: Sequence[_Word]) -> int:
    i = 0
    while i < len(words):
        if words[i].norm in _FILLERS:
            i += 1
            continue
        phrase = next((p for p in _FILLER_PHRASES
                       if tuple(w.norm for w in words[i:i + len(p)]) == p), None)
        if phrase is None:
            break
        i += len(phrase)
    return i


def _skeleton(word: str) -> str:
    """Roughly how a word sounds: its first letter and its consonants."""
    w = (word.replace("ph", "f").replace("ck", "k").replace("x", "ks")
         .replace("q", "k").replace("c", "k").replace("z", "s"))
    out = w[:1]
    for ch in w[1:]:
        if ch in "aeiouyhw" or ch == out[-1]:
            continue
        out += ch
    return out


def _alike(heard: str, name: str) -> bool:
    """True when ``heard`` is ``name`` or plausibly a mishearing of it ("cloud", "Claude")."""
    if heard == name:
        return True
    if len(heard) < 4 or len(name) < 4 or abs(len(heard) - len(name)) > 2:
        return False
    sound = _skeleton(name)
    return len(sound) >= 3 and _skeleton(heard) == sound


def _name_variants(action: Action) -> list[tuple[str, ...]]:
    """Word sequences that name ``action``: each name, without lead-ins and generic tails."""
    variants: list[tuple[str, ...]] = []
    for phrase in (action.name, *action.spoken_names):
        words = tuple(w.norm for w in _words(phrase))
        if not words:
            continue
        core = words
        while len(core) > 1 and core[0] in _LEAD_INS:
            core = core[1:]
        forms = [words]
        for form in (words, core):
            short = form
            while len(short) > 1 and short[-1] in _NAME_TAILS:
                short = short[:-1]
            forms.extend((form, short))
        for i, form in enumerate(forms):
            # A shortened form must still be distinctive; the name itself always counts.
            if form not in variants and (i == 0 or len("".join(form)) >= 3):
                variants.append(form)
    return variants


def _match_len(words: Sequence[_Word], at: int, variant: Sequence[str]) -> int:
    """How many words from ``at`` say ``variant``; 0 when they don't.

    Spacing is ignored ("chat GPT" says "ChatGPT", "to do" says "todo"), and
    each word may be a near-homophone of the name's word.
    """
    target = "".join(variant)
    joined = ""
    for k in range(at, len(words)):
        joined += words[k].norm
        if joined == target:
            return k - at + 1
        if not target.startswith(joined):
            break
    n = len(variant)
    if at + n <= len(words) and all(_alike(words[at + i].norm, variant[i]) for i in range(n)):
        return n
    return 0


@dataclass(frozen=True)
class GateMatch:
    """Where an utterance names an action.

    ``leading`` matches start the utterance (after fillers and lead-ins);
    the words for the action follow ``name_end``. Trailing matches (Route by
    voice only) end it; the words for the action come before ``first``.
    """

    action_id: str
    first: int
    name_start: int
    name_end: int
    leading: bool


def _leading_match(words: Sequence[_Word], variants: Sequence[Sequence[str]]
                   ) -> Optional[tuple[int, int, int]]:
    start = _skip_fillers(words)
    for skip in range(_MAX_LEAD_INS + 1):
        at = start + skip
        if at >= len(words) or (skip and words[at - 1].norm not in _LEAD_INS):
            return None
        best = max((_match_len(words, at, v) for v in variants), default=0)
        if best:
            return start, at, at + best
    return None


def _polite_tail(words: Sequence[_Word]) -> int:
    norms = [w.norm for w in words[-2:]]
    if norms[-1:] in (["please"], ["thanks"]):
        return 1
    return 2 if norms == ["thank", "you"] else 0


def _trailing_match(words: Sequence[_Word], variants: Sequence[Sequence[str]]
                    ) -> Optional[tuple[int, int, int]]:
    end = len(words) - _polite_tail(words)
    for variant in variants:
        for at in range(max(0, end - len(variant) - 2), end):
            if _match_len(words, at, variant) != end - at:
                continue
            first = at
            while first > 0 and at - first < _MAX_LEAD_INS and words[first - 1].norm in _LEAD_INS:
                first -= 1
            if first > 0:
                return first, at, end
    return None


def gate(text: str, actions: Sequence[Action], *, route: str) -> list[GateMatch]:
    """Actions ``text`` names, most completely named first.

    A plain dictation (``route`` "auto") names an action only at its start;
    with Route by voice (``route`` "hotkey") a name said last counts too.
    """
    words = _words(text)
    if not words:
        return []
    matches: list[GateMatch] = []
    for action in actions:
        variants = _name_variants(action)
        found = _leading_match(words, variants)
        if found is not None:
            matches.append(GateMatch(action.id, *found, leading=True))
            continue
        if route == ROUTE_HOTKEY:
            found = _trailing_match(words, variants)
            if found is not None:
                matches.append(GateMatch(action.id, *found, leading=False))
    order = {a.id: i for i, a in enumerate(actions)}
    matches.sort(key=lambda m: (not m.leading, -(m.name_end - m.first), order[m.action_id]))
    return matches


# -- payload spans -----------------------------------------------------------

def _without_polite_end(span: str) -> str:
    trimmed = _POLITE_END.sub(lambda m: m.group(1), span).rstrip(_TRAILING_JUNK)
    return trimmed if _words(trimmed) else span


def _spans(text: str, words: Sequence[_Word], matches: Sequence[GateMatch]) -> list[str]:
    """Code-proposed payloads: the words after (or before) each named action, then all of it."""
    spans: list[str] = []

    def add(span: str) -> None:
        span = span.strip()
        if _words(span) and span not in spans:
            spans.append(span)

    for match in matches:
        if match.leading:
            if match.name_end >= len(words):
                continue
            after = text[words[match.name_end - 1].end:].lstrip(_LEADING_JUNK).rstrip()
            linked = match.name_end
            while (linked < len(words) - 1 and linked - match.name_end < _MAX_LINK_WORDS
                   and words[linked].norm in _LINK_WORDS):
                linked += 1
            unlinked = text[words[linked].start:].rstrip() if linked > match.name_end else after
            for span in (after, unlinked):
                add(span)
            for span in (after, unlinked):
                add(_without_polite_end(span))
        else:
            add(text[:words[match.first].start].rstrip(_TRAILING_JUNK))
    add(text.strip())
    return spans


#: Long dictations are shown to the judge by their start and end, which is
#: where an action is addressed; the request stays well inside TypeSafe's
#: state budget however long someone talks.
_STATE_HEAD = 1500
_STATE_TAIL = 400
_SPAN_HEAD = 200
_SPAN_TAIL = 80


def _clip(text: str, head: int, tail: int) -> str:
    return text if len(text) <= head + tail + 3 else f"{text[:head]} … {text[-tail:]}"


def _sentence_start(span: str) -> str:
    """``span`` with a lowercase first letter raised, as the start of a new request."""
    if span[:1].islower() and not span[1:2].isupper():
        return span[0].upper() + span[1:]
    return span


# -- the request ----------------------------------------------------------------

#: Worded as measured in benchmarks/voice_routing; rewording needs a live re-run.
_INSTRUCTIONS = {
    ROUTE_AUTO: (
        "`dictation` starts with the name of one of the user's saved actions (speech "
        "recognition may mishear a name, such as \"cloud\" for \"Claude\"). Is the user "
        "speaking to that action, the way you address an assistant (\"Claude, ...\", "
        "\"ask Claude to ...\", \"todo: ...\"), to give it a request? Choose the action. "
        "Choose none when the name is the subject or object of an ordinary sentence "
        "meant to be typed, such as \"Claude said ...\" or \"I asked Claude ...\"."
    ),
    ROUTE_HOTKEY: (
        "The user recorded `dictation` with a shortcut that sends the words to one "
        "of their saved actions. Which action is it meant for? Usually the user says "
        "the action's name or a spoken name (speech recognition may mishear a name, "
        "such as \"cloud\" for \"Claude\"); otherwise choose the action whose purpose "
        "clearly fits the request. Choose none when no action fits."
    ),
}
_NONE_CRITERIA = {
    ROUTE_AUTO: {
        "what": "Ordinary dictation to type where the cursor is.",
        "includes": "Sentences in which an action's name is just a word, such as "
                    "\"Claude said the build is green.\"",
    },
    ROUTE_HOTKEY: {
        "what": "No action fits: ordinary dictation, or a request none of the actions handles.",
        "includes": "Sentences that only mention an action's name or talk about it.",
    },
}
PAYLOAD_QUESTION = (
    "Which option is the part of `dictation` meant for the action: the request "
    "itself, without the words at its start or end that only name or address the "
    "action?"
)
PAYLOAD_ADDRESSING = ("an action name, with words such as \"hey\", \"ask\", \"tell\", "
                      "\"send this to\" or \"add it to my\"")
PAYLOAD_NONE = "None of these; use the whole dictation as said."
_MAX_PAYLOAD_NAMES = 12

_KIND_PHRASES = {
    # Links only prefill (a new chat, a search); the user sends.
    ActionKind.OPEN_LINK: "Opens an app or website with the words filled in, ready for "
                          "the user to send.",
    ActionKind.CHANNEL: "Sends the words into a running Claude Code session.",
    ActionKind.WEBHOOK: "Sends the words to a web service.",
    ActionKind.PROGRAM: "Runs a program on this computer with the words.",
    ActionKind.CLIPBOARD: "Copies the words without pasting them.",
    ActionKind.SCRIPTS: "Sends the words to the user's own scripts.",
}


def _kind_phrase(action: Action) -> str:
    if action.kind == ActionKind.AGENT:
        agent = AGENT_NAMES.get(action.param("agent"), "a coding agent")
        if action.param("mode") == AgentMode.ASK:
            return f"Asks {agent} the words as a question and shows the answer."
        return f"Starts a {agent} session with the words as the task."
    return _KIND_PHRASES.get(action.kind, "")


def _criterion(action: Action, purpose_chars: int, spoken: int) -> dict:
    entry: dict[str, Any] = {"name": action.name}
    if spoken and action.spoken_names:
        entry["spoken_names"] = list(action.spoken_names[:spoken])
    if purpose_chars and action.description:
        entry["purpose"] = action.description[:purpose_chars]
    entry["does"] = _kind_phrase(action)
    return entry


#: The whole request stays under this many characters of JSON, below
#: services.typesafe.MAX_STATE_CHARS, by describing each action more
#: briefly when there are many.
_REQUEST_CHARS = 20_000
_DESCRIPTION_LEVELS = ((300, 8), (160, 4), (80, 2), (0, 0))


@dataclass(frozen=True)
class Judgment:
    """What the judge answered, before the confidence threshold."""

    #: The action it chose, or None for none.
    action_id: Optional[str]
    confidence: float
    #: Probability of the chosen option.
    probability: float
    payload: str


@dataclass(frozen=True)
class RoutePlan:
    """One routing request: the candidates, and the questions asked about them."""

    text: str
    route: str
    candidates: tuple[str, ...]
    matched: tuple[str, ...]
    state: dict
    questions: dict
    #: Payload option → the span of ``text`` it stands for.
    spans: Mapping[str, str] = field(default_factory=dict)

    def read(self, answers: Optional[Mapping[str, Any]]) -> Optional[Judgment]:
        """The judgment in ``answers``, or None when they don't answer the route question."""
        route = (answers or {}).get("route")
        if not isinstance(route, Mapping):
            return None
        choice = route.get("choice")
        if choice != NONE and choice not in self.candidates:
            return None
        probabilities = route.get("probabilities") or {}
        confidence = float(route.get("confidence") or 0.0)
        probability = float(probabilities.get(choice, confidence)) if isinstance(
            probabilities, Mapping) else confidence
        return Judgment(
            action_id=None if choice == NONE else str(choice),
            confidence=confidence,
            probability=probability,
            payload=self._payload((answers or {}).get("payload")),
        )

    def _payload(self, answer: Any) -> str:
        full = self.text.strip()
        if not self.spans or not isinstance(answer, Mapping):
            return full
        probabilities = answer.get("probabilities")
        if isinstance(probabilities, Mapping) and probabilities:
            # "None of these" means the whole dictation, so it counts for that span.
            weight: dict[str, float] = {}
            for option, p in probabilities.items():
                span = self.spans.get(option, full) if option != NONE else full
                if isinstance(p, (int, float)):
                    weight[span] = weight.get(span, 0.0) + float(p)
            span = max(weight, key=weight.get) if weight else full
        else:
            span = self.spans.get(answer.get("choice"), full)
        return span if span == full else _sentence_start(span)


def plan(text: str, route: str, actions: Sequence[Action]) -> Optional[RoutePlan]:
    """The request that decides where ``text`` goes, or None when there is nothing to ask.

    ``actions`` are the enabled actions. With ``route`` "auto" only the
    actions the words start with are candidates, and none at all means no
    request; with "hotkey" every action is one, named ones first.
    """
    text = (text or "").strip()
    words = _words(text)
    if route not in _INSTRUCTIONS or not words or not actions:
        return None
    matches = gate(text, actions, route=route)
    if route == ROUTE_AUTO and not matches:
        return None
    by_id = {a.id: a for a in actions}
    matched = tuple(dict.fromkeys(m.action_id for m in matches))
    candidates = matched if route == ROUTE_AUTO else matched + tuple(
        a.id for a in actions if a.id not in matched)

    spans = _spans(text, words, matches)
    span_options: dict[str, str] = {}
    for span in spans:
        span_options.setdefault(_clip(span, _SPAN_HEAD, _SPAN_TAIL), span)
    state = {"dictation": _clip(text, _STATE_HEAD, _STATE_TAIL)}

    for purpose_chars, spoken in _DESCRIPTION_LEVELS:
        criteria: dict[str, Any] = {
            action_id: _criterion(by_id[action_id], purpose_chars, spoken)
            for action_id in candidates
        }
        criteria[NONE] = _NONE_CRITERIA[route]
        questions: dict[str, Any] = {
            "route": {"type": "choice", "instructions": _INSTRUCTIONS[route],
                      "criteria": criteria},
        }
        if len(span_options) > 1:
            payload_criteria: dict[str, Any] = {option: None for option in span_options}
            payload_criteria[NONE] = PAYLOAD_NONE
            names = list(dict.fromkeys(
                name for action_id in matched
                for name in (by_id[action_id].name, *by_id[action_id].spoken_names)
            ))[:_MAX_PAYLOAD_NAMES]
            questions["payload"] = {
                "type": "choice",
                "instructions": {"question": PAYLOAD_QUESTION, "action_names": names,
                                 "addressing": PAYLOAD_ADDRESSING},
                "criteria": payload_criteria,
            }
        if len(json.dumps({"state": state, "questions": questions})) <= _REQUEST_CHARS:
            break
    return RoutePlan(
        text=text,
        route=route,
        candidates=candidates,
        matched=matched,
        state=state,
        questions=questions,
        spans=span_options if "payload" in questions else {},
    )


# -- deciding ------------------------------------------------------------------

@dataclass(frozen=True)
class RouteDecision:
    action_id: str
    payload: str
    confidence: float


@dataclass(frozen=True)
class RoutingStatus:
    enabled: bool
    ready: bool
    message: str


def decide(text: str, job, settings: Mapping, *, judge=None) -> Optional[RouteDecision]:
    """The action ``text`` names, or None to paste it; worker thread, never raises.

    Publishes ``route.decided`` whenever the judge was asked: with the action
    when the words were routed, else ``fallback`` true (and the judge's
    confidence in its choice, when it answered).
    """
    try:
        return _decide(text, job, settings or {}, judge)
    except Exception as exc:
        logger.warning("Spoken routing failed (%s); pasting the dictation", type(exc).__name__)
        return None


def _decide(text: str, job, settings: Mapping, judge) -> Optional[RouteDecision]:
    from services.settings import (
        resolve_voice_routing_enabled,
        resolve_voice_routing_on_dictation,
    )

    route = getattr(job, "route", "") or ""
    if route not in (ROUTE_HOTKEY, ROUTE_AUTO) or not resolve_voice_routing_enabled(settings):
        return None
    if route == ROUTE_AUTO and not resolve_voice_routing_on_dictation(settings):
        return None
    request = plan(text, route, [a for a in load_actions(settings) if a.enabled])
    if request is None:
        return None
    if judge is None:
        from services.typesafe import judge_from_settings

        judge = judge_from_settings(dict(settings))
        if judge is None:
            logger.debug("Spoken routing skipped: Fast judgments aren't set up")
            return None
    try:
        answers = judge.ask(request.state, request.questions, timeout_s=ROUTE_TIMEOUT_S)
    except Exception as exc:
        logger.warning("Spoken routing judgment failed (%s)", type(exc).__name__)
        answers = None
    judgment = request.read(answers)
    routed = (judgment is not None and judgment.action_id is not None
              and judgment.confidence >= ROUTE_CONFIDENCE)
    logger.debug(
        "Spoken routing (%s, %d candidates): %s at %s",
        route, len(request.candidates),
        "no answer" if judgment is None else (judgment.action_id or NONE),
        "-" if judgment is None else f"{judgment.confidence:.2f}",
    )
    events.publish(catalog.ROUTE_DECIDED, catalog.route_decided(
        job,
        judgment.action_id if routed else None,
        judgment.confidence if judgment is not None else None,
        fallback=not routed,
    ))
    if not routed:
        return None
    return RouteDecision(judgment.action_id, judgment.payload, judgment.confidence)


_WHERE_ACTIONS = "Settings → Actions"
_WHERE_FAST = "Settings → Meeting Mode → Fast judgments"


def status(settings: Mapping) -> RoutingStatus:
    """Whether spoken routing can run, and what to tell the user; never raises."""
    try:
        return _status(settings or {})
    except Exception as exc:
        logger.debug("Spoken routing status unavailable: %s", type(exc).__name__)
        return RoutingStatus(False, False, "Spoken routing isn't available right now.")


def _status(settings: Mapping) -> RoutingStatus:
    from services import typesafe
    from services.settings import (
        resolve_typesafe_enabled,
        resolve_voice_routing_enabled,
        resolve_voice_routing_on_dictation,
    )

    if not resolve_voice_routing_enabled(settings):
        return RoutingStatus(False, False, f"Spoken routing is off. Turn it on in {_WHERE_ACTIONS}.")
    judge_route = typesafe.route_from_settings(dict(settings))
    if not resolve_typesafe_enabled(settings):
        return RoutingStatus(True, False, (
            f"Spoken routing needs Fast judgments. Turn them on in {_WHERE_FAST}."))
    if not typesafe.key_present(dict(settings), route=judge_route):
        return RoutingStatus(True, False, (
            f"Spoken routing needs {judge_route.key_phrase}. Add one in {_WHERE_FAST}."))
    actions = load_actions(settings)
    if not actions:
        return RoutingStatus(True, False, (
            f"Spoken routing is on, but there are no actions yet. Add one in {_WHERE_ACTIONS}."))
    if not any(a.enabled for a in actions):
        return RoutingStatus(True, False, (
            f"Spoken routing is on, but every action is turned off. Turn one on in "
            f"{_WHERE_ACTIONS}."))
    if resolve_voice_routing_on_dictation(settings):
        sent = ("What you record with Route by voice, and dictations that start with an "
                f"action's name, are sent to {judge_route.name} to choose the action.")
    else:
        sent = (f"What you record with Route by voice is sent to {judge_route.name} to "
                "choose the action.")
    return RoutingStatus(True, True, f"Spoken routing is on. {sent}")
