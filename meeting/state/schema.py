"""In-memory meeting state document: the single source of truth for the dashboard.

The state document holds everything the dashboard renders except the
transcript itself (segments live in an append-only log streamed separately;
card items reference them by id as evidence anchors).

Serialization contract: ``MeetingState.to_dict()`` round-trips through
``MeetingState.from_dict()`` and is what gets snapshotted to
``meeting_sessions.state_json`` and sent to web clients in the WS ``hello``.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from meeting.time_utils import utc_now_iso

#: Cards rendered on the dashboard. ``timeline`` items use ``data.start_s``;
#: ``action_items`` use ``data.owner_participant_id``; ``risks`` may carry
#: ``data.severity``. ``live_notes`` blocks are the AI note taker's record:
#: ``data.heading`` carries the short section label and ``data.start_s`` the
#: meeting-clock stamp of the passage the note covers.
CARD_KEYS = (
    "key_points",
    "decisions",
    "action_items",
    "risks",
    "timeline",
    "live_notes",
    "user_notes",
)

#: Post-meeting cloud consolidation lifecycle (orthogonal to meeting status).
FINALIZATION_STATUSES = (
    "pending",
    "running",
    "completed",
    "disabled",
    "unavailable",
    "failed",
)
_TERMINAL_MEETING_STATUSES = frozenset({
    "ended", "failed", "needs_recovery",
})


def now_iso() -> str:
    """Current UTC instant; legacy persisted naive values remain readable."""
    return utc_now_iso()


def new_id(prefix: str) -> str:
    """Short unique id with a type prefix (``it_``, ``q_``, ``p_``, ``sg_``)."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class CardItem:
    """One entry on a dashboard card (key point, decision, action item, ...)."""
    id: str
    card: str
    text: str
    data: Dict[str, Any] = field(default_factory=dict)
    status: str = "proposed"
    author_type: str = "agent"
    author_id: Optional[str] = None
    pinned: bool = False
    revision: int = 1
    evidence: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def protected(self) -> bool:
        """True when agent ops may no longer modify this item."""
        return self.pinned or self.status in ("edited", "confirmed")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "card": self.card, "text": self.text,
            "data": dict(self.data), "status": self.status,
            "author_type": self.author_type, "author_id": self.author_id,
            "pinned": self.pinned, "revision": self.revision,
            "evidence": list(self.evidence),
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CardItem":
        return cls(
            id=d["id"], card=d["card"], text=d.get("text", ""),
            data=dict(d.get("data") or {}), status=d.get("status", "proposed"),
            author_type=d.get("author_type", "agent"),
            author_id=d.get("author_id"),
            pinned=bool(d.get("pinned", False)),
            revision=int(d.get("revision", 1)),
            evidence=list(d.get("evidence") or []),
            created_at=d.get("created_at", now_iso()),
            updated_at=d.get("updated_at", now_iso()),
        )


@dataclass
class Question:
    """A quiet-inbox question, with optional suggestion and resolution."""
    id: str
    text: str
    status: str = "open"
    suggested_answer: Optional[str] = None
    suggested_confidence: Optional[float] = None
    answer: Optional[str] = None
    answer_source: Optional[str] = None  # user | audio
    confidence: Optional[float] = None
    thread: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[str] = field(default_factory=list)
    asked_at: str = field(default_factory=now_iso)
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "text": self.text, "status": self.status,
            "suggested_answer": self.suggested_answer,
            "suggested_confidence": self.suggested_confidence,
            "answer": self.answer, "answer_source": self.answer_source,
            "confidence": self.confidence,
            "thread": list(self.thread), "evidence": list(self.evidence),
            "asked_at": self.asked_at, "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Question":
        return cls(
            id=d["id"], text=d.get("text", ""), status=d.get("status", "open"),
            suggested_answer=d.get("suggested_answer"),
            suggested_confidence=d.get("suggested_confidence"),
            answer=d.get("answer"), answer_source=d.get("answer_source"),
            confidence=d.get("confidence"),
            thread=list(d.get("thread") or []),
            evidence=list(d.get("evidence") or []),
            asked_at=d.get("asked_at", now_iso()),
            resolved_at=d.get("resolved_at"), resolved_by=d.get("resolved_by"),
        )


@dataclass
class Participant:
    """A person in the meeting: the host ("me"), a diarized remote-speaker
    cluster, or a joined dashboard guest."""
    id: str
    display_name: str
    kind: str = "others_cluster"
    name_source: str = "default"
    is_provisional: bool = False
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "display_name": self.display_name,
            "kind": self.kind, "name_source": self.name_source,
            "is_provisional": self.is_provisional,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Participant":
        return cls(
            id=d["id"], display_name=d.get("display_name", ""),
            kind=d.get("kind", "others_cluster"),
            name_source=d.get("name_source", "default"),
            is_provisional=bool(d.get("is_provisional", False)),
            created_at=d.get("created_at", now_iso()),
            updated_at=d.get("updated_at", now_iso()),
        )


