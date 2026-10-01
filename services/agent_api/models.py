"""Public v1 response contracts, deliberately separate from database rows."""

from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):  # noqa: UP046 - source installs support Python 3.11
    items: list[T]
    next_cursor: str | None = None


class ApiStatus(BaseModel):
    api_version: Literal["1"] = "1"
    read_only: Literal[True] = True


class TranscriptionSummary(BaseModel):
    id: str
    title: str | None = None
    timestamp: str
    model: str
    source_name: str | None = None
    audio_duration: float | None = None
    origin_device_id: str | None = None
    origin_device_name: str | None = None
    preview: str


class Transcription(TranscriptionSummary):
    text: str
    raw_text: str | None = None
    cleanup_provider: str | None = None
    cleanup_model: str | None = None


class Meeting(BaseModel):
    id: str
    title: str
    status: str
    started_at: str
    ended_at: str | None = None
    state_seq: int
    origin_device_id: str | None = None
    origin_device_name: str | None = None


class Segment(BaseModel):
    id: str
    meeting_id: str
    start_s: float
    end_s: float
    channel: str
    text: str
    speaker_participant_id: str | None = None
    speaker_source: str


class Participant(BaseModel):
    id: str
    display_name: str
    kind: str = "others_cluster"


class InsightData(BaseModel):
    """Allowlisted card metadata; future internal fields stay private."""

    heading: str | None = None
    start_s: float | None = None
    owner_participant_id: str | None = None
    due_date: str | None = None
    severity: str | None = None


class Insight(BaseModel):
    id: str
    card: str
    text: str = ""
    status: str = "proposed"
    pinned: bool = False
    data: InsightData = Field(default_factory=InsightData)
    evidence: list[str] = Field(default_factory=list)


class Question(BaseModel):
    id: str
    text: str = ""
    status: str = "open"
    answer: str | None = None
    answer_source: str | None = None
    evidence: list[str] = Field(default_factory=list)


class Report(BaseModel):
    id: str
    title: str = ""
    request: str = ""
    status: str = "running"
    markdown: str = ""


class MeetingInsights(BaseModel):
    meeting_id: str
    state_seq: int
    snapshot_available: bool
    rolling_summary: str = ""
    rolling_summary_evidence: list[str] = Field(default_factory=list)
    participants: list[Participant] = Field(default_factory=list)
    cards: list[Insight] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    custom_reports: list[Report] = Field(default_factory=list)


class SearchHit(BaseModel):
    kind: Literal["transcription", "meeting", "segment"]
    id: str
    meeting_id: str | None = None
    title: str
    timestamp: str
    excerpt: str
    matched_field: Literal["text", "raw_text", "source_name", "title"]
    start_s: float | None = None
    end_s: float | None = None
    origin_device_id: str | None = None
    resource: str


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail
