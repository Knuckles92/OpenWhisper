"""Boundary protocols for every Meeting Mode subsystem.

Each subsystem (capture, spool, ASR, diarization, agent core, state store,
web transport, persistence) is used exclusively through the protocols defined
here so implementations can be swapped — and so the package can become a
standalone application without rewrites.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable

import numpy as np

# ---------------------------------------------------------------------------
# Shared value types
# ---------------------------------------------------------------------------

#: Channel identifiers for the two capture streams.
CHANNEL_MIC = "mic"
CHANNEL_LOOPBACK = "loopback"
CHANNELS = (CHANNEL_MIC, CHANNEL_LOOPBACK)


@dataclass
class CaptureBlock:
    """A block of raw audio frames delivered by a capture source.

    Attributes:
        channel: ``mic`` or ``loopback``.
        frames: Mono int16 numpy array at ``sample_rate``.
        sample_rate: Sample rate of ``frames`` in Hz.
        t_mono: ``time.monotonic()`` timestamp of the first frame.
    """
    channel: str
    frames: np.ndarray
    sample_rate: int
    t_mono: float


@dataclass
class SpooledChunk:
    """A finalized on-disk audio chunk registered for transcription.

    Attributes:
        chunk_id: Database row id (``meeting_audio_chunks.id``).
        meeting_id: Owning meeting session id.
        channel: ``mic`` or ``loopback``.
        seq: Per-(meeting, channel) sequence number.
        file_path: Absolute path to the 16 kHz mono int16 WAV file.
        start_s: Meeting-clock offset of the first sample, in seconds.
        duration_s: Chunk duration in seconds.
        sample_rate: Sample rate of the WAV file (16000).
    """
    chunk_id: int
    meeting_id: str
    channel: str
    seq: int
    file_path: str
    start_s: float
    duration_s: float
    sample_rate: int


@dataclass
class TranscriptSegment:
    """A timestamped transcript segment produced by the ASR engine.

    ``start_s``/``end_s`` are meeting-clock offsets (chunk offset + intra-chunk
    segment timing), making segment ids stable evidence anchors.
    """
    segment_id: str
    meeting_id: str
    chunk_id: Optional[int]
    channel: str
    start_s: float
    end_s: float
    text: str
    speaker_participant_id: Optional[str] = None
    speaker_source: str = "channel"  # channel | diarizer | human
    speaker_pinned: bool = False
    embedding: Optional[bytes] = None


@dataclass
class OpResult:
    """Outcome of a single state-patch operation.

    Attributes:
        ok: Whether the op was applied.
        op: The op as submitted (normalized).
        reason: Rejection reason when ``ok`` is False (e.g. ``human_edited``,
            ``revision_mismatch``, ``unknown_op``).
        target_id: Id of the entity the op affected (item/question/participant).
        effect: Full post-apply entity dict for broadcast/optimistic UI, or a
            small dict describing the change (e.g. topic/rolling summary text).
        inverse: Precomputed inverse op enabling host undo, when derivable.
        current_revision: Echoed current revision on ``revision_mismatch``.
        seq: State sequence number assigned when the op was applied (set by
            the store; None for rejected ops).
    """
    ok: bool
    op: Dict[str, Any]
    reason: Optional[str] = None
    target_id: Optional[str] = None
    effect: Optional[Dict[str, Any]] = None
    inverse: Optional[Dict[str, Any]] = None
    current_revision: Optional[int] = None
    seq: Optional[int] = None


@dataclass
class AgentConfig:
    """Configuration handed to an agent core at meeting start."""
    meeting_id: str
    provider: str
    model: str
    api_key: Optional[str]
    system_prompt: str
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckpointPayload:
    """Input for one agent checkpoint (or the final consolidation pass).

    Attributes:
        request_id: Unique id for cancellation/correlation.
        state_snapshot: Full ``MeetingState`` dict (the rolling context).
        new_segments: Transcript segments added since the previous checkpoint
            (for ``consolidate``: the complete final transcript).
        is_consolidation: True for the end-of-meeting full pass.
        is_polish: True for a transcript-text cleanup pass (``revise_segment_text``
            only; does not advance the card-checkpoint cursor).
        is_notes: True for a note-taker pass (``live_notes`` card ops only;
            does not advance the card-checkpoint cursor).
    """
    request_id: str
    state_snapshot: Dict[str, Any]
    new_segments: List[Dict[str, Any]]
    is_consolidation: bool = False
    is_polish: bool = False
    is_notes: bool = False


@dataclass
class AgentResult:
    """Outcome of a checkpoint: per-op results plus usage metadata."""
    ok: bool
    op_results: List[OpResult] = field(default_factory=list)
    error: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

@runtime_checkable
class CaptureSource(Protocol):
    """A single audio input stream (microphone or WASAPI loopback)."""

    channel: str

    def start(self, on_block: Callable[[CaptureBlock], None]) -> None:
        """Open the stream and begin delivering blocks to ``on_block``.

        ``on_block`` is invoked from the audio thread; it must be fast and
        must never raise.
        """
        ...

    def stop(self) -> None:
        """Stop the stream and release the device."""
        ...

    def is_active(self) -> bool:
        """True while the underlying stream is delivering frames."""
        ...


# ---------------------------------------------------------------------------
# Spool
# ---------------------------------------------------------------------------

@runtime_checkable
class ChunkSpool(Protocol):
    """Durable chunked WAV writer for one channel of a meeting."""

    def feed(self, block: CaptureBlock) -> None:
        """Append captured frames; finalize a chunk when cut criteria are met."""
        ...

    def flush(self) -> Optional[SpooledChunk]:
        """Finalize and register any pending partial chunk (end of meeting)."""
        ...


# ---------------------------------------------------------------------------
# ASR
# ---------------------------------------------------------------------------

@runtime_checkable
class AsrEngine(Protocol):
    """Background transcription of spooled chunks with per-chunk retry."""

    def start(
        self,
        on_chunk_result: Callable[[SpooledChunk, List[TranscriptSegment]], None],
    ) -> None:
        """Start the worker; the callback durably commits each chunk result."""
        ...

    def enqueue(self, chunk: SpooledChunk) -> None:
        """Queue a finalized chunk for transcription."""
        ...

    def drain(self, timeout_s: float) -> bool:
        """Block until the queue is empty or ``timeout_s`` elapses."""
        ...

    def stop(self) -> None:
        """Stop the worker and release the model."""
        ...


# ---------------------------------------------------------------------------
# Diarization
# ---------------------------------------------------------------------------

@runtime_checkable
class Diarizer(Protocol):
    """Progressive speaker separation for the loopback channel."""

    def assign(self, segment: TranscriptSegment, audio: np.ndarray,
               sample_rate: int) -> Optional[str]:
        """Return a participant id for the segment, or None to keep channel-level."""
        ...

    def set_relabel_callback(
        self, cb: Callable[[List[Dict[str, Any]]], None]
    ) -> None:
        """Register a callback receiving ``reassign_segment_speaker`` op dicts
        emitted by periodic re-clustering."""
        ...

    def pin(self, segment_id: str, participant_id: str) -> None:
        """Record a human correction as clustering supervision."""
        ...

    def is_available(self) -> bool:
        """False when the embedding model is missing or the diarizer degraded."""
        ...


# ---------------------------------------------------------------------------
# Agent core
# ---------------------------------------------------------------------------

@runtime_checkable
class AgentToolHost(Protocol):
    """Callbacks the agent core uses to act on meeting state.

    Implemented by ``MeetingEngine``; every mutation is validated by the
    state-patch layer regardless of what the agent requested.
    """

    def apply_agent_ops(self, ops: List[Dict[str, Any]]) -> List[OpResult]:
        """Validate and apply state-patch ops on behalf of the agent."""
        ...

    def ask_question(self, text: str, evidence: List[str]) -> OpResult:
        """Add a question to the quiet inbox."""
        ...

    def resolve_question(self, question_id: str, answer_text: str,
                         confidence: float, evidence: List[str]) -> OpResult:
        """Answer an open question from audio evidence (thresholded)."""
        ...


@runtime_checkable
class AgentCore(Protocol):
    """A meeting-intelligence backend (Pi sidecar, direct OpenRouter, ...)."""

    def initialize(self, cfg: AgentConfig, tools: AgentToolHost) -> None:
        """Prepare the core for a meeting (spawn sidecar, probe capabilities)."""
        ...

    def checkpoint(self, payload: CheckpointPayload) -> AgentResult:
        """Run one rolling update. Blocking; called from a worker thread."""
        ...

    def consolidate(self, payload: CheckpointPayload) -> AgentResult:
        """Run the end-of-meeting full pass. Blocking."""
        ...

    def cancel(self) -> None:
        """Cancel any in-flight request."""
        ...

    def is_healthy(self) -> bool:
        """True when the core can accept checkpoints."""
        ...

    def shutdown(self) -> None:
        """Release all resources (terminate sidecar process, close clients)."""
        ...


# ---------------------------------------------------------------------------
# State store
# ---------------------------------------------------------------------------

@runtime_checkable
class StateStore(Protocol):
    """Single-writer meeting-state document with audit and fan-out."""

    def apply(self, actor_type: str, actor_id: Optional[str],
              ops: List[Dict[str, Any]]) -> List[OpResult]:
        """Validate and apply ops; bump seq; persist; notify subscribers."""
        ...

    def snapshot(self) -> Dict[str, Any]:
        """Deep-copied full state dict (safe to serialize)."""
        ...

    def subscribe(self, cb: Callable[[int, List[OpResult]], None]) -> None:
        """Register a listener invoked after each applied batch."""
        ...

    def unsubscribe(self, cb: Callable[[int, List[OpResult]], None]) -> None:
        ...


# ---------------------------------------------------------------------------
# Web transport
# ---------------------------------------------------------------------------

@runtime_checkable
class TransportServer(Protocol):
    """The localhost/LAN web server hosting the dashboard."""

    def start(self) -> str:
        """Start serving; returns the base URL (scheme://host:port)."""
        ...

    def stop(self) -> None:
        ...

    def broadcast(self, message: Dict[str, Any]) -> None:
        """Push a JSON-serializable message to all connected clients."""
        ...


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@runtime_checkable
class MeetingRepository(Protocol):
    """All SQL for meeting persistence, behind one seam."""

    # -- meetings (named to avoid clashing with DatabaseManager.get_session) --
    def create_meeting(self, **fields) -> None: ...
    def update_meeting(self, meeting_id: str, **fields) -> None: ...
    def rename_meeting(self, meeting_id: str, title: str) -> None: ...
    def replace_tokens(self, meeting_id: str, host_token: str,
                       guest_token: str) -> None: ...
    def get_meeting(self, meeting_id: str) -> Optional[Dict[str, Any]]: ...
    def list_meetings(self) -> List[Dict[str, Any]]: ...
    def delete_meeting(self, meeting_id: str) -> None: ...
    def heartbeat(self, meeting_id: str) -> None: ...
    def find_interrupted_meetings(self) -> List[Dict[str, Any]]: ...

    # -- audio chunks --
    def register_chunk(self, **fields) -> int: ...
    def set_chunk_status(self, chunk_id: int, status: str,
                         error: Optional[str] = None) -> None: ...
    def get_pending_chunks(self, meeting_id: str) -> List[Dict[str, Any]]: ...
    def count_unfinished_chunks(self, meeting_id: str) -> int: ...
    def reset_unfinished_chunks(self, meeting_id: str) -> int: ...
    def get_audio_chunks(self, meeting_id: str) -> List[Dict[str, Any]]: ...
    def next_chunk_seq(self, meeting_id: str, channel: str) -> int: ...

    # -- segments --
    def add_segments(self, segments: List[TranscriptSegment]) -> None: ...
    def commit_chunk_transcription(
        self, meeting_id: str, chunk_id: int,
        segments: List[TranscriptSegment],
    ) -> Any: ...
    def get_segment(self, meeting_id: str,
                    segment_id: str) -> Optional[Dict[str, Any]]: ...
    def segment_exists(self, meeting_id: str, segment_id: str) -> bool: ...
    def update_segment_speaker(
        self, meeting_id: str, segment_id: str,
        participant_id: Optional[str], source: str, pinned: bool,
    ) -> None: ...
    def update_segment_text(
        self, meeting_id: str, segment_id: str, text: str,
    ) -> Optional[Dict[str, Any]]: ...
    def get_segments(self, meeting_id: str, after_start_s: float = -1.0,
                     limit: Optional[int] = None) -> List[Dict[str, Any]]: ...
    def get_segments_in_range(
        self, meeting_id: str, channel: str,
        start_s: float, end_s: float,
    ) -> List[Dict[str, Any]]: ...
    def revise_segments_in_range(
        self, meeting_id: str, channel: str,
        start_s: float, end_s: float,
        segments: List[TranscriptSegment],
        remove_ids: List[str],
    ) -> Any: ...
    def get_segments_page(
        self, meeting_id: str, cursor_start_s: Optional[float] = None,
        cursor_id: Optional[str] = None, limit: int = 500,
    ) -> List[Dict[str, Any]]: ...
    def set_segment_embedding(self, meeting_id: str, segment_id: str,
                              embedding: bytes) -> None: ...

    # -- state write-through --
    def on_ops_applied(self, meeting_id: str, state: Dict[str, Any],
                       results: List[OpResult], actor_type: str,
                       actor_id: Optional[str]) -> None: ...
    def persist_state(self, meeting_id: str, state: Dict[str, Any]) -> None: ...
    def get_event(self, meeting_id: str, seq: int) -> Optional[Dict[str, Any]]: ...
    def event_is_undone(self, meeting_id: str, seq: int) -> bool: ...
    def list_events(self, meeting_id: str, before_seq: Optional[int] = None,
                    limit: int = 100) -> List[Dict[str, Any]]: ...

    # -- search --
    def search_transcripts(self, query: str) -> List[Dict[str, Any]]: ...