@dataclass
class TopicState:
    """The evolving main topic plus its revision history."""
    current: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"current": self.current, "history": list(self.history)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TopicState":
        return cls(current=d.get("current", ""), history=list(d.get("history") or []))


@dataclass
class FinalizationState:
    """Optional post-meeting cloud consolidation outcome.

    Orthogonal to durable meeting status (``ended`` / ``needs_recovery``):
    capture and transcript durability can complete while final insights are
    still running, disabled, unavailable, or failed.
    """
    status: str = "pending"
    message: str = ""
    stage: str = ""
    current_step: int = 0
    total_steps: int = 0
    step_details: str = ""
    steps: List[Dict[str, Any]] = field(default_factory=list)
    summary_stats: Dict[str, Any] = field(default_factory=dict)
    #: Desktop-card visibility only. Does not change pipeline outcome: an
    #: incomplete meeting stays incomplete, and Past Meetings still lists it.
    card_deferred: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "stage": self.stage,
            "current_step": self.current_step,
            "total_steps": self.total_steps,
            "step_details": self.step_details,
            "steps": [dict(s) for s in self.steps],
            "summary_stats": dict(self.summary_stats),
            "card_deferred": bool(self.card_deferred),
        }

    def history_pill(self, *, meeting_status: str = "") -> Optional[tuple[str, str]]:
        """Return ``(label, tone)`` for compact history list pills.

        Args:
            meeting_status: Meeting lifecycle status. Live sessions omit a pill.

        Returns:
            A label/tone pair, or ``None`` when the row should show no insights
            pill (in-flight work, or a live meeting).
        """
        if meeting_status in {"active", "paused", "ending"}:
            return None
        failed_steps = any(
            str(step.get("status") or "") == "failed"
            for step in (self.steps or [])
        )
        incomplete = bool(failed_steps) or self.status in {"failed", "unavailable"}
        if self.card_deferred and incomplete:
            return ("Saved for later", "warning")
        if self.status == "failed" or (self.status == "completed" and failed_steps):
            return ("Incomplete", "warning")
        if self.status == "unavailable":
            return ("Unavailable", "warning")
        if self.status == "completed":
            return ("Ready", "success")
        if self.status == "disabled":
            return ("Off", "neutral")
        return None

    @classmethod
    def default_for_cloud(cls, cloud_enabled: bool) -> "FinalizationState":
        """Initial finalization for a newly created meeting.

        Args:
            cloud_enabled: Whether cloud intelligence is on for the meeting.

        Returns:
            ``pending`` when cloud is enabled, otherwise ``disabled``.
        """
        if cloud_enabled:
            return cls(status="pending", message="")
        return cls(
            status="disabled",
            message="Cloud intelligence is off for this meeting.",
        )

    @classmethod
    def infer_legacy(
        cls,
        *,
        cloud_enabled: bool,
        meeting_status: str,
    ) -> "FinalizationState":
        """Infer finalization for snapshots that predate this field.

        Args:
            cloud_enabled: Persisted cloud-intelligence flag.
            meeting_status: Persisted meeting lifecycle status.

        Returns:
            A conservative finalization value that never claims historical
            cloud success when the outcome was never recorded.
        """
        if not cloud_enabled:
            return cls(
                status="disabled",
                message="Cloud intelligence is off for this meeting.",
            )
        if meeting_status in _TERMINAL_MEETING_STATUSES:
            return cls(
                status="unavailable",
                message=(
                    "Final cloud insights were not recorded for this meeting."
                ),
            )
        return cls(status="pending", message="")

    @classmethod
    def coerce(
        cls,
        value: Any,
        *,
        cloud_enabled: bool = False,
        meeting_status: str = "active",
    ) -> "FinalizationState":
        """Parse or fall back from a persisted/API finalization value.

        Args:
            value: ``None``, a ``FinalizationState``, or a ``{status, message}``
                mapping. Unknown shapes and statuses fall back to legacy
                inference so corrupt data never reaches the UI unchanged.
            cloud_enabled: Meeting cloud flag used for legacy inference.
            meeting_status: Meeting lifecycle status used for legacy inference.

        Returns:
            A validated ``FinalizationState``.
        """
        if isinstance(value, cls):
            if value.status in FINALIZATION_STATUSES:
                return cls(
                    status=value.status,
                    message=str(value.message or ""),
                    stage=str(value.stage or ""),
                    current_step=int(value.current_step or 0),
                    total_steps=int(value.total_steps or 0),
                    step_details=str(value.step_details or ""),
                    steps=list(value.steps or []),
                    summary_stats=dict(value.summary_stats or {}),
                    card_deferred=_coerce_card_deferred(
                        getattr(value, "card_deferred", False)
                    ),
                )
            return cls.infer_legacy(
                cloud_enabled=cloud_enabled, meeting_status=meeting_status,
            )
        if value is None:
            return cls.infer_legacy(
                cloud_enabled=cloud_enabled, meeting_status=meeting_status,
            )
        if not isinstance(value, dict):
            return cls.infer_legacy(
                cloud_enabled=cloud_enabled, meeting_status=meeting_status,
            )
        status = value.get("status")
        if status not in FINALIZATION_STATUSES:
            return cls.infer_legacy(
                cloud_enabled=cloud_enabled, meeting_status=meeting_status,
            )
        message = value.get("message", "")
        if message is None:
            message = ""
        steps_val = value.get("steps")
        steps_list = [dict(s) for s in steps_val] if isinstance(steps_val, (list, tuple)) else []
        stats_val = value.get("summary_stats")
        stats_dict = dict(stats_val) if isinstance(stats_val, dict) else {}
        return cls(
            status=str(status),
            message=str(message),
            stage=str(value.get("stage") or ""),
            current_step=int(value.get("current_step") or 0),
            total_steps=int(value.get("total_steps") or 0),
            step_details=str(value.get("step_details") or ""),
            steps=steps_list,
            summary_stats=stats_dict,
            card_deferred=_coerce_card_deferred(value.get("card_deferred", False)),
        )

    @classmethod
    def normalize_historical(
        cls,
        value: Any,
        *,
        cloud_enabled: bool,
        meeting_status: str,
    ) -> "FinalizationState":
        """Coerce finalization for archived/historical serving after restart.

        Live meetings may legitimately expose ``status=ended`` with
        ``finalization=running`` during the bounded consolidation pass. Once a
        snapshot is only historical (archive dashboard / stored REST state),
        an interrupted ``running``/``pending`` value must become a durable
        terminal outcome so the UI never claims work is still in flight.

        Args:
            value: Persisted finalization payload or ``None``.
            cloud_enabled: Meeting cloud-intelligence flag.
            meeting_status: Persisted meeting lifecycle status.

        Returns:
            A finalization value safe to show for non-live history views.
        """
        fin = cls.coerce(
            value,
            cloud_enabled=cloud_enabled,
            meeting_status=meeting_status,
        )
        if meeting_status not in _TERMINAL_MEETING_STATUSES:
            return fin
        if fin.status not in {"pending", "running"}:
            return fin
        if not cloud_enabled:
            return cls(
                status="disabled",
                message="Cloud intelligence is off for this meeting.",
                stage=fin.stage,
                current_step=fin.current_step,
                total_steps=fin.total_steps,
                step_details=fin.step_details,
                steps=list(fin.steps or []),
                summary_stats=fin.summary_stats,
                card_deferred=fin.card_deferred,
            )
        if fin.status == "running":
            interrupted_steps = []
            for step in fin.steps or []:
                row = dict(step)
                if row.get("status") in {"running", "pending"}:
                    row["status"] = "failed"
                    row["detail"] = (
                        row.get("detail")
                        or "Interrupted before this step finished."
                    )
                interrupted_steps.append(row)
            return cls(
                status="failed",
                message=(
                    "Final cloud insights were interrupted before they "
                    "finished."
                ),
                stage=fin.stage,
                current_step=fin.current_step,
                total_steps=fin.total_steps,
                step_details=fin.step_details,
                steps=interrupted_steps,
                summary_stats=fin.summary_stats,
                card_deferred=fin.card_deferred,
            )
        return cls(
            status="unavailable",
            message=(
                "Final cloud insights were not recorded for this meeting."
            ),
            stage=fin.stage,
            current_step=fin.current_step,
            total_steps=fin.total_steps,
            step_details=fin.step_details,
            steps=list(fin.steps or []),
            summary_stats=fin.summary_stats,
            card_deferred=fin.card_deferred,
        )


