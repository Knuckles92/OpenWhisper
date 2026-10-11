"""Route by voice: which saved action the words name, decided by Jev (stream F).

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
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from services.actions.model import AGENT_NAMES, Action, ActionKind, AgentMode, load_actions
from services.automation import catalog, events

logger = logging.getLogger(__name__)

ROUTE_TIMEOUT_S = 1.5
#: A route needs the judge's confidence in its chosen action at least this
#: high. Measured on synthetic utterances (benchmarks/voice_routing,
#: jev-1.13.0, October 10, 2026): on the first 104, no false route at 0.4 or
#: above in any run and the words that must be pasted never got above 0.33
#: for an action; on all 133, no false route at 0.5 or above, the closest
#: being 0.41 ("Use the webhook from the staging config." with Route by
#: voice). 0.5 keeps a margin for messier real speech; 0.6 would lose a
#: further tenth of the routes people meant without avoiding any measured
#: false route.
ROUTE_CONFIDENCE = 0.5

#: ``DictationJob.route`` values.
ROUTE_HOTKEY = "hotkey"
ROUTE_AUTO = "auto"

#: The "not for an action" option of the route question, and the "none of
#: these spans" option of the payload question.
NONE = "none"

# -- words -------------------------------------------------------------------

#: A word, with apostrophes and hyphens inside it ("don't", "to-do").
_WORD = re.compile(r"[^\W_]+(?:['’\-‐][^\W_]+)*")
#: Punctuation between two words that marks a pause ("To do, email …").
_PAUSE = re.compile(r"[,.;:!?…—–-]")
#: Punctuation that ends a sentence, so the next word's capital says nothing.
_SENTENCE_END = re.compile(r"[.!?…]")


@dataclass(frozen=True)
class _Word:
    norm: str
    start: int
    end: int
    #: Punctuation, or the end of the text, follows the word.
    pause: bool = False
    #: Written as a name (see ``_written_as_name``).
    name: bool = False


_JOINERS = str.maketrans("", "", "'’-‐")


def _norm(token: str) -> str:
    return token.casefold().translate(_JOINERS)


def _written_as_name(token: str, sentence_start: bool) -> bool:
    """Whether ``token`` is written like a proper noun: with capitals inside
    it ("ChatGPT"), or capitalized where no sentence starts ("Ask Claude",
    "tell Claude to …"). Speech recognition writes names this way too."""
    return any(ch.isupper() for ch in token[1:]) or (not sentence_start and token[:1].isupper())


def _words(text: str) -> list[_Word]:
    """Words of ``text``, casefolded and without apostrophes or hyphens, with their offsets."""
    text = text or ""
    found = list(_WORD.finditer(text))
    return [
        _Word(_norm(m.group()), m.start(), m.end(),
              pause=i + 1 == len(found) or bool(_PAUSE.search(text, m.end(), found[i + 1].start())),
              name=_written_as_name(m.group(), sentence_start=i == 0 or bool(
                  _SENTENCE_END.search(text, found[i - 1].end(), m.start()))))
        for i, m in enumerate(found)
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
#: said last ("…, add it to my todo list"). Never "the": a name after it is
#: an ordinary noun ("send the scripts to Bob", "use the webhook from …").
_LEAD_INS = frozenset({
    "ask", "tell", "send", "message", "ping", "use", "open", "start", "add", "put",
    "save", "post", "note", "have", "let", "lets", "can", "could", "would", "will",
    "you", "this", "that", "it", "to", "into", "for", "my", "a", "an", "new",
    "with", "via", "using", "in", "on", "hey", "please", "go", "and",
})
_MAX_LEAD_INS = 4

#: Generic last words of an action name ("Tell my Claude Code session"),
#: dropped so that saying "Claude Code" still names it.
_NAME_TAILS = frozenset({
    "session", "sessions", "chat", "app", "action", "hotkey", "hook", "webhook",
    "list", "window", "tab", "please",
})

#: Words that carry an ordinary phrase on ("to do that", "to do it with
#: …"): pronouns, determiners, prepositions, conjunctions and a few adverbs.
#: Before one of them, a name heard as several words ("to do" for Todo) is
#: just those words. Not question words, "can", "please" or a verb: they
#: start the request ("chat GPT what's …", "to do buy milk").
_PHRASE_GOES_ON = frozenset({
    "it", "its", "this", "that", "thats", "these", "those", "them", "him", "her", "me", "us",
    "so", "something", "anything", "everything", "nothing", "one", "all", "both", "each",
    "either", "some", "any", "more", "most", "less", "much", "many", "enough", "such",
    "other", "another", "else", "the", "a", "an", "my", "your", "our", "their", "his", "no",
    "every", "about", "with", "without", "for", "from", "in", "on", "at", "by", "to", "of",
    "into", "onto", "after", "before", "over", "under", "around", "through", "during", "like",
    "than", "as", "between", "against", "up", "down", "out", "off", "away", "back", "and",
    "or", "but", "if", "because", "then", "while", "unless", "until", "though", "although",
    "since", "whether", "well", "better", "right", "now", "later", "again", "too", "also",
    "just", "only", "even", "here", "there", "today", "tonight", "tomorrow", "first", "next",
    "already", "anyway", "instead", "together", "properly", "differently", "otherwise",
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


# -- how a word sounds -------------------------------------------------------------

#: Vowel sounds by spelling, longest spellings first. Claude's "au", cloud's
#: "ou", clod's "o" and clawed's "aw" are one open sound; clued's "ue" isn't.
_VOWEL_SOUNDS = (
    (("ai", "ay", "ei", "ey"), "ay"),
    (("oi", "oy"), "oy"),
    (("oo", "ew", "eu", "ue", "ui", "u"), "u"),
    (("a", "o"), "o"),
    (("e", "i", "y"), "e"),
)


def _sound(word: str) -> Optional[tuple[str, str, str, int]]:
    """Roughly how a word sounds; None when it has no vowel.

    ``(consonants before the first vowel, that vowel's sound, the consonants
    after it, syllables)``: "claude", "cloud", "clod" and "clawed" all sound
    ``("kl", "o", "d", 1)``; "could" (no "l" before the vowel), "cloudy" (two
    syllables) and "clued" (another vowel) don't.
    """
    w = (word.replace("ph", "f").replace("ck", "k").replace("x", "ks")
         .replace("q", "k").replace("c", "k").replace("z", "s"))
    if len(w) > 3 and w.endswith("e") and w[-2] not in "aeiouy":
        w = w[:-1]                    # a silent final e: "claude", "code"
    elif len(w) > 4 and w.endswith("ed") and w[-3] not in "td":
        w = w[:-2] + "d"              # "clawed", "called": no syllable of its own
    groups: list[tuple[int, int]] = []
    i = 0
    while i < len(w):
        if w[i] in "aeiou" or (w[i] == "y" and i > 0):
            j = i + 1
            while j < len(w) and w[j] in "aeiouwy":
                j += 1
            groups.append((i, j))
            i = j
        else:
            i += 1
    if not groups:
        return None
    first, after = groups[0]
    nucleus = w[first:after]
    vowel = next((sound for spellings, sound in _VOWEL_SOUNDS if nucleus.startswith(spellings)),
                 nucleus)
    tail = ""
    for ch in w[after:]:
        if ch in "aeiouyhw" or (tail and ch == tail[-1]):
            continue
        tail += ch
    return w[:first], vowel, tail, len(groups)


def _alike(heard: str, name: str) -> bool:
    """True when ``heard`` is ``name`` or plausibly a mishearing of it ("cloud", "Claude").

    A mishearing keeps the name's consonants around its first vowel, that
    vowel's sound and its syllables; short names ("todo") must be heard exactly.
    """
    if heard == name:
        return True
    if len(heard) < 4 or len(name) < 4 or abs(len(heard) - len(name)) > 2:
        return False
    sound = _sound(name)
    return (sound is not None and len(sound[0]) + len(sound[2]) >= 3
            and _sound(heard) == sound)


# -- names -------------------------------------------------------------------------

@dataclass(frozen=True)
class _Variant:
    """Words that name an action, and what may come before them."""

    words: tuple[str, ...]
    #: The name's own leading words dropped to make this form ("send to my"
    #: from "Send to my scripts"); said before it, they still name the action.
    dropped: tuple[str, ...] = ()
    #: Whether any lead-in words may come before it ("can you ask Claude").
    after_lead_ins: bool = True
    #: Whether they may anyway when a word of it is heard written as a name
    #: ("tell Claude to …" for an action typed "ask claude").
    after_lead_ins_as_name: bool = False

    def follows(self, said: Sequence[str], heard: Sequence[_Word]) -> bool:
        """Whether the lead-in words ``said`` just before it, heard as ``heard``,
        leave it naming the action."""
        if not said or self.after_lead_ins:
            return True
        rest = iter(said)
        if self.dropped and all(word in rest for word in self.dropped):
            return True
        return self.after_lead_ins_as_name and any(word.name for word in heard)


def _name_variants(action: Action) -> list[_Variant]:
    """What names ``action``: each name as given, then without lead-ins and generic tails.

    A name as given may follow lead-in words ("can you ask Claude") unless
    it is only generic words ("Webhook"). A shortened form may follow them
    only when it holds a proper noun, typed as one in the name ("tell
    Claude" for "Ask Claude") or heard as one ("tell Claude" for "ask
    claude"), or after the words it dropped ("send this to my scripts");
    else only at the start ("Scripts, …"), so "send my scripts to Bob" names
    nothing.
    """
    variants: list[_Variant] = []
    seen: set[tuple[str, ...]] = set()

    def add(form: tuple[str, ...], dropped: tuple[str, ...], after_lead_ins: bool,
            as_name: bool = False) -> None:
        if form and form not in seen:
            seen.add(form)
            variants.append(_Variant(form, dropped, after_lead_ins, as_name))

    for phrase in (action.name, *action.spoken_names):
        tokens = [m.group() for m in _WORD.finditer(phrase)]
        words = tuple(_norm(t) for t in tokens)
        if not words:
            continue
        typed_as_name = [_written_as_name(t, sentence_start=i == 0) for i, t in enumerate(tokens)]
        add(words, (), not all(w in _NAME_TAILS for w in words))
        core = 0
        while core < len(words) - 1 and words[core] in _LEAD_INS:
            core += 1
        for start in (0, core):
            end = len(words)
            while end - start > 1 and words[end - 1] in _NAME_TAILS:
                end -= 1
            for stop in (len(words), end):
                form = words[start:stop]
                # A shortened form must still be distinctive.
                if len("".join(form)) >= 3:
                    add(form, words[:start], any(typed_as_name[start:stop]),
                        not all(w in _NAME_TAILS for w in form))
    return variants


def _match_len(words: Sequence[_Word], at: int, variant: Sequence[str]) -> int:
    """How many words from ``at`` say ``variant``; 0 when they don't.

    Spacing is ignored ("chat gpt" says "ChatGPT", "to do buy milk" says
    "Todo"), though a name heard as more words than it has isn't named when
    the words go on as an ordinary phrase without a pause ("to do that"),
    and each word may be a near-homophone of the name's word.
    """
    target = "".join(variant)
    joined = ""
    for k in range(at, len(words)):
        joined += words[k].norm
        if joined == target:
            if (k - at + 1 > len(variant) and not words[k].pause
                    and words[k + 1].norm in _PHRASE_GOES_ON):
                break
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


def _leading_match(words: Sequence[_Word], variants: Sequence[_Variant]
                   ) -> Optional[tuple[int, int, int]]:
    start = _skip_fillers(words)
    for skip in range(_MAX_LEAD_INS + 1):
        at = start + skip
        if at >= len(words) or (skip and words[at - 1].norm not in _LEAD_INS):
            return None
        said = [w.norm for w in words[start:at]]
        best = 0
        for variant in variants:
            n = _match_len(words, at, variant.words)
            if n > best and variant.follows(said, words[at:at + n]):
                best = n
        if best:
            return start, at, at + best
    return None


def _polite_tail(words: Sequence[_Word]) -> int:
    norms = [w.norm for w in words[-2:]]
    if norms[-1:] in (["please"], ["thanks"]):
        return 1
    return 2 if norms == ["thank", "you"] else 0


def _trailing_match(words: Sequence[_Word], variants: Sequence[_Variant]
                    ) -> Optional[tuple[int, int, int]]:
    end = len(words) - _polite_tail(words)
    for variant in variants:
        for at in range(max(0, end - len(variant.words) - 2), end):
            if at == 0 or _match_len(words, at, variant.words) != end - at:
                continue
            first = at
            while first > 0 and at - first < _MAX_LEAD_INS and words[first - 1].norm in _LEAD_INS:
                first -= 1
            if first < at and not variant.follows([w.norm for w in words[first:at]],
                                                  words[at:end]):
                first = at
            # Said last on its own, a name is set off by a pause ("Lock the
            # front door, home."), not the end of a sentence about it.
            if first == 0 or (first == at and not words[at - 1].pause):
                continue
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
        agent = AGENT_NAMES.get(action.param("agent"), "coding agent")
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
    #: Set, with an empty ``action_id``, when the judge couldn't answer
    #: ("unreachable", "timeout"), so Route by voice can say so instead of
    #: "No action matched".
    failure: str = ""


@dataclass(frozen=True)
class RoutingStatus:
    enabled: bool
    ready: bool
    message: str
    #: What is missing: ``FIX_FAST_JUDGMENTS``, ``FIX_ACTIONS`` or
    #: ``FIX_TRIGGER``; "" when ready or off.
    fix: str = ""


#: Fast judgments are off, or the chosen route has no key.
FIX_FAST_JUDGMENTS = "fast_judgments"
#: No actions, or every action is turned off.
FIX_ACTIONS = "actions"
#: Nothing starts it: no Route by voice shortcut, and normal dictations aren't listened to.
FIX_TRIGGER = "trigger"


#: ``RouteDecision.failure``: no answer within ``ROUTE_TIMEOUT_S``.
FAILURE_TIMEOUT = "timeout"
#: ``RouteDecision.failure``: no usable answer (Fast judgments off or without
#: a key at that moment, a network or HTTP error, a malformed answer).
FAILURE_UNREACHABLE = "unreachable"
#: How often the wait for the judge looks at whether the dictation was canceled.
_CANCEL_POLL_S = 0.02
_CANCELED = "canceled"


def decide(text: str, job, settings: Mapping, *, judge=None,
           cancel: Optional[threading.Event] = None) -> Optional[RouteDecision]:
    """Where ``text`` goes; worker thread, never raises.

    Returns:
        The action the words are for; None to paste them as a dictation (they
        don't name an action, the judge chose none or wasn't confident
        enough, or ``cancel`` was set); or ``RouteDecision("", "", 0.0,
        failure=…)`` when the judge couldn't answer: "timeout" after
        ``ROUTE_TIMEOUT_S`` of wall-clock time, else "unreachable".

    The judge is asked on a helper thread, so neither a slow answer nor
    reading its key holds the dictation past ``ROUTE_TIMEOUT_S``; a set
    ``cancel`` ends the wait at once and nothing is sent once it is set.
    Publishes ``route.decided`` whenever the words were to be judged and the
    dictation wasn't canceled: with the action when they were routed, else
    ``fallback`` true (and the judge's confidence in its choice, when it
    answered).
    """
    try:
        return _decide(text, job, settings or {}, judge, cancel)
    except Exception as exc:
        logger.warning("Route by voice failed (%s); pasting the dictation", type(exc).__name__)
        return None


def _ask(request: RoutePlan, settings: Mapping, judge,
         cancel: Optional[threading.Event]) -> tuple[Optional[Mapping], str]:
    """``(answers, "")``, or ``(None, failure)`` with a ``FAILURE_*`` or ``_CANCELED``."""
    done = threading.Event()
    outcome: dict[str, Any] = {}

    def work() -> None:
        try:
            asking = judge
            if asking is None:
                from services.typesafe import judge_from_settings

                asking = judge_from_settings(dict(settings))
            if asking is None:
                logger.debug("Route by voice couldn't ask: Fast judgments aren't set up")
            else:
                outcome["answers"] = asking.ask(request.state, request.questions,
                                                timeout_s=ROUTE_TIMEOUT_S)
        except Exception as exc:
            logger.warning("Route by voice judgment failed (%s)", type(exc).__name__)
        finally:
            done.set()

    threading.Thread(target=work, name="route-by-voice", daemon=True).start()
    deadline = time.monotonic() + ROUTE_TIMEOUT_S
    while True:
        if cancel is not None and cancel.is_set():
            return None, _CANCELED
        left = deadline - time.monotonic()
        if left <= 0 and not done.is_set():
            return None, FAILURE_TIMEOUT
        if done.is_set() or done.wait(left if cancel is None else min(left, _CANCEL_POLL_S)):
            break
    answers = outcome.get("answers")
    return (answers, "") if answers is not None else (None, FAILURE_UNREACHABLE)


def _decide(text: str, job, settings: Mapping, judge,
            cancel: Optional[threading.Event]) -> Optional[RouteDecision]:
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
    if request is None or (cancel is not None and cancel.is_set()):
        return None
    answers, failure = _ask(request, settings, judge, cancel)
    if failure == _CANCELED:
        return None
    judgment = request.read(answers) if not failure else None
    if judgment is None and not failure:
        failure = FAILURE_UNREACHABLE  # it answered, but not the route question
    routed = (judgment is not None and judgment.action_id is not None
              and judgment.confidence >= ROUTE_CONFIDENCE)
    logger.debug(
        "Route by voice (%s, %d candidates): %s at %s",
        route, len(request.candidates),
        failure or (judgment.action_id or NONE),
        "-" if judgment is None else f"{judgment.confidence:.2f}",
    )
    events.publish(catalog.ROUTE_DECIDED, catalog.route_decided(
        job,
        judgment.action_id if routed else None,
        judgment.confidence if judgment is not None else None,
        fallback=not routed,
    ))
    if failure:
        return RouteDecision("", "", 0.0, failure=failure)
    if not routed:
        return None
    return RouteDecision(judgment.action_id, judgment.payload, judgment.confidence)


#: The status shows on the Actions page, below the actions and the Route by
#: voice switch and above its shortcut; Fast judgments live on another page.
_WHERE_FAST = "Settings → Meeting Mode → Fast judgments"
#: ``hotkeys`` entry of the Route by voice shortcut.
ROUTE_HOTKEY_NAME = "route_record"


def _route_shortcut(settings: Mapping) -> str:
    hotkeys = settings.get("hotkeys")
    value = hotkeys.get(ROUTE_HOTKEY_NAME) if isinstance(hotkeys, Mapping) else ""
    return value.strip() if isinstance(value, str) else ""


def status(settings: Mapping) -> RoutingStatus:
    """Whether Route by voice can run, what it still needs, and what it sends; never raises."""
    try:
        return _status(settings or {})
    except Exception as exc:
        logger.debug("Route by voice status unavailable: %s", type(exc).__name__)
        return RoutingStatus(False, False, "Route by voice isn't available right now.")


def _status(settings: Mapping) -> RoutingStatus:
    from services import typesafe
    from services.settings import (
        resolve_typesafe_enabled,
        resolve_voice_routing_enabled,
        resolve_voice_routing_on_dictation,
    )

    if not resolve_voice_routing_enabled(settings):
        return RoutingStatus(False, False, "Route by voice is off. Turn it on above.")
    judge_route = typesafe.route_from_settings(dict(settings))
    if not resolve_typesafe_enabled(settings):
        return RoutingStatus(True, False, (
            f"Route by voice needs Fast judgments. Turn them on in {_WHERE_FAST}."),
            FIX_FAST_JUDGMENTS)
    if not typesafe.key_present(dict(settings), route=judge_route):
        return RoutingStatus(True, False, (
            f"Route by voice needs {judge_route.key_phrase}. Add one in {_WHERE_FAST}."),
            FIX_FAST_JUDGMENTS)
    actions = load_actions(settings)
    if not actions:
        return RoutingStatus(True, False, (
            "Route by voice is on, but there are no actions yet. Add one above."), FIX_ACTIONS)
    if not any(a.enabled for a in actions):
        return RoutingStatus(True, False, (
            "Route by voice is on, but every action is turned off. Turn one on above."),
            FIX_ACTIONS)
    shortcut = bool(_route_shortcut(settings))
    on_dictation = resolve_voice_routing_on_dictation(settings)
    if not (shortcut or on_dictation):
        return RoutingStatus(True, False, (
            "Route by voice is on, but has nothing to listen to yet. Set a Route by voice "
            "shortcut below, or turn on Also listen to normal dictations."), FIX_TRIGGER)
    to = f"to {judge_route.name} to choose the action."
    if shortcut and on_dictation:
        sent = ("Everything you record with the Route by voice shortcut, and dictations "
                f"that start with an action's name, are sent {to}")
    elif shortcut:
        sent = f"Everything you record with the Route by voice shortcut is sent {to}"
    else:
        sent = f"Dictations that start with an action's name are sent {to}"
    return RoutingStatus(True, True, f"Route by voice is on. {sent}")
