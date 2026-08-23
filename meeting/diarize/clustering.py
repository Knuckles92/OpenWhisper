"""Online speaker clustering for the loopback channel.

Progressive, best-effort diarization: each transcript segment gets an
embedding, is assigned to the nearest speaker cluster by cosine similarity
(or seeds a new "Speaker N" cluster), and a periodic agglomerative re-cluster
corrects early mistakes, emitting relabel ops for segments whose assignment
changed. Human pins are authoritative: pinned segments are never relabeled
and dominate the cluster-to-participant mapping.

The re-cluster runs over a *bounded working set* — the most recent
``RECLUSTER_WINDOW`` embeddings, every pinned embedding, and one centroid
"anchor" per participant summarizing everything older. Average-linkage
clustering is O(N^3); an unbounded working set made a long meeting's
re-cluster cost more wall-clock than the segments it processed, starving the
ASR worker. Bounding the set makes each pass constant-cost regardless of
meeting length; the trade-off is that segments older than the window are no
longer revisited (they were already corrected by earlier passes) while their
identity still participates through the anchors.

Any internal failure degrades gracefully — ``assign`` starts returning None
and transcripts fall back to channel-level Me/Others labels.

Threading: ``assign`` is called from the single ASR worker thread (the
periodic re-cluster runs inline there); ``pin`` arrives from web-server
threads. Internal state is guarded by a lock, which is never held across
``store.apply`` or the relabel callback to avoid lock-order inversions with
the state store. Because that lock is released between computing relabel ops
and dispatching them, the ops are re-validated against the pin flags under
the lock immediately before dispatch — a pin landing in that window must win.
"""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

from meeting.diarize.embedder import EmbedderUnavailable, SpeakerEmbedder
from meeting.diarize.fbank import SAMPLE_RATE
from meeting.interfaces import TranscriptSegment

logger = logging.getLogger(__name__)

#: Cosine similarity at or above which a segment joins an existing cluster.
ASSIGN_THRESHOLD = 0.62
#: EMA weight of a new embedding when updating a cluster centroid.
EMA_ALPHA = 0.15
#: Maximum number of clusters; beyond this, segments join the nearest anyway.
MAX_CLUSTERS = 10
#: Re-cluster after this many new embeddings ...
RECLUSTER_EVERY_N = 25
#: ... or after this many seconds (whichever comes first, checked in assign).
RECLUSTER_EVERY_S = 60.0
#: Average-linkage merge stops once min inter-cluster cosine distance exceeds this.
MERGE_DISTANCE = 0.38
#: Pinned segments count this many times in cluster-to-participant mapping.
PIN_WEIGHT = 100
#: Max temporal gap for inheriting a label when a segment has no embedding.
INHERIT_WINDOW_S = 10.0
#: Most recent unpinned embeddings considered by one re-cluster pass.
RECLUSTER_WINDOW = 400
#: Hard cap on the re-cluster working set (pinned + recent + anchors), so a
#: pathological number of pins can never restore the O(N^3) blowup.
MAX_WORKING_SET = 600
#: Vote weight cap for a participant anchor; strictly below PIN_WEIGHT so an
#: anchor summarizing thousands of old segments never outranks a human pin.
ANCHOR_MAX_WEIGHT = PIN_WEIGHT - 1
#: Relabelable members an over-merged group needs before it earns a new
#: participant (keeps re-clustering noise from spawning phantom speakers).
MIN_SPLIT_SEGMENTS = 3


@dataclass
class _SegRecord:
    """One observed segment: its embedding (if any) and current label."""
    segment_id: str
    embedding: Optional[np.ndarray]
    participant_id: Optional[str]
    pinned: bool
    start_s: float
    end_s: float


@dataclass
class _Anchor:
    """A participant's out-of-window history, summarized as one vector.

    Anchors join the re-cluster distance matrix so historical identity keeps
    influencing cluster-to-participant mapping without paying for every old
    embedding. They are never relabeled and never emit ops.
    """
    participant_id: str
    centroid: np.ndarray
    weight: float


@dataclass
class _PlannedGroup:
    """One AHC group resolved to a target participant (phase 1 output).

    Attributes:
        participant_id: Existing participant the group maps to, or None while
            ``needs_new_participant`` is pending phase 2.
        segment_ids: Relabelable (non-pinned, non-anchor) member segment ids.
        needs_new_participant: True when this group lost the contest for its
            dominant participant id and must be split off as a new speaker.
    """
    participant_id: Optional[str]
    segment_ids: List[str]
    needs_new_participant: bool = False


