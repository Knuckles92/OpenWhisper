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
never masquerades as a human-confirmed item. Recaps use the existing note
agent; explicit term corrections remain reversible source-backed notes.
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
#: Spoken notes go straight to the notes page without waiting for an agent pass.
#: ``user_notes`` remains the human guidance channel.
COMMAND_CARDS: Mapping[str, str] = {
    "mark_decision": "decisions",
    "mark_action": "action_items",
    "note_this": "live_notes",
}
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
        if end > start or start - end > REFERENT_MAX_GAP_S:
            continue
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
    # Explicit dictated content or a point preceding the wake word in the same
    # ASR segment takes precedence over unrelated earlier transcript rows.
    content = command_row.get("_content") or command_row.get("_prefix")
    text = content or " ".join((r.get("text") or "").strip() for r in referents).strip()
    if not text:
        return []
    evidence = [] if content else [r["id"] for r in referents if r.get("id")]
    evidence.extend(command_row.get("_evidence_ids") or ([command_id] if command_id else []))
    data = {"source": "voice_command", "command": command,
            "command_segment_id": command_id}
    if card == "live_notes":
        anchor = command_row if content or not referents else referents[0]
        data.update(heading="Requested note", start_s=float(anchor.get("start_s") or 0.0))
    return [{
        "op": "add_item", "card": card, "text": text, "evidence": evidence,
        "data": data,
    }]


