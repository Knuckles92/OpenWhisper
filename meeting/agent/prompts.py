"""Prompt construction for the meeting-intelligence agent.

Two entry points: :func:`build_system_prompt` (the agent's standing charter,
shared by every core — Pi sidecar and direct) and
:func:`build_checkpoint_user_prompt` (the per-checkpoint rendering of the
dashboard state plus new transcript segments). All numeric policy values are
imported from :mod:`meeting.state.patches` so the prompt can never disagree
with validation.
"""
from __future__ import annotations

from typing import Any, Dict, List

from meeting.agent.question_engine import build_question_guidance
from meeting.state.patches import (
    MAX_EVIDENCE_REFS,
    MAX_OPEN_QUESTIONS,
    RESOLVE_CONFIDENCE,
    SUGGEST_CONFIDENCE,
)
from meeting.state.schema import CARD_KEYS

__all__ = [
    "build_system_prompt",
    "build_checkpoint_user_prompt",
    "render_state_compact",
    "format_segment_line",
    "JSON_FALLBACK_INSTRUCTIONS",
]

_MAX_ITEM_TEXT_CHARS = 240

_SYSTEM_PROMPT_TEMPLATE = """\
You are the live meeting-intelligence copilot inside OpenWhisper's Meeting Mode.
You maintain a shared dashboard that meeting participants watch and edit in real
time. You have two equally important jobs: be a live thinking aid for the people
in the meeting, and build a faithful, durable record of what was said and decided.

You receive periodic checkpoints: the current dashboard state (with ids,
revisions, and statuses) followed by new transcript segments. You act ONLY by
emitting operations through the tools you are given; you never write prose
directly to the user.

OPERATIONS
- add_item(card, text, data?, evidence): add an entry to a dashboard card.
- update_item(id, base_revision, set, evidence): change an item's text and/or
  data via the "set" object. base_revision MUST equal the item's current
  revision as shown in the state.
- remove_item(id, base_revision, evidence): mark one of your items removed (wrong,
  duplicated, or superseded).
- set_topic(text, evidence): update the current main topic when discussion moves on.
- set_rolling_summary(text, evidence): replace the running summary of the meeting so far.
  Keep it to a few tight sentences; rewrite it, do not append endlessly.
- upsert_participant(display_name, kind, evidence): create a participant for a
  distinct remote speaker you can identify; or, with id, rename one you created.
- suggest_participant_name(participant_id, display_name, evidence): propose a
  name for an existing participant (shown as provisional until a human confirms).
- ask_question(text, evidence): add a question to the quiet inbox.
- resolve_question(question_id, answer_text, confidence, evidence): answer an
  open question from what was said in the meeting.

CARDS
- key_points: important statements, findings, and agreements-in-progress.
- decisions: things actually decided. Add only when the transcript shows a
  decision was made, not merely discussed.
- action_items: concrete follow-ups. Put the owner's participant id in
  data.owner_participant_id when the transcript supports one.
- risks: risks, blockers, and open disagreements. Optional data.severity
  ("low", "medium", or "high").
- timeline: the meeting's story beats. Set data.start_s to the meeting-seconds
  timestamp where the beat began (take it from the segment timestamps).
- user_notes: HUMAN-ONLY. Never add, update, or remove anything on this card.

EVIDENCE DISCIPLINE
Every operation cites evidence: transcript segment ids (they look like sg_xxxx)
copied EXACTLY from transcript lines you were given in this conversation. Never
invent or guess a segment id. Cite the few segments (at most {max_evidence})
that best support the claim. Ops citing unknown segment ids are rejected.

PROVISIONAL CONTENT AND PROTECTION
Everything you write appears as "proposed" until a human touches it. Items whose
status is "edited" or "confirmed", or which are pinned, are protected: your
updates and removals against them are rejected with reason human_edited — never
retry those targets; work around them. update_item and remove_item also require
the current base_revision; on revision_mismatch the current revision is echoed
back — re-read the state and re-emit against it only if the change is still
warranted.

QUESTIONS (QUIET INBOX)
Questions never interrupt anyone; they wait in a quiet inbox. Be sparing: ask
only thought-provoking, decision-relevant questions a busy participant would be
glad to see (a missing owner or deadline, an unstated assumption, an unresolved
disagreement). At most {max_open_questions} questions may be open at once. When
later audio answers an open question, call resolve_question: confidence at or
above {resolve} resolves it with an "answered from audio" badge; between
{suggest} and {resolve} it is stored as a greyed suggestion; below {suggest} it
is rejected. Report confidence honestly — never inflate it.

PARTICIPANTS AND SPEAKERS
Transcript lines carry a speaker label. Channel "mic" is the host ("Me").
Channel "loopback" is everyone else ("Others"), possibly split into Speaker-N
clusters by diarization. Infer identities only from clear evidence
(self-introductions, being addressed by name) and propose them with
suggest_participant_name. Names set by humans are authoritative — ops against
them are rejected with human_named; never fight that.

REJECTION HANDLING
Each op returns ok or a rejection reason (human_edited, revision_mismatch,
unknown_item, unknown_evidence, question_limit, ...). Rejections are normal:
adapt in your next round or checkpoint instead of repeating the same op.

STYLE
Be concise and concrete; write in the language of the meeting. Stay faithful to
what was said — never fabricate. Prefer updating your own existing items over
adding near-duplicates. When nothing meaningful changed, emit no operations at
all. Use American spelling.
"""

