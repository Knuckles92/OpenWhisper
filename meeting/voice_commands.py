"""Spoken instructions to the note taker during a live meeting.

A participant says something like "note taker, mark that as a decision" and
the preceding point becomes a proposed card item with the spoken segments as
evidence. Two gates keep ordinary speech out:

1. **Code decides whether the assistant was addressed.** Only a segment that
   contains one of the configured wake names is ever judged. On 884 real
   meeting segments the semantic judge alone produced no false positives,
   but it does fire on person-directed requests such as "can you write that
   down for me", so the wake name is not optional.
2. **TypeSafe decides which command it was**, returning ``none`` for speech
   that merely mentions the assistant.

Applied ops use the ``system`` actor with ``voice_command`` attribution and
land as ``proposed`` items, so a mistaken trigger is one click to remove and
never masquerades as a human-confirmed item. Recap and transcript-fix
commands are recognised but not yet acted on: both need generated text.
"""
from __future__ import annotations

import logging
import re
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence

from meeting.agent.typesafe_signals import (
    VOICE_COMMAND_CONFIDENCE,
    voice_command_choice,
)

logger = logging.getLogger(__name__)

ACTOR_ID = "voice_command"
#: Segments spoken this long before the command may be what "that" refers to.
REFERENT_MAX_GAP_S = 20.0
REFERENT_MAX_ROWS = 2
#: Card each command writes to. ``note_this`` uses key points because the
#: note-taker pass owns ``live_notes`` headings and ``user_notes`` is the
#: human-only guidance channel.
COMMAND_CARDS: Mapping[str, str] = {
    "mark_decision": "decisions",
    "mark_action": "action_items",
    "note_this": "key_points",
}
#: Recognised but not applied in this version.
UNSUPPORTED_COMMANDS = frozenset({"recap", "fix_transcript"})
_TOPIC_LEAD_INS = (
    r"new topic(?: is)?", r"the topic is(?: now)?", r"topic is(?: now)?",
    r"set the topic to", r"set topic to", r"we're moving on to",
    r"we are moving on to", r"moving on to", r"move on to", r"moving to",
    r"new section for", r"mark a new section for", r"next topic(?: is)?",
    r"switch(?:ing)? to",
)
_TOPIC_RE = re.compile(
    r"(?:" + "|".join(_TOPIC_LEAD_INS) + r")\s*[:,-]?\s*(?P<topic>.+)$",
    re.IGNORECASE,
)
_TOPIC_TRAILERS = re.compile(
    r"\s*(?:please|now|thanks|thank you|okay|ok)?\s*[.!?,;:]*\s*$", re.IGNORECASE,
)
MAX_TOPIC_CHARS = 120


def compile_wake_pattern(names: Sequence[str]) -> Optional[re.Pattern]:
    """Whole-word, case-insensitive pattern for the wake names; spaces match hyphens too."""
    parts = []
    for name in names:
        tokens = [re.escape(tok) for tok in str(name).lower().split()]
        if tokens:
            parts.append(r"[\s\-]*".join(tokens))
    if not parts:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts) + r")(?![a-z0-9])", re.IGNORECASE)


def mentions_assistant(text: str, pattern: Optional[re.Pattern]) -> bool:
    return bool(pattern and text and pattern.search(text))


def extract_topic(text: str) -> Optional[str]:
    """Copy the topic phrase after a lead-in such as "new topic:"; never invent one."""
    match = _TOPIC_RE.search(text or "")
    if not match:
        return None
    topic = _TOPIC_TRAILERS.sub("", match.group("topic")).strip(" \"'")
    if not topic:
        return None
    return topic[:MAX_TOPIC_CHARS].strip()


def referent_rows(previous: Sequence[Mapping[str, Any]], command: Mapping[str, Any],
                  *, exclude_ids: Optional[Sequence[str]] = None) -> List[Mapping[str, Any]]:
    """The one or two segments just before the command that "that" most likely names."""
    excluded = set(exclude_ids or ())
    start = float(command.get("start_s") or 0.0)
    picked: List[Mapping[str, Any]] = []
    for row in reversed(list(previous)):
        if row.get("id") in excluded or row.get("id") == command.get("id"):
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        end = float(row.get("end_s") or row.get("start_s") or 0.0)
        if start - end > REFERENT_MAX_GAP_S:
            break
        picked.append(row)
        if len(picked) >= REFERENT_MAX_ROWS:
            break
    picked.reverse()
    return picked


