"""Canned demo meeting used by developer mode.

Seeds a live ``MeetingEngine`` with a multi-speaker transcript and a handful
of mid-meeting cards so End can exercise polish, state repair, and the final
report without capturing audio.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from meeting.interfaces import CHANNEL_LOOPBACK, CHANNEL_MIC, TranscriptSegment
from meeting.state.schema import new_id

logger = logging.getLogger(__name__)

DEMO_TITLE = "Demo Planning Sync"
DEMO_NOTE = "Demo meeting loaded — no audio is being captured."

# (speaker_key, start_s, end_s, text). ``me`` is the host mic; others are
# loopback speakers. A few lines keep ASR-like artifacts so transcript polish
# has something to clean.
_TURNS: Tuple[Tuple[str, float, float, str], ...] = (
    ("me", 4.0, 12.0,
     "Welcome everyone. Thanks for making time for this planning sync."),
    ("jordan", 13.0, 18.5,
     "Yeah thanks for setting this up. I have the Q3 notes ready."),
    ("sam", 19.5, 25.0,
     "Same here. Can we start with the beta date?"),
    ("me", 26.0, 38.0,
     "um so I I think we should ship the beta in June. That's the date "
     "we floated last week."),
    ("jordan", 39.5, 52.0,
     "June works if we freeze the API by May fifteenth. Otherwise the "
     "mobile team slips."),
    ("sam", 53.0, 64.0,
     "I'm good with June. We we decided to go with FastAPI last Thursday, "
     "right?"),
    ("me", 65.0, 78.0,
     "Yes. FastAPI for the dashboard backend, React for the UI. Let's "
     "treat that as a decision."),
    ("jordan", 79.0, 94.0,
     "I'll draft the RFC this week. Who reviews it?"),
    ("me", 95.0, 108.0,
     "I can review it. Target Friday so we can socialize it Monday."),
    ("sam", 109.0, 128.0,
     "One risk: the vendor delay on the speech model license. If that "
     "slips we we have to ship with the fallback model."),
    ("jordan", 129.0, 145.0,
     "Yeah that's the main risk. Can we get a drop-dead date from them?"),
    ("me", 146.0, 162.0,
     "I'll ping them today. If we don't hear by next Wednesday we switch "
     "to the fallback and document it in the RFC."),
    ("sam", 163.0, 178.0,
     "Should the RFC also cover guest links and the LAN bind option?"),
    ("jordan", 179.0, 196.0,
     "Yes, and retention. People asked how long we keep meeting audio."),
    ("me", 197.0, 214.0,
     "Keep audio for fourteen days unless the host exports. That's the "
     "current proposal."),
    ("sam", 215.0, 232.0,
     "Works for me. Is the June deadline firm? Leadership keeps asking."),
    ("me", 233.0, 248.0,
     "It's firm unless the vendor miss forces a fallback. Let's keep "
     "that as an open question until I hear back."),
    ("jordan", 249.0, 268.0,
     "Timeline so far: kickoff is done, API freeze May fifteenth, beta "
     "in June, RFC draft this week."),
    ("sam", 269.0, 286.0,
     "I'll take the mobile client spike after the RFC lands. Need the "
     "auth section first."),
    ("me", 287.0, 304.0,
     "Noted. Anything else before we wrap the planning part?"),
    ("jordan", 305.0, 322.0,
     "Just the action items: I draft the RFC, you review, you chase the "
     "vendor, Sam owns the mobile spike."),
    ("sam", 323.0, 338.0,
     "And we should flag the license risk as high until we get a date."),
    ("me", 339.0, 356.0,
     "Agreed. I'll also add a timeline beat for kickoff so the dashboard "
     "isn't empty. Thanks everyone."),
    ("jordan", 357.0, 366.0,
     "Thanks. I'll send the RFC outline this afternoon."),
    ("sam", 367.0, 376.0,
     "Sounds good. Talk Friday."),
)

_SPEAKERS: Tuple[Tuple[str, str, str], ...] = (
    ("jordan", "Jordan", "others_cluster"),
    ("sam", "Sam", "others_cluster"),
)


def seed_demo_meeting(
    *,
    meeting_id: str,
    me_participant_id: Optional[str],
    store: Any,
    repository: Any,
    clock: Any = None,
    title: Optional[str] = None,
) -> Dict[str, Any]:
    """Insert the canned transcript and mid-meeting cards.

    Args:
        meeting_id: Active meeting session id.
        me_participant_id: Host participant id created at engine start.
        store: ``MeetingStateStore`` for the live meeting.
        repository: Meeting repository used to persist segments.
        clock: Optional ``MeetingClock``; when given, meeting time is
            re-anchored at the last transcript offset.
        title: Meeting title override. Defaults to ``DEMO_TITLE``.

    Returns:
        ``{"segment_count", "last_end_s", "participant_ids"}``.

    Raises:
        RuntimeError: When participants or segments cannot be seeded.
    """
    speaker_ids = {"me": me_participant_id}
    participant_ops: List[Dict[str, Any]] = [
        {
            "op": "set_title",
            "text": (title or DEMO_TITLE).strip() or DEMO_TITLE,
        }
    ]
    for key, name, kind in _SPEAKERS:
        participant_ops.append({
            "op": "upsert_participant",
            "display_name": name,
            "kind": kind,
            "is_provisional": False,
        })
    results = store.apply("system", "demo", participant_ops)
    for key, result in zip(
        [None] + [row[0] for row in _SPEAKERS],
        results,
    ):
        if not result.ok:
            raise RuntimeError(
                f"Demo seed failed on {result.op.get('op')}: {result.reason}"
            )
        if key is None:
            continue
        speaker_ids[key] = result.effect["participant"]["id"]
    if not speaker_ids.get("me"):
        raise RuntimeError("Demo seed needs a host participant")

    segments, segment_ids = _build_segments(meeting_id, speaker_ids)
    repository.add_segments(segments)
    last_end_s = segments[-1].end_s if segments else 0.0

    card_ops = _card_ops(segment_ids)
    card_results = store.apply("system", "demo", card_ops)
    failed = [r for r in card_results if not r.ok]
    if failed:
        logger.warning(
            "Demo seed dropped %d card ops: %s",
            len(failed),
            ", ".join(f"{r.op.get('op')}:{r.reason}" for r in failed),
        )

    followup = _followup_ops(card_results)
    if followup:
        pin_results = store.apply("system", "demo", followup)
        pin_failed = [r for r in pin_results if not r.ok]
        if pin_failed:
            logger.warning(
                "Demo seed dropped %d follow-up ops: %s",
                len(pin_failed),
                ", ".join(
                    f"{r.op.get('op')}:{r.reason}" for r in pin_failed
                ),
            )

    if clock is not None and last_end_s > 0:
        resume = getattr(clock, "resume_from_recovery", None)
        if callable(resume):
            resume(last_end_s)

    return {
        "segment_count": len(segments),
        "last_end_s": last_end_s,
        "participant_ids": dict(speaker_ids),
    }


def _build_segments(
    meeting_id: str,
    speaker_ids: Dict[str, Optional[str]],
) -> Tuple[List[TranscriptSegment], List[str]]:
    """Build transcript segments from the canned turns."""
    segments: List[TranscriptSegment] = []
    segment_ids: List[str] = []
    for key, start_s, end_s, text in _TURNS:
        segment_id = new_id("sg")
        is_host = key == "me"
        segments.append(TranscriptSegment(
            segment_id=segment_id,
            meeting_id=meeting_id,
            chunk_id=None,
            channel=CHANNEL_MIC if is_host else CHANNEL_LOOPBACK,
            start_s=start_s,
            end_s=end_s,
            text=text,
            speaker_participant_id=speaker_ids.get(key),
            speaker_source="channel" if is_host else "diarizer",
        ))
        segment_ids.append(segment_id)
    return segments, segment_ids


def _card_ops(segment_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """Mid-meeting cards/questions that End's report can revise."""
    def evidence(*indexes: int) -> List[str]:
        return [segment_ids[i] for i in indexes if 0 <= i < len(segment_ids)]

    return [
        {
            "op": "set_topic",
            "text": "Q3 roadmap and June beta",
            "evidence": evidence(3, 5),
        },
        {
            "op": "set_rolling_summary",
            "text": (
                "The team is aligning on a June beta, FastAPI for the "
                "backend, and an RFC this week. A vendor license delay "
                "is the main risk."
            ),
            "evidence": evidence(3, 5, 9),
        },
        {
            "op": "add_item",
            "card": "key_points",
            "text": "Ship the beta in June",
            "evidence": evidence(3),
        },
        {
            "op": "add_item",
            "card": "decisions",
            "text": "Use FastAPI for the dashboard backend",
            "evidence": evidence(5, 6),
        },
        {
            "op": "add_item",
            "card": "action_items",
            "text": "Draft the RFC this week",
            "data": {},
            "evidence": evidence(7),
        },
        {
            "op": "add_item",
            "card": "risks",
            "text": "Vendor delay on the speech model license",
            "data": {"severity": "high"},
            "evidence": evidence(9),
        },
        {
            "op": "add_item",
            "card": "timeline",
            "text": "Kickoff done",
            "data": {"start_s": 4.0},
            "evidence": evidence(0),
        },
        {
            "op": "ask_question",
            "text": "Is the June deadline firm?",
            "evidence": evidence(15),
        },
    ]


def _followup_ops(card_results: Sequence[Any]) -> List[Dict[str, Any]]:
    """Pin/confirm the FastAPI decision so it survives proposed-card cleanup."""
    ops: List[Dict[str, Any]] = []
    for result in card_results:
        if not result.ok or result.op.get("op") != "add_item":
            continue
        if result.op.get("card") != "decisions":
            continue
        item_id = result.target_id
        if not item_id:
            continue
        ops.append({"op": "confirm_item", "id": item_id})
        ops.append({"op": "pin_item", "id": item_id})
    return ops