def _coerce_card_deferred(value: Any) -> bool:
    """Parse the desktop-card deferral flag; unknown shapes stay visible."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return False


def finalization_from_meeting_row(meeting: Dict[str, Any]) -> FinalizationState:
    """Normalize the insights payload stored on a repository meeting row.

    Args:
        meeting: Repository meeting dict, possibly including ``state_json``.

    Returns:
        A historical finalization value safe to show on list UIs.
    """
    raw = meeting.get("state_json")
    data: Dict[str, Any] = {}
    if isinstance(raw, dict):
        data = raw
    elif raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict):
            data = parsed
    return FinalizationState.normalize_historical(
        data.get("finalization"),
        cloud_enabled=bool(
            data.get("cloud_enabled", meeting.get("cloud_enabled"))
        ),
        meeting_status=str(meeting.get("status") or "ended"),
    )


def compact_finalization_list_fields(meeting: Dict[str, Any]) -> Dict[str, Any]:
    """Public list-row fields derived from a meeting's finalization snapshot.

    Args:
        meeting: Repository meeting dict.

    Returns:
        Compact fields safe to expose on meeting-list APIs. Does not include
        ``state_json`` or step details.
    """
    status = str(meeting.get("status") or "ended")
    fin = finalization_from_meeting_row(meeting)
    fields: Dict[str, Any] = {
        "finalization_status": fin.status,
        "finalization_deferred": bool(fin.card_deferred),
    }
    pill = fin.history_pill(meeting_status=status)
    if pill:
        fields["insights_pill"] = pill[0]
        fields["insights_tone"] = pill[1]
    return fields


@dataclass
class MeetingState:
    """The complete dashboard document for one meeting."""
    meeting_id: str
    seq: int = 0
    status: str = "active"  # active | paused | ending | ended | failed | needs_recovery
    cloud_enabled: bool = False
    intelligence_online: bool = False
    diarization_available: bool = False
    title: str = ""
    topic: TopicState = field(default_factory=TopicState)
    rolling_summary: str = ""
    rolling_summary_evidence: List[str] = field(default_factory=list)
    capture: Dict[str, Any] = field(default_factory=lambda: {
        "mic_available": False,
        "loopback_available": False,
        "message": "",
    })
    participants: Dict[str, Participant] = field(default_factory=dict)
    cards: Dict[str, List[CardItem]] = field(
        default_factory=lambda: {k: [] for k in CARD_KEYS}
    )
    questions: Dict[str, Question] = field(default_factory=dict)
    finalization: FinalizationState = field(default_factory=FinalizationState)
    report_views: List[str] = field(
        default_factory=lambda: ["ribbon", "brief", "signal"]
    )

    def find_item(self, item_id: str) -> Optional[CardItem]:
        """Locate a card item by id across all cards."""
        for items in self.cards.values():
            for item in items:
                if item.id == item_id:
                    return item
        return None

    def open_question_count(self) -> int:
        return sum(1 for q in self.questions.values() if q.status == "open")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "meeting_id": self.meeting_id,
            "seq": self.seq,
            "status": self.status,
            "cloud_enabled": self.cloud_enabled,
            "intelligence_online": self.intelligence_online,
            "diarization_available": self.diarization_available,
            "title": self.title,
            "topic": self.topic.to_dict(),
            "rolling_summary": self.rolling_summary,
            "rolling_summary_evidence": list(self.rolling_summary_evidence),
            "capture": dict(self.capture),
            "participants": {pid: p.to_dict() for pid, p in self.participants.items()},
            "cards": {
                card: [item.to_dict() for item in items]
                for card, items in self.cards.items()
            },
            "questions": [q.to_dict() for q in self.questions.values()],
            "finalization": self.finalization.to_dict(),
            "report_views": list(self.report_views),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MeetingState":
        cloud_enabled = bool(d.get("cloud_enabled", False))
        meeting_status = d.get("status", "active")
        # Legacy snapshots omit finalization; infer rather than invent success.
        if "finalization" in d:
            finalization = FinalizationState.coerce(
                d.get("finalization"),
                cloud_enabled=cloud_enabled,
                meeting_status=meeting_status,
            )
        else:
            finalization = FinalizationState.infer_legacy(
                cloud_enabled=cloud_enabled,
                meeting_status=meeting_status,
            )
        state = cls(
            meeting_id=d["meeting_id"],
            seq=int(d.get("seq", 0)),
            status=meeting_status,
            cloud_enabled=cloud_enabled,
            intelligence_online=bool(d.get("intelligence_online", False)),
            diarization_available=bool(d.get("diarization_available", False)),
            title=d.get("title", ""),
            topic=TopicState.from_dict(d.get("topic") or {}),
            rolling_summary=d.get("rolling_summary", ""),
            rolling_summary_evidence=list(
                d.get("rolling_summary_evidence") or []
            ),
            capture={
                "mic_available": bool(
                    (d.get("capture") or {}).get("mic_available", False)
                ),
                "loopback_available": bool(
                    (d.get("capture") or {}).get("loopback_available", False)
                ),
                "message": str((d.get("capture") or {}).get("message", "")),
            },
            finalization=finalization,
            report_views=list(d.get("report_views") or ["ribbon", "brief", "signal"]),
        )
        for pid, pd in (d.get("participants") or {}).items():
            state.participants[pid] = Participant.from_dict(pd)
        cards = d.get("cards") or {}
        for card in CARD_KEYS:
            state.cards[card] = [CardItem.from_dict(i) for i in cards.get(card, [])]
        for qd in d.get("questions") or []:
            q = Question.from_dict(qd)
            state.questions[q.id] = q
        return state