@dataclass
class _Cluster:
    """One speaker cluster."""
    participant_id: str
    centroid: np.ndarray
    count: int = 0
    pinned_segments: Set[str] = field(default_factory=set)

    @property
    def pinned_dominated(self) -> bool:
        """True when pinned supervision outweighs organic membership.

        With PIN_WEIGHT=100 a single pin dominates until the cluster has
        more than 100 members; dominated clusters skip EMA centroid drift so
        human supervision stays anchored.
        """
        return len(self.pinned_segments) * PIN_WEIGHT >= self.count > 0


def _resample_to_16k(mono: np.ndarray, sample_rate: int) -> np.ndarray:
    """Resample mono float32 audio to 16 kHz.

    Uses ``fft_resample`` from ``services.streaming_transcriber`` when the
    host application is present; falls back to linear interpolation when the
    meeting package runs standalone. In practice the ASR engine already
    hands us 16 kHz audio, so this is a safety path.
    """
    if sample_rate == SAMPLE_RATE or mono.size == 0:
        return mono
    num = max(1, int(round(mono.size * SAMPLE_RATE / float(sample_rate))))
    try:
        from services.streaming_transcriber import fft_resample
    except ImportError:
        x_new = np.linspace(0.0, mono.size - 1, num)
        return np.interp(x_new, np.arange(mono.size), mono).astype(np.float32)
    return np.asarray(fft_resample(mono, num), dtype=np.float32)


def _to_mono_f32(audio: np.ndarray) -> np.ndarray:
    """Coerce audio to 1-D float32 in [-1, 1] (int16 input normalized)."""
    a = np.asarray(audio)
    if a.ndim > 1:
        a = a.reshape(a.shape[0], -1).mean(axis=1)
    if a.dtype == np.int16:
        a = a.astype(np.float32) / 32768.0
    return np.ascontiguousarray(a, dtype=np.float32).reshape(-1)


def _average_linkage(dist: np.ndarray, threshold: float) -> List[List[int]]:
    """Average-linkage agglomerative clustering over a distance matrix.

    Merges the closest pair of clusters (Lance-Williams average update)
    until the minimum inter-cluster distance exceeds ``threshold``.

    Dead rows and columns are masked to +inf in place so each iteration is a
    single full-matrix ``argmin`` with no per-merge submatrix copy. Callers
    must keep N bounded: the algorithm is inherently O(N^3) time / O(N^2)
    memory.

    Args:
        dist: Symmetric [N, N] pairwise distance matrix.
        threshold: Stop merging once the closest pair is farther than this.

    Returns:
        List of clusters, each a list of row indices into ``dist``.
    """
    n = dist.shape[0]
    if n == 0:
        return []
    if n == 1:
        return [[0]]

    d = dist.astype(np.float64).copy()
    np.fill_diagonal(d, np.inf)
    members: List[List[int]] = [[i] for i in range(n)]
    sizes = np.ones(n, dtype=np.float64)
    alive = np.ones(n, dtype=bool)
    alive_count = n

    while alive_count > 1:
        flat = int(np.argmin(d))
        a, b = divmod(flat, n)
        if d[a, b] > threshold:
            break

        # Lance-Williams average-linkage update: d(A∪B, C).
        merged = (sizes[a] * d[a] + sizes[b] * d[b]) / (sizes[a] + sizes[b])
        d[b, :] = np.inf
        d[:, b] = np.inf
        d[a, :] = merged
        d[:, a] = merged
        d[a, a] = np.inf
        d[a, b] = np.inf
        d[b, a] = np.inf

        sizes[a] += sizes[b]
        members[a].extend(members[b])
        alive[b] = False
        alive_count -= 1

    return [members[i] for i in np.where(alive)[0]]