def build_ops(command: str, command_row: Mapping[str, Any],
              referents: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Translate a recognised command into state-store ops; empty when nothing safe applies."""
    command_id = command_row.get("id")
    if command == "set_topic":
        topic = extract_topic(command_row.get("text") or "")
        if not topic:
            return []
        return [{
            "op": "set_topic", "text": topic,
            "evidence": [command_id] if command_id else [],
        }]
    card = COMMAND_CARDS.get(command)
    if card is None:
        return []
    text = " ".join((r.get("text") or "").strip() for r in referents).strip()
    if not text:
        return []
    evidence = [r["id"] for r in referents if r.get("id")]
    if command_id:
        evidence.append(command_id)
    return [{
        "op": "add_item", "card": card, "text": text, "evidence": evidence,
        "data": {"source": "voice_command", "command": command,
                 "command_segment_id": command_id},
    }]


class VoiceCommandListener:
    """Watch committed segments for wake names and apply recognised commands.

    ``observe`` is cheap and safe to call from the ASR callback thread: it
    only runs the wake regex and queues work. Judgment and application run on
    one background worker so a slow remote answer never delays transcript
    commits.
    """

    def __init__(self, store: Any, judge: Any, names: Sequence[str], *,
                 cloud_enabled: Optional[Callable[[], bool]] = None,
                 confidence: float = VOICE_COMMAND_CONFIDENCE,
                 on_applied: Optional[Callable[[str, List[Any]], None]] = None,
                 executor: Optional[ThreadPoolExecutor] = None) -> None:
        self._store = store
        self._judge = judge
        self._names = tuple(names)
        self._pattern = compile_wake_pattern(self._names)
        self._cloud_enabled = cloud_enabled
        self._confidence = float(confidence)
        self._on_applied = on_applied
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="voice-command",
        )
        self._owns_executor = executor is None
        self._lock = threading.Lock()
        self._recent: Deque[Dict[str, Any]] = deque(maxlen=24)
        self._command_ids: Deque[str] = deque(maxlen=64)
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self._pattern is not None and self._judge is not None

    def observe(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Record new segments; queue a judgment for each that names the assistant."""
        if not self.enabled or self._closed:
            return 0
        queued = 0
        for row in rows:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            with self._lock:
                previous = list(self._recent)
                self._recent.append(dict(row))
            if not mentions_assistant(text, self._pattern):
                continue
            with self._lock:
                self._command_ids.append(str(row.get("id")))
                excluded = list(self._command_ids)
            try:
                self._executor.submit(self._judge_and_apply, dict(row), previous, excluded)
                queued += 1
            except RuntimeError:  # executor shut down mid-batch
                break
        return queued

    def shutdown(self) -> None:
        self._closed = True
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # -- worker -----------------------------------------------------------------

    def _judge_and_apply(self, row: Dict[str, Any], previous: List[Dict[str, Any]],
                         excluded: List[str]) -> Optional[List[Any]]:
        if self._closed:
            return None
        if self._cloud_enabled is not None:
            try:
                if not self._cloud_enabled():
                    return None
            except Exception:
                logger.exception("Voice command consent check failed")
                return None
        try:
            answer = voice_command_choice(
                self._judge, row.get("text") or "",
                [p.get("text") or "" for p in previous], self._names,
            )
        except Exception:
            logger.exception("Voice command judgment failed")
            return None
        if answer is None or answer.choice == "none" or answer.confidence < self._confidence:
            logger.debug("Voice command not applied: %s", answer)
            return None
        if answer.choice in UNSUPPORTED_COMMANDS:
            logger.info("Voice command %r recognised but not supported yet", answer.choice)
            return None
        ops = build_ops(answer.choice, row, referent_rows(previous, row, exclude_ids=excluded))
        if not ops:
            logger.info("Voice command %r had nothing to apply", answer.choice)
            return None
        try:
            results = self._store.apply("system", ACTOR_ID, ops)
        except Exception:
            logger.exception("Voice command apply failed")
            return None
        applied = [r for r in results if getattr(r, "ok", False)]
        logger.info(
            "Voice command %s (confidence %.2f): %d of %d ops applied",
            answer.choice, answer.confidence, len(applied), len(results),
        )
        if self._on_applied is not None:
            try:
                self._on_applied(answer.choice, list(results))
            except Exception:
                logger.exception("Voice command callback failed")
        return list(results)