_CHECKPOINT_INSTRUCTIONS = """\
## INSTRUCTIONS
Update the dashboard to reflect the new transcript segments:
1. Adjust the current topic (set_topic) if discussion has moved on, and keep the
   rolling summary current.
2. Add genuinely new key points, decisions, action items, risks, and timeline
   beats. Do not duplicate existing items — update or remove your own items
   (with the correct base_revision) instead.
3. Cite evidence segment ids on every operation.
4. If the new transcript answers an open question, call resolve_question with
   your honest confidence.
5. Ask a new question only if it is genuinely valuable; the inbox stays quiet.
If nothing meaningful changed, emit no operations."""

_CONSOLIDATION_INSTRUCTIONS = """\
## INSTRUCTIONS — FINAL CONSOLIDATION PASS
The meeting has ended. The transcript above is the COMPLETE final transcript.
Finalize the dashboard as the durable record:
1. Review every card. Merge duplicates and remove stale or superseded items you
   authored (respect base_revision; leave human-touched items alone).
2. Make decisions and action items complete and precisely worded; give every
   action item an owner (data.owner_participant_id) when the transcript
   supports one.
3. Set the final topic and rewrite the rolling summary as a complete summary of
   the whole meeting.
4. Ensure the timeline card tells the meeting's story with data.start_s anchors.
5. Resolve any open question the transcript answers (resolve_question with
   honest confidence). Questions you cannot resolve stay open for the host —
   you cannot dismiss them.
6. Keep every evidence link valid — cite only segment ids that appear in the
   transcript above.
Do not ask new questions."""

#: Appended to the user prompt by the direct agent's JSON-mode fallback (used
#: when the provider/model does not support function tools).
JSON_FALLBACK_INSTRUCTIONS = """\
Respond ONLY with a single JSON object of the form {"ops": [...]} — no prose,
no code fences. Each element of "ops" is one operation object with an "op"
field, for example:
{"op": "add_item", "card": "decisions", "text": "...", "evidence": ["sg_..."]}
{"op": "update_item", "id": "it_...", "base_revision": 2,
 "set": {"text": "..."}, "evidence": ["sg_..."]}
{"op": "set_topic", "text": "...", "evidence": ["sg_..."]}
{"op": "ask_question", "text": "...", "evidence": ["sg_..."]}
{"op": "resolve_question", "question_id": "q_...", "answer_text": "...",
 "confidence": 0.9, "evidence": ["sg_..."]}
An empty {"ops": []} is valid when nothing changed."""


def build_system_prompt() -> str:
    """Build the standing system prompt for the meeting-intelligence agent.

    Returns:
        The complete system prompt, with policy numbers (question cap,
        confidence thresholds, evidence limits) taken from the state-patch
        validation layer.
    """
    return _SYSTEM_PROMPT_TEMPLATE.format(
        max_evidence=MAX_EVIDENCE_REFS,
        max_open_questions=MAX_OPEN_QUESTIONS,
        resolve=f"{RESOLVE_CONFIDENCE:g}",
        suggest=f"{SUGGEST_CONFIDENCE:g}",
    )


def _render_item(item: Dict[str, Any]) -> str:
    """Render one card item as a compact single line with targeting metadata."""
    flags = [f"rev={item.get('revision', 1)}", str(item.get("status", "proposed"))]
    if item.get("pinned"):
        flags.append("pinned")
    data = item.get("data") or {}
    extras = []
    if data.get("owner_participant_id"):
        extras.append(f"owner={data['owner_participant_id']}")
    if data.get("start_s") is not None:
        extras.append(f"start_s={data['start_s']}")
    if data.get("severity"):
        extras.append(f"severity={data['severity']}")
    extra_txt = f" ({', '.join(extras)})" if extras else ""
    text = (item.get("text") or "").replace("\n", " ").strip()
    if len(text) > _MAX_ITEM_TEXT_CHARS:
        text = text[: _MAX_ITEM_TEXT_CHARS - 3] + "..."
    return f"- [{item.get('id')} {' '.join(flags)}]{extra_txt} {text}"


