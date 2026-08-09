"""In-memory meeting state document: the single source of truth for the dashboard.

The state document holds everything the dashboard renders except the
transcript itself (segments live in an append-only log streamed separately;
card items reference them by id as evidence anchors).

Serialization contract: ``MeetingState.to_dict()`` round-trips through
``MeetingState.from_dict()`` and is what gets snapshotted to
``meeting_sessions.state_json`` and sent to web clients in the WS ``hello``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

#: Cards rendered on the dashboard. ``timeline`` items use ``data.start_s``;
#: ``action_items`` use ``data.owner_participant_id``; ``risks`` may carry
#: ``data.severity``.
CARD_KEYS = (
    "key_points",
    "decisions",
    "action_items",
    "risks",
    "timeline",
    "user_notes",
)

ITEM_STATUSES = ("proposed", "edited", "confirmed", "removed")
QUESTION_STATUSES = ("open", "resolved", "dismissed")
PARTICIPANT_KINDS = ("me", "others_cluster", "guest")
NAME_SOURCES = ("default", "human", "agent_inferred")
ACTOR_TYPES = ("agent", "user", "host", "system")


def now_iso() -> str:
    """Current wall-clock time in the project's ISO-string convention."""
    return datetime.now().isoformat()


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
class MeetingState:
    """The complete dashboard document for one meeting."""
    meeting_id: str
    seq: int = 0
    status: str = "active"  # active | paused | ended | failed | needs_recovery
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
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MeetingState":
        state = cls(
            meeting_id=d["meeting_id"],
            seq=int(d.get("seq", 0)),
            status=d.get("status", "active"),
            cloud_enabled=bool(d.get("cloud_enabled", False)),
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