class VoiceCommandListener:
    """Watch previews for acknowledgement and committed segments for durable commands.

    ``observe`` is cheap and safe to call from the ASR callback thread: it
    only runs the wake regex and queues work. Judgment and application run on
    one background worker so a slow remote answer never delays transcript
    commits.
    """

    def __init__(self, store: Any, judge: Any, names: Sequence[str], *,
                 cloud_enabled: Optional[Callable[[], bool]] = None,
                 judge_provider: Optional[Callable[[], Any]] = None,
                 confidence: float = VOICE_COMMAND_CONFIDENCE,
                 on_applied: Optional[Callable[[str, List[Any]], None]] = None,
                 executor: Optional[ThreadPoolExecutor] = None,
                 on_command: Optional[Callable] = None,
                 on_feedback: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self._store = store
        self._judge = judge
        self._judge_provider = judge_provider
        self._names = tuple(names)
        self._pattern = compile_wake_pattern(self._names)
        self._cloud_enabled = cloud_enabled
        self._confidence = float(confidence)
        self._on_applied = on_applied
        self._on_command = on_command
        self._on_feedback = on_feedback
        self._slots = threading.BoundedSemaphore(16)
        self._preview_pending: Dict[str, Dict[str, Any]] = {}
        self._preview_running = False
        self._preview_last: Dict[str, Dict[str, Any]] = {}
        self._versions: Dict[str, int] = {}
        self._handled = deque(maxlen=32)
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
        return self._pattern is not None

    def _feedback(self, phase: str, message: str, command: str = "") -> None:
        if self._closed or self._on_feedback is None:
            return
        try:
            self._on_feedback({"phase": phase, "message": message, "command": command})
        except Exception:
            logger.exception("Voice feedback callback failed")

    def _allowed(self) -> bool:
        try:
            return self._cloud_enabled is None or bool(self._cloud_enabled())
        except Exception:
            logger.exception("Voice command consent check failed")
            return False

    def _prepare(self, row, previous):
        row = dict(row)
        text = str(row.get("text") or "").strip()
        match = self._pattern.search(text) if self._pattern else None
        if not match and previous:
            prior = previous[-1]
            gap = float(row.get("start_s") or 0) - float(prior.get("end_s") or 0)
            if (prior.get("channel") == row.get("channel") and 0 <= gap <= 3
                    and self._is_wake_stub(prior.get("text", ""))):
                text = prior["text"].rstrip(" .,!?:;") + ", " + text
                row["text"] = text
                row["start_s"] = prior.get("start_s", row.get("start_s"))
                row["_evidence_ids"] = list(prior.get("_evidence_ids") or
                                                ([prior["id"]] if prior.get("id") else []))
                if row.get("id"):
                    row["_evidence_ids"].append(row["id"])
                match = self._pattern.search(text)
        if not match:
            return None
        row["_prefix"] = text[:match.start()].strip(" .,!?:;")
        if row["_prefix"].lower() in ("hey", "okay", "ok", "hi"):
            row["_prefix"] = ""
        body = text[match.end():].lstrip(" .,!?:;").strip()
        # Copy only explicit argument forms. Deictic commands still use referents.
        inline = re.search(r"^(?:(?:can|could|would) you\s+)?(?:please\s+)?(?:note that|note this:|remember that|(?:take|make|add) a note(?: that)?|"
                           r"add (?:an action item|a decision)(?: that)?|write down|capture this:)"
                           r"\s*[.:,-]?\s+(.+)$", body, re.I)
        if inline:
            row["_content"] = inline.group(1).strip()
        return row

    def _is_wake_stub(self, text):
        match = self._pattern.search(text) if self._pattern else None
        if not match:
            return False
        tail = text[match.end():].strip(" .,!?:;").lower()
        tail = re.sub(r"^(?:(?:can|could|would) you\s+)?(?:please\s+)?", "", tail)
        return (tail in ("", "please", "can you", "could you", "would you", "note that",
                         "mark that as", "write down")
                or re.fullmatch(r"(?:take|make|add) a note(?: that)?", tail) is not None)

    def observe(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Queue bounded committed commands; join a wake name split across segments."""
        if not self.enabled or self._closed:
            return 0
        queued = 0
        for source in rows:
            if not (source.get("text") or "").strip():
                continue
            with self._lock:
                if any(r.get("id") == source.get("id") for r in self._recent):
                    continue
                previous = sorted(self._recent, key=lambda r: float(r.get("start_s") or 0))
                self._recent.append(dict(source))
            row = self._prepare(source, [r for r in previous
                                        if r.get("channel") == source.get("channel")])
            if row is None:
                continue
            channel = str(row.get("channel", ""))
            with self._lock:
                self._versions[channel] = self._versions.get(channel, 0) + 1
            if self._is_wake_stub(row.get("text", "")):
                with self._lock:
                    # Carry a short wake preamble across more than two ASR rows.
                    self._recent[-1] = dict(row)
                self._feedback("heard", "Heard you — listening for the rest…")
                continue
            with self._lock:
                if str(row.get("id")) in self._command_ids:
                    continue
                self._command_ids.append(str(row.get("id")))
                excluded = list(self._command_ids) + list(row.get("_evidence_ids", []))
                self._handled.append(dict(row))
            self._feedback("heard", "Heard you…")
            if not self._slots.acquire(blocking=False):
                self._feedback("error", "Voice commands are busy. Please repeat that shortly.")
                continue
            try:
                self._executor.submit(self._committed_job, row, previous, excluded)
                queued += 1
            except RuntimeError:
                self._slots.release()
                break
        return queued

    def _committed_job(self, row, previous, excluded):
        try:
            return self._judge_and_apply(row, previous, excluded)
        finally:
            self._slots.release()

    def observe_preview(self, payload: Mapping[str, Any]) -> None:
        """Acknowledge and classify provisional text; never write it to meeting state.

        Only the latest preview per channel is retained while Jev is busy. A
        committed segment invalidates any in-flight preview before it can emit
        stale feedback. The durable path independently validates the final text.
        """
        if not self.enabled or self._closed or not payload.get("text"):
            return
        channel = str(payload.get("channel", ""))
        with self._lock:
            prior = self._preview_last.get(channel)
            self._preview_last[channel] = dict(payload)
            recent = list(self._recent)
        row = self._prepare(payload, [prior] if prior else [])
        if row is None:
            return
        normalize = lambda t: " ".join(re.findall(r"\w+", t.lower()))
        with self._lock:
            # Rolling windows repeat already saved commands. Do not reopen the bubble.
            for handled in self._handled:
                if (str(handled.get("channel", "")) == channel
                        and float(handled.get("end_s") or 0) >= float(row.get("start_s") or 0)
                        and normalize(handled.get("text", "")) in normalize(row.get("text", ""))):
                    return
            if prior and prior.get("text") == payload.get("text"):
                return
            version = self._versions.get(channel, 0) + 1
            self._versions[channel] = version
            self._preview_pending[channel] = dict(row=row, previous=recent, version=version)
            running = self._preview_running
            self._preview_running = True
        self._feedback("heard", "Heard you — listening…")
        if not running:
            try:
                self._executor.submit(self._preview_job)
            except RuntimeError:
                with self._lock:
                    self._preview_running = False

    def _preview_job(self):
        with self._lock:
            if not self._preview_pending or self._closed:
                self._preview_running = False
                return
            channel = next(iter(self._preview_pending))
            job = self._preview_pending.pop(channel)
        try:
            if not self._allowed():
                self._feedback("unavailable", "Voice commands need cloud features enabled for this meeting.")
                return
            judge = self._judge_provider() if self._judge_provider else self._judge
            if judge is None:
                self._feedback("unavailable", "Add a TypeSafe API key in Settings to use voice commands.")
                return
            row = job["row"]
            if self._is_wake_stub(row.get("text", "")):
                return
            answer = voice_command_choice(judge, row.get("text", ""),
                                          [p.get("text", "") for p in job["previous"]], self._names)
            allowed = self._allowed()
            with self._lock:
                # Keep the freshness check and feedback ordered with commits.
                # Otherwise a saved callback could be overtaken by this preview.
                if self._versions.get(channel) == job["version"] and not self._closed and allowed:
                    if answer is None:
                        self._feedback("error", "Voice recognition is unavailable. Please try again.")
                    elif answer.choice != "none" and answer.confidence >= self._confidence:
                        self._feedback("recognized", "Got it — waiting for the transcript…", answer.choice)
        except Exception:
            logger.exception("Voice command preview failed")
        finally:
            with self._lock:
                again = bool(self._preview_pending) and not self._closed
                if not again:
                    self._preview_running = False
            if again:
                try:
                    self._executor.submit(self._preview_job)
                except RuntimeError:
                    with self._lock:
                        self._preview_running = False

    def shutdown(self) -> None:
        self._closed = True
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    # -- worker -----------------------------------------------------------------

    def _judge_and_apply(self, row: Dict[str, Any], previous: List[Dict[str, Any]],
                         excluded: List[str]) -> Optional[List[Any]]:
        if self._closed:
            return None
        if not self._allowed():
            self._feedback("unavailable", "Voice commands need cloud features enabled for this meeting.")
            return None
        try:
            judge = self._judge_provider() if self._judge_provider else self._judge
            if judge is None:
                self._feedback("unavailable", "Add a TypeSafe API key in Settings to use voice commands.")
                return None
            answer = voice_command_choice(
                judge, row.get("text") or "",
                [p.get("text") or "" for p in previous], self._names,
            )
        except Exception:
            logger.exception("Voice command judgment failed")
            self._feedback("error", "Could not recognize the command. Please try again.")
            return None
        if self._closed or not self._allowed():
            return None
        if answer is None or answer.choice == "none" or answer.confidence < self._confidence:
            logger.debug("Voice command not applied: %s", answer)
            self._feedback("error" if answer is None else "uncertain",
                           "Voice recognition is unavailable. Please try again." if answer is None else
                           "No command saved. Try ‘assistant, note that…’.")
            return None
        if self._closed or not self._allowed():
            return None
        self._feedback("working", {
            "mark_decision": "Recording the decision…", "mark_action": "Adding an action item…",
            "note_this": "Taking a note…", "set_topic": "Updating the topic…",
            "recap": "Preparing a recap…", "fix_transcript": "Correcting the transcript…",
        }.get(answer.choice, "Working on it…"), answer.choice)
        if answer.choice in ("recap", "fix_transcript"):
            if self._on_command is not None:
                try:
                    return self._on_command(answer.choice, row, previous)
                except Exception:
                    logger.exception("Spoken action failed")
            self._feedback("error", "This command is currently unavailable.", answer.choice)
            return None
        ops = build_ops(answer.choice, row, referent_rows(previous, row, exclude_ids=excluded))
        if not ops:
            logger.info("Voice command %r had nothing to apply", answer.choice)
            self._feedback("uncertain", "Nothing saved yet. Say the point you want me to capture.", answer.choice)
            return None
        try:
            results = self._store.apply("system", ACTOR_ID, ops)
        except Exception:
            logger.exception("Voice command apply failed")
            self._feedback("error", "The command could not be saved. Please try again.", answer.choice)
            return None
        applied = [r for r in results if getattr(r, "ok", False)]
        logger.info(
            "Voice command %s (confidence %.2f): %d of %d ops applied",
            answer.choice, answer.confidence, len(applied), len(results),
        )
        self._feedback("saved" if applied else "error", {
            "mark_decision": "Decision noted", "mark_action": "Action item added",
            "note_this": "Added to Meeting Notes", "set_topic": "Topic updated",
        }.get(answer.choice, "Saved") if applied else "The command could not be saved.", answer.choice)
        if self._on_applied is not None:
            try:
                self._on_applied(answer.choice, list(results))
            except Exception:
                logger.exception("Voice command callback failed")
        return list(results)