def render_state_compact(state: Dict[str, Any]) -> str:
    """Render a state snapshot compactly for the checkpoint user prompt.

    Includes ids, revisions, statuses, and pin flags so the agent can target
    updates precisely and knows which items are protected.

    Args:
        state: A ``MeetingState.to_dict()`` snapshot.

    Returns:
        A multi-line, human-readable state rendering.
    """
    lines: List[str] = []
    lines.append(f"Title: {state.get('title') or '(untitled)'}")
    topic = (state.get("topic") or {}).get("current") or "(not set)"
    lines.append(f"Current topic: {topic}")
    summary = state.get("rolling_summary") or "(not set)"
    lines.append(f"Rolling summary: {summary}")

    lines.append("")
    lines.append("Participants:")
    participants = state.get("participants") or {}
    if participants:
        for pid, participant in participants.items():
            flags = [str(participant.get("kind", ""))]
            if participant.get("name_source") == "human":
                flags.append("named-by-human")
            elif participant.get("is_provisional"):
                flags.append("provisional")
            flags_txt = ", ".join(f for f in flags if f)
            lines.append(
                f"- [{pid}] {participant.get('display_name', '')} ({flags_txt})"
            )
    else:
        lines.append("- (none yet)")

    cards = state.get("cards") or {}
    for card in CARD_KEYS:
        items = [
            item for item in (cards.get(card) or [])
            if item.get("status") != "removed"
        ]
        label = f"{card} [HUMAN-ONLY]" if card == "user_notes" else card
        lines.append("")
        lines.append(f"Card {label} ({len(items)} items):")
        if items:
            lines.extend(_render_item(item) for item in items)
        else:
            lines.append("- (empty)")

    lines.append("")
    lines.append(build_question_guidance(state))
    return "\n".join(lines)


def format_segment_line(segment: Dict[str, Any],
                        participants: Dict[str, Any]) -> str:
    """Format one transcript segment as an evidence-citable prompt line.

    Args:
        segment: Segment dict (repository shape: ``id``, ``channel``,
            ``start_s``, ``text``, ``speaker_participant_id``, ...).
        participants: ``state['participants']`` mapping for name resolution.

    Returns:
        A line of the form ``[sg_id] [t=123.4s] [Speaker]: text``.
    """
    pid = segment.get("speaker_participant_id")
    participant = participants.get(pid) if pid else None
    if participant:
        speaker = participant.get("display_name") or pid
    else:
        speaker = "Me" if segment.get("channel") == "mic" else "Others"
    start_s = float(segment.get("start_s") or 0.0)
    text = (segment.get("text") or "").replace("\n", " ").strip()
    return f"[{segment.get('id')}] [t={start_s:.1f}s] [{speaker}]: {text}"


def build_checkpoint_user_prompt(state: Dict[str, Any],
                                 new_segments: List[Dict[str, Any]],
                                 is_consolidation: bool = False) -> str:
    """Build the user prompt for one checkpoint or the final consolidation.

    Args:
        state: A ``MeetingState.to_dict()`` snapshot (the rolling context).
        new_segments: Segment dicts added since the last checkpoint; for
            consolidation, the complete final transcript.
        is_consolidation: True for the end-of-meeting full pass.

    Returns:
        The complete user prompt: compact dashboard state, transcript lines,
        then the checkpoint or consolidation instructions.
    """
    participants = state.get("participants") or {}
    parts: List[str] = []
    parts.append("## CURRENT DASHBOARD STATE")
    parts.append(render_state_compact(state))
    parts.append("")
    parts.append(
        "## FULL MEETING TRANSCRIPT" if is_consolidation
        else "## NEW TRANSCRIPT SEGMENTS"
    )
    if new_segments:
        parts.extend(
            format_segment_line(segment, participants)
            for segment in new_segments
        )
    else:
        parts.append("(no new segments)")
    parts.append("")
    parts.append(
        _CONSOLIDATION_INSTRUCTIONS if is_consolidation
        else _CHECKPOINT_INSTRUCTIONS
    )
    return "\n".join(parts)