def create_diarizer(model_path: Optional[str], store, repository,
                    meeting_id: str) -> Optional["OnlineDiarizer"]:
    """Build an ``OnlineDiarizer``, or None when diarization is unavailable.

    Args:
        model_path: Path to the speaker-embedding ONNX model, or None.
        store: ``MeetingStateStore`` used to create "Speaker N" participants.
        repository: ``MeetingRepository`` for persisting segment embeddings.
        meeting_id: The owning meeting session id.

    Returns:
        A ready diarizer, or None when the model is missing or fails to
        load (the meeting proceeds with channel-level Me/Others labels).
    """
    if not model_path:
        logger.info("Diarization disabled: no speaker embedding model configured")
        return None
    if not os.path.isfile(model_path):
        logger.warning("Diarization disabled: model not found at %s", model_path)
        return None
    try:
        embedder = SpeakerEmbedder(model_path)
    except EmbedderUnavailable:
        logger.warning("Diarization disabled: embedder failed to load",
                       exc_info=True)
        return None
    return OnlineDiarizer(embedder, store, repository, meeting_id)


class OnlineDiarizer:
    """Incremental cosine assignment plus periodic full re-clustering."""

    def __init__(self, embedder: SpeakerEmbedder, store, repository,
                 meeting_id: str) -> None:
        self._embedder = embedder
        self._store = store
        self._repository = repository
        self._meeting_id = meeting_id

        self._lock = threading.Lock()
        self._clusters: List[_Cluster] = []
        self._records: List[_SegRecord] = []
        self._by_segment_id: Dict[str, _SegRecord] = {}
        self._relabel_cb: Optional[Callable[[List[Dict[str, Any]]], None]] = None
        self._degraded = False
        # Separate from _lock: degradation is announced from paths that may or
        # may not hold _lock, and must be logged exactly once.
        self._degrade_lock = threading.Lock()
        self._speaker_counter = 0
        self._new_since_recluster = 0
        self._last_recluster_mono = time.monotonic()

    def assign(self, segment: TranscriptSegment, audio: np.ndarray,
               sample_rate: int) -> Optional[str]:
        """Assign a participant id to a transcript segment.

        Embeds the segment audio and matches it against cluster centroids;
        seeds a new "Speaker N" participant when nothing matches. Segments
        too short to embed inherit the temporally nearest label within
        ``INHERIT_WINDOW_S`` seconds. Triggers the periodic re-cluster.

        Called from the ASR worker thread. Never raises: any failure logs
        once and flips the diarizer into degraded (channel-level) mode.

        Args:
            segment: The transcript segment being labeled.
            audio: Mono audio for the segment (int16 or float32).
            sample_rate: Sample rate of ``audio`` in Hz.

        Returns:
            A participant id, or None to keep the channel-level label.
        """
        if self._degraded:
            return None
        try:
            return self._assign_inner(segment, audio, sample_rate)
        except Exception:
            self._degrade()
            return None

    def set_relabel_callback(
        self, cb: Callable[[List[Dict[str, Any]]], None]
    ) -> None:
        """Register the callback receiving re-cluster relabel op dicts.

        Args:
            cb: Called with a list of ``reassign_segment_speaker`` op dicts;
                the engine applies them through the store as ``system``.
        """
        with self._lock:
            self._relabel_cb = cb

    def pin(self, segment_id: str, participant_id: str) -> None:
        """Record a human speaker correction as clustering supervision.

        Marks the segment pinned (never relabeled again) and moves its
        embedding's weight to the target participant's cluster, creating an
        internal cluster for that participant if needed.

        Args:
            segment_id: The corrected segment.
            participant_id: The participant the human assigned.
        """
        try:
            with self._lock:
                record = self._by_segment_id.get(segment_id)
                if record is None:
                    # Segment we never embedded (mic channel, pre-diarizer, or
                    # too short). Track the pin so future re-clusters and
                    # temporal inheritance respect it; -inf times keep it out
                    # of the inheritance window.
                    record = _SegRecord(
                        segment_id=segment_id, embedding=None,
                        participant_id=participant_id, pinned=True,
                        start_s=float("-inf"), end_s=float("-inf"),
                    )
                    self._records.append(record)
                    self._by_segment_id[segment_id] = record

                old_pid = record.participant_id
                record.pinned = True
                record.participant_id = participant_id

                if record.embedding is not None and old_pid != participant_id:
                    old_cluster = self._find_cluster(old_pid)
                    if old_cluster is not None:
                        old_cluster.count = max(0, old_cluster.count - 1)
                        old_cluster.pinned_segments.discard(segment_id)
                        if (old_cluster.count == 0
                                and not old_cluster.pinned_segments):
                            self._clusters.remove(old_cluster)

                target = self._find_cluster(participant_id)
                if target is not None:
                    target.pinned_segments.add(segment_id)
                    if record.embedding is not None and old_pid != participant_id:
                        target.count += 1
                        self._ema_update(target, record.embedding)
                elif record.embedding is not None:
                    # Supervision may exceed MAX_CLUSTERS: a pinned cluster is
                    # ground truth, not an inference.
                    self._clusters.append(_Cluster(
                        participant_id=participant_id,
                        centroid=record.embedding.copy(),
                        count=1,
                        pinned_segments={segment_id},
                    ))
        except Exception:
            logger.exception("Diarizer pin failed for segment %s", segment_id)

    def is_available(self) -> bool:
        """Whether diarization is still producing speaker labels.

        Latches to False for the rest of the meeting once ``assign`` has hit
        a runtime failure, so callers polling this (the engine, the dashboard
        availability chip) see the degradation instead of advertising
        diarization that silently returns None forever.

        Returns:
            False when the diarizer degraded or the embedder is unusable.
        """
        if self._degraded:
            return False
        try:
            available = bool(self._embedder.available)
        except Exception:
            self._degrade("Diarizer embedder availability probe failed")
            return False
        if not available:
            self._degrade("Diarizer embedder became unavailable")
        return available

    def _degrade(self, message: str = "Diarizer failed") -> None:
        """Latch degraded mode, logging the cause exactly once.

        Args:
            message: Log message describing what failed; the active exception
                (when any) is attached as a traceback.
        """
        with self._degrade_lock:
            if self._degraded:
                return
            self._degraded = True
            exc_info = sys.exc_info()[0] is not None
            logger.log(
                logging.ERROR if exc_info else logging.WARNING,
                "%s (meeting %s); degrading to channel-level labels",
                message, self._meeting_id, exc_info=exc_info,
            )

    def _assign_inner(self, segment: TranscriptSegment, audio: np.ndarray,
                      sample_rate: int) -> Optional[str]:
        mono = _to_mono_f32(audio)
        mono = _resample_to_16k(mono, sample_rate)
        embedding = self._embedder.embed(mono)

        if embedding is None:
            with self._lock:
                pid = self._nearest_temporal_pid(segment)
                pid = self._track(segment, None, pid)
            return pid

        with self._lock:
            # A human may have pinned this segment already (correction racing
            # the ASR worker). The pin is authoritative: keep its participant,
            # just attach the embedding as supervision.
            pinned_record = self._by_segment_id.get(segment.segment_id)
            honor_pin = pinned_record is not None and pinned_record.pinned
            match = None if honor_pin else self._match_cluster(embedding)

        if honor_pin:
            with self._lock:
                pid = self._track(segment, embedding, None)
                do_recluster = self._should_recluster()
        elif match is None:
            pid = self._create_participant()  # store.apply outside the lock
            with self._lock:
                self._clusters.append(_Cluster(
                    participant_id=pid, centroid=embedding.copy(), count=1,
                ))
                pid = self._track(segment, embedding, pid)
                do_recluster = self._should_recluster()
        else:
            cluster, similarity = match
            with self._lock:
                if (similarity >= ASSIGN_THRESHOLD
                        and not cluster.pinned_dominated):
                    self._ema_update(cluster, embedding)
                cluster.count += 1
                pid = self._track(segment, embedding, cluster.participant_id)
                do_recluster = self._should_recluster()

        # The ASR chunk commit persists this in the same transaction as the
        # segment and chunk status. Writing it here would race a row that does
        # not exist until that commit.
        segment.embedding = embedding.astype(np.float32).tobytes()

        if do_recluster:
            relabel_ops = self._filter_stale_ops(self._recluster())
            if relabel_ops:
                cb = self._relabel_cb
                if cb is not None:
                    cb(relabel_ops)
        return pid

    def _filter_stale_ops(
        self, ops: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Drop relabel ops invalidated by a pin (or by a later assignment).

        The re-cluster releases the lock before the callback runs; a human
        correction landing in that window would otherwise be clobbered by an
        op computed before it existed. Re-checking under the lock immediately
        before dispatch closes that race — human pins are authoritative.

        Args:
            ops: Candidate ``reassign_segment_speaker`` op dicts.

        Returns:
            The ops still consistent with current in-memory records.
        """
        if not ops:
            return []
        with self._lock:
            fresh: List[Dict[str, Any]] = []
            for op in ops:
                record = self._by_segment_id.get(op.get("segment_id"))
                if record is None or record.pinned:
                    continue
                if record.participant_id != op.get("participant_id"):
                    continue
                fresh.append(op)
        return fresh

    def _track(self, segment: TranscriptSegment,
               embedding: Optional[np.ndarray],
               participant_id: Optional[str]) -> Optional[str]:
        """Record a segment observation (lock held).

        A pin recorded before the segment was embedded wins: the existing
        record keeps its participant id and pinned flag, and only gains the
        embedding plus real timestamps.

        Args:
            segment: The segment being observed.
            embedding: Its embedding, or None when it was too short.
            participant_id: The assignment the online pass arrived at.

        Returns:
            The participant id actually recorded (the pinned one, if any).
        """
        existing = self._by_segment_id.get(segment.segment_id)
        if existing is not None and existing.pinned:
            if embedding is not None:
                existing.embedding = embedding
                self._new_since_recluster += 1
            existing.start_s = segment.start_s
            existing.end_s = segment.end_s
            return existing.participant_id

        record = _SegRecord(
            segment_id=segment.segment_id, embedding=embedding,
            participant_id=participant_id, pinned=False,
            start_s=segment.start_s, end_s=segment.end_s,
        )
        self._records.append(record)
        self._by_segment_id[segment.segment_id] = record
        if embedding is not None:
            self._new_since_recluster += 1
        return participant_id

    def _match_cluster(self, embedding: np.ndarray):
        """Best cluster for an embedding, or None to seed a new one (lock held).

        Returns the best cluster below threshold anyway once MAX_CLUSTERS is
        reached (the periodic re-cluster corrects forced assignments).
        """
        if not self._clusters:
            return None
        sims = np.array(
            [float(np.dot(c.centroid, embedding)) for c in self._clusters])
        best = int(np.argmax(sims))
        if sims[best] >= ASSIGN_THRESHOLD or len(self._clusters) >= MAX_CLUSTERS:
            return self._clusters[best], float(sims[best])
        return None

    def _ema_update(self, cluster: _Cluster, embedding: np.ndarray) -> None:
        """EMA centroid update with renormalization (lock held)."""
        centroid = (1.0 - EMA_ALPHA) * cluster.centroid + EMA_ALPHA * embedding
        norm = float(np.linalg.norm(centroid))
        if norm > 1e-8:
            cluster.centroid = (centroid / norm).astype(np.float32)

    def _find_cluster(self, participant_id: Optional[str]) -> Optional[_Cluster]:
        for cluster in self._clusters:
            if cluster.participant_id == participant_id:
                return cluster
        return None

    def _nearest_temporal_pid(self, segment: TranscriptSegment) -> Optional[str]:
        """Label of the temporally nearest labeled segment (lock held)."""
        best_pid: Optional[str] = None
        best_gap = INHERIT_WINDOW_S
        for record in self._records:
            if record.participant_id is None:
                continue
            gap = max(0.0, record.start_s - segment.end_s,
                      segment.start_s - record.end_s)
            if gap <= best_gap:
                best_gap = gap
                best_pid = record.participant_id
        return best_pid

    def _create_participant(self) -> str:
        """Create a provisional "Speaker N" participant via the state store.

        Raises:
            RuntimeError: The store rejected the op (degrades the diarizer).
        """
        with self._lock:
            self._speaker_counter += 1
            number = self._speaker_counter
        results = self._store.apply("system", "diarizer", [{
            "op": "upsert_participant",
            "display_name": f"Speaker {number}",
            "kind": "others_cluster",
            "is_provisional": True,
        }])
        if not results or not results[0].ok:
            reason = results[0].reason if results else "no result"
            raise RuntimeError(f"Participant creation rejected: {reason}")
        effect = results[0].effect or {}
        pid = (effect.get("participant") or {}).get("id") or results[0].target_id
        if not pid:
            raise RuntimeError("Participant creation returned no id")
        return pid

    def _should_recluster(self) -> bool:
        """Whether the periodic re-cluster is due (lock held)."""
        if self._new_since_recluster <= 0:
            return False
        if self._new_since_recluster >= RECLUSTER_EVERY_N:
            return True
        return time.monotonic() - self._last_recluster_mono >= RECLUSTER_EVERY_S

    def _recluster(self) -> List[Dict[str, Any]]:
        """Periodic bounded re-cluster; returns relabel ops for changed segments.

        Runs in three phases so participant creation (which goes through
        ``store.apply``) never happens under the diarizer lock:

        1. Under the lock: build the bounded working set, cluster it, and
           resolve each group to a participant id — flagging groups that lost
           the contest for their dominant id as needing a split.
        2. Without the lock: create "Speaker N" participants for those groups.
        3. Under the lock: apply the mapping, re-validating pins (a pin may
           have landed during phase 2) and rebuilding centroids.

        Returns:
            ``reassign_segment_speaker`` op dicts for changed segments; the
            caller re-filters and forwards them outside the lock.
        """
        planned = self._plan_recluster()
        if not planned:
            return []
        planned = self._create_split_participants(planned)
        return self._apply_recluster(planned)

    def _plan_recluster(self) -> List[_PlannedGroup]:
        """Cluster the bounded working set and resolve groups to participants.

        Returns:
            One ``_PlannedGroup`` per group that has relabelable members;
            groups needing a new participant carry ``needs_new_participant``.
        """
        with self._lock:
            self._new_since_recluster = 0
            self._last_recluster_mono = time.monotonic()

            embedded = [r for r in self._records if r.embedding is not None]
            if len(embedded) < 2:
                return []

            records, anchors = self._working_set(embedded)
            if len(records) + len(anchors) < 2:
                return []

            vectors = [r.embedding for r in records]
            vectors.extend(a.centroid for a in anchors)
            matrix = np.stack(vectors)
            dist = np.clip(1.0 - matrix @ matrix.T, 0.0, 2.0)
            groups = _average_linkage(dist, MERGE_DISTANCE)

        return self._resolve_groups(groups, records, anchors)

    def _working_set(
        self, embedded: List[_SegRecord]
    ) -> Tuple[List[_SegRecord], List[_Anchor]]:
        """Pick the bounded set of vectors one re-cluster pass works on (lock held).

        Every pinned embedding participates (pins must always anchor
        identity), plus the most recent unpinned embeddings up to the
        remaining budget; everything older is summarized as one unit-norm
        centroid per participant.

        Args:
            embedded: All records that carry an embedding, in arrival order.

        Returns:
            ``(records, anchors)`` — relabelable records and read-only
            participant anchors for the out-of-window history.
        """
        pinned = [r for r in embedded if r.pinned]
        unpinned = [r for r in embedded if not r.pinned]
        budget = max(0, min(RECLUSTER_WINDOW, MAX_WORKING_SET - len(pinned)))
        recent = unpinned[len(unpinned) - budget:] if budget else []

        selected = pinned + recent
        selected_ids = {r.segment_id for r in selected}

        history: Dict[str, List[np.ndarray]] = {}
        for record in embedded:
            if record.segment_id in selected_ids or record.participant_id is None:
                continue
            history.setdefault(record.participant_id, []).append(record.embedding)

        anchors: List[_Anchor] = []
        for pid, vectors in history.items():
            centroid = np.stack(vectors).mean(axis=0)
            norm = float(np.linalg.norm(centroid))
            if norm <= 1e-8:
                continue
            anchors.append(_Anchor(
                participant_id=pid,
                centroid=(centroid / norm).astype(np.float32),
                weight=float(min(len(vectors), ANCHOR_MAX_WEIGHT)),
            ))
        return selected, anchors

    def _resolve_groups(self, groups: List[List[int]],
                        records: List[_SegRecord],
                        anchors: List[_Anchor]) -> List[_PlannedGroup]:
        """Map each AHC group to a participant id, splitting over-merges.

        A group's participant is the weighted-argmax of its members' current
        labels (pins count ``PIN_WEIGHT``, anchors their capped history size).
        When two groups claim the same participant the online pass over-merged
        two speakers: the weaker group is flagged for a new participant so
        re-clustering can split as well as join.

        Args:
            groups: Index groups returned by ``_average_linkage``.
            records: Working-set records (matrix rows ``0..len(records)-1``).
            anchors: Participant anchors (the remaining matrix rows).

        Returns:
            Planned groups with relabelable member ids.
        """
        n_records = len(records)
        planned: List[_PlannedGroup] = []
        claims: Dict[str, List[Tuple[float, _PlannedGroup]]] = {}

        for group in groups:
            weights: Dict[str, float] = {}
            members: List[_SegRecord] = []
            for i in group:
                if i < n_records:
                    record = records[i]
                    if not record.pinned:
                        members.append(record)
                    pid, weight = record.participant_id, (
                        PIN_WEIGHT if record.pinned else 1.0)
                else:
                    anchor = anchors[i - n_records]
                    pid, weight = anchor.participant_id, anchor.weight
                if pid is None:
                    continue
                weights[pid] = weights.get(pid, 0.0) + weight
            if not weights:
                continue
            dominant = max(weights.items(), key=lambda kv: (kv[1], kv[0]))[0]
            entry = _PlannedGroup(
                participant_id=dominant,
                segment_ids=[r.segment_id for r in members],
            )
            planned.append(entry)
            claims.setdefault(dominant, []).append((weights[dominant], entry))

        for pid, contenders in claims.items():
            if len(contenders) < 2:
                continue
            # Strongest claim keeps the participant; the rest are separate
            # speakers the online pass merged into it.
            contenders.sort(key=lambda c: (
                -c[0], -len(c[1].segment_ids),
                c[1].segment_ids[0] if c[1].segment_ids else ""))
            for _, entry in contenders[1:]:
                if len(entry.segment_ids) >= MIN_SPLIT_SEGMENTS:
                    entry.needs_new_participant = True
                    entry.participant_id = None
                    logger.info(
                        "Re-cluster splitting %d segments off participant %s"
                        " (meeting %s)", len(entry.segment_ids), pid,
                        self._meeting_id)
        return planned

    def _create_split_participants(
        self, planned: List[_PlannedGroup]
    ) -> List[_PlannedGroup]:
        """Create participants for split-off groups (lock NOT held).

        ``_create_participant`` calls ``store.apply``; holding the diarizer
        lock across it would invert lock order with the state store. A
        creation failure is not fatal — the group simply keeps its previous
        labels for this pass.

        Args:
            planned: Planned groups from ``_resolve_groups``.

        Returns:
            The same groups, with created participant ids filled in and any
            group whose creation failed dropped.
        """
        if not any(p.needs_new_participant for p in planned):
            return planned
        resolved: List[_PlannedGroup] = []
        for entry in planned:
            if not entry.needs_new_participant:
                resolved.append(entry)
                continue
            try:
                entry.participant_id = self._create_participant()
            except Exception:
                logger.exception(
                    "Re-cluster could not create a split participant"
                    " (meeting %s); leaving the group merged", self._meeting_id)
                continue
            resolved.append(entry)
        return resolved

    def _apply_recluster(
        self, planned: List[_PlannedGroup]
    ) -> List[Dict[str, Any]]:
        """Apply the resolved mapping and rebuild centroids (takes the lock).

        Pins are re-validated here because phase 2 ran without the lock: a
        segment pinned in the meantime keeps the human's participant.

        Args:
            planned: Planned groups with participant ids resolved.

        Returns:
            ``reassign_segment_speaker`` op dicts for segments that moved.
        """
        relabel_ops: List[Dict[str, Any]] = []
        with self._lock:
            for entry in planned:
                pid = entry.participant_id
                if pid is None:
                    continue
                for segment_id in entry.segment_ids:
                    record = self._by_segment_id.get(segment_id)
                    if record is None or record.pinned:
                        continue  # human corrections are never relabeled
                    if record.participant_id == pid:
                        continue
                    record.participant_id = pid
                    relabel_ops.append({
                        "op": "reassign_segment_speaker",
                        "segment_id": segment_id,
                        "participant_id": pid,
                    })
            self._rebuild_clusters(
                [r for r in self._records if r.embedding is not None])
        return relabel_ops

    def _rebuild_clusters(self, embedded: List[_SegRecord]) -> None:
        """Rebuild centroids from post-relabel assignments (lock held)."""
        by_pid: Dict[str, List[_SegRecord]] = {}
        for record in embedded:
            if record.participant_id is None:
                continue
            by_pid.setdefault(record.participant_id, []).append(record)

        clusters: List[_Cluster] = []
        for pid, records in by_pid.items():
            matrix = np.stack([r.embedding for r in records])
            centroid = matrix.mean(axis=0)
            norm = float(np.linalg.norm(centroid))
            if norm > 1e-8:
                centroid = centroid / norm
            clusters.append(_Cluster(
                participant_id=pid,
                centroid=centroid.astype(np.float32),
                count=len(records),
                pinned_segments={r.segment_id for r in records if r.pinned},
            ))
        self._clusters = clusters
