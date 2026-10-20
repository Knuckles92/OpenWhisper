"""Post-meeting OpenAI speaker identification over the loopback channel.

Uploads the system-audio recording to ``gpt-4o-transcribe-diarize``, maps
returned turns onto the existing local transcript by time overlap, and
emits ``reassign_segment_speaker`` ops. Local Whisper text is never replaced.

OpenAI retires that model on 2027-02-26 with no diarizing replacement; see
``services.openai_retirement``. After that the setting resolves to on-device
labels, and a model that disappears early is reported as ``retired``.

``transcribe_fn`` is injectable so tests never touch the network.
"""
from __future__ import annotations

import io
import logging
import re
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Sequence, Tuple

import numpy as np

from meeting.diarize.cloud_audio import (
    DEFAULT_BITRATE,
    DEFAULT_BYTE_BUDGET,
    DEFAULT_WINDOW_OVERLAP_S,
    cut_reference_clip,
    encode_mp3,
    plan_windows,
)
from meeting.interfaces import CHANNEL_LOOPBACK
from services import openai_retirement

logger = logging.getLogger(__name__)

#: Default Transcription API model. Rejects ``prompt``.
DEFAULT_MODEL = "gpt-4o-transcribe-diarize"

#: OpenAI accepts at most four named reference clips.
MAX_KNOWN_SPEAKERS = 4

#: Relabel ops applied in batches so one failure cannot drop the rest.
REASSIGN_BATCH = 40

#: Tie-break epsilon when two turns cover a segment equally.
_OVERLAP_TIE_EPS = 1e-6

ProgressCb = Callable[[str, int, int], None]
TranscribeFn = Callable[..., List[Dict[str, Any]]]


def overlap_duration(
    start_a: float, end_a: float, start_b: float, end_b: float,
) -> float:
    """Seconds of overlap between two half-open intervals."""
    return max(0.0, min(float(end_a), float(end_b)) - max(float(start_a), float(start_b)))


def map_turns_to_segments(
    segments: Sequence[Dict[str, Any]],
    turns: Sequence[Dict[str, Any]],
) -> Dict[str, str]:
    """Assign each unpinned segment to the turn with maximum time overlap.

    Segments with no overlapping turn are omitted (they keep their current
    label). Ties go to the earlier-starting turn.

    Args:
        segments: Transcript rows with ``id``/``start_s``/``end_s`` and
            optional ``speaker_pinned``.
        turns: ``{speaker, start, end}`` dicts in meeting-clock time.

    Returns:
        ``segment_id -> api_speaker``.
    """
    usable_turns = _usable_turns(turns)
    assigned: Dict[str, str] = {}
    for row in segments:
        if bool(row.get("speaker_pinned")):
            continue
        segment_id = str(row.get("id") or "")
        if not segment_id:
            continue
        try:
            start = float(row.get("start_s"))
            end = float(row.get("end_s"))
        except (TypeError, ValueError):
            continue
        best_speaker = _best_overlapping_speaker(start, end, usable_turns)
        if best_speaker is not None:
            assigned[segment_id] = best_speaker
    return assigned


def _usable_turns(turns: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Turns with a speaker and a positive numeric span."""
    usable_turns = []
    for turn in turns:
        speaker = str(turn.get("speaker") or "").strip()
        try:
            start = float(turn.get("start"))
            end = float(turn.get("end"))
        except (TypeError, ValueError):
            continue
        if not speaker or end <= start:
            continue
        usable_turns.append({"speaker": speaker, "start": start, "end": end})
    return usable_turns


def _best_overlapping_speaker(
    start: float, end: float, usable_turns: Sequence[Dict[str, Any]],
) -> Optional[str]:
    """Speaker of the turn overlapping ``[start, end)`` most; ties go to the earlier turn."""
    best_speaker = None
    best_overlap = 0.0
    best_turn_start = 0.0
    for turn in usable_turns:
        overlap = overlap_duration(start, end, turn["start"], turn["end"])
        if overlap <= 0.0:
            continue
        clearly_longer = overlap > best_overlap + _OVERLAP_TIE_EPS
        tied = abs(overlap - best_overlap) <= _OVERLAP_TIE_EPS
        if clearly_longer or (
            tied and (best_speaker is None or turn["start"] < best_turn_start)
        ):
            best_overlap = overlap
            best_speaker = turn["speaker"]
            best_turn_start = turn["start"]
    return best_speaker


def resolve_speaker_participants(
    speaker_to_segment_ids: Dict[str, List[str]],
    segments_by_id: Dict[str, Dict[str, Any]],
    named_map: Dict[str, str],
) -> Dict[str, Optional[str]]:
    """Map each API speaker onto an existing participant id, or None to create.

    Named reference clips win. Remaining speakers take the majority current
    local label of the segments they cover. Two API speakers that vote for
    the same participant: the one covering more segments keeps it; the
    other is treated as a new voice.

    Args:
        speaker_to_segment_ids: API speaker → assigned segment ids.
        segments_by_id: Transcript rows keyed by id.
        named_map: API speaker label → existing participant id.

    Returns:
        API speaker → participant id, or ``None`` when a new participant
        should be created.
    """
    mapping: Dict[str, Optional[str]] = {}
    claimed = set()
    for speaker, participant_id in named_map.items():
        if speaker in speaker_to_segment_ids or speaker in named_map:
            mapping[speaker] = participant_id
            claimed.add(participant_id)

    pending: List[Tuple[str, str, int, int]] = []
    for speaker, segment_ids in speaker_to_segment_ids.items():
        if speaker in mapping:
            continue
        votes: Dict[str, int] = {}
        for segment_id in segment_ids:
            row = segments_by_id.get(segment_id) or {}
            participant_id = row.get("speaker_participant_id")
            if participant_id:
                key = str(participant_id)
                votes[key] = votes.get(key, 0) + 1
        if not votes:
            mapping[speaker] = None
            continue
        winner, count = max(votes.items(), key=lambda item: (item[1], item[0]))
        pending.append((speaker, winner, count, len(segment_ids)))

    pending.sort(key=lambda item: (-item[2], -item[3], item[0]))
    for speaker, winner, _count, _n in pending:
        if winner in claimed:
            mapping[speaker] = None
        else:
            mapping[speaker] = winner
            claimed.add(winner)
    return mapping


def speaker_slug(name: str) -> str:
    """Short identifier safe to send as ``known_speaker_names``."""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", (name or "").strip())
    cleaned = cleaned.strip("_") or "speaker"
    return cleaned[:32]


def pick_reference_segments(
    participants: Iterable[Dict[str, Any]],
    segments: Sequence[Dict[str, Any]],
    *,
    limit: int = MAX_KNOWN_SPEAKERS,
) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Human-named others-cluster participants paired with their best clip.

    Prefers the longest unpinned loopback segment attributed to the
    participant. Falls back to a pinned segment on that participant when
    nothing else is long enough.
    """
    by_participant = _loopback_rows_by_participant(segments)
    picked: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for participant in participants:
        if len(picked) >= int(limit):
            break
        if not _is_human_named_cluster(participant):
            continue
        participant_id = str(participant.get("id") or "")
        if not participant_id:
            continue
        best = _longest_reference_row(list(by_participant.get(participant_id) or []))
        if best is None:
            continue
        picked.append((participant, best))
    return picked


def _loopback_rows_by_participant(
    segments: Sequence[Dict[str, Any]],
) -> Dict[str, List[Dict[str, Any]]]:
    by_participant: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in segments:
        if str(row.get("channel") or "") != CHANNEL_LOOPBACK:
            continue
        participant_id = row.get("speaker_participant_id")
        if not participant_id:
            continue
        by_participant[str(participant_id)].append(row)
    return by_participant


def _is_human_named_cluster(participant: Dict[str, Any]) -> bool:
    if str(participant.get("kind") or "") != "others_cluster":
        return False
    return str(participant.get("name_source") or "") == "human"


def _row_duration(row: Dict[str, Any]) -> float:
    """Segment length in seconds; 0.0 when its times are not numeric."""
    try:
        return float(row.get("end_s") or 0.0) - float(row.get("start_s") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _longest_reference_row(candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Longest unpinned row (any row when all are pinned), if it has positive length."""
    if not candidates:
        return None
    unpinned = [row for row in candidates if not row.get("speaker_pinned")]
    pool = unpinned or candidates
    best = max(pool, key=_row_duration)
    if _row_duration(best) <= 0.0:
        return None
    return best


def parse_diarized_result(result: Any) -> List[Dict[str, Any]]:
    """Normalize an API / fake response into ``{speaker, start, end}`` turns."""
    if result is None:
        return []
    data = result
    if hasattr(result, "model_dump"):
        try:
            data = result.model_dump()
        except Exception:
            data = result
    if not isinstance(data, dict):
        segments = getattr(result, "segments", None)
        data = {"segments": list(segments or [])}
    turns: List[Dict[str, Any]] = []
    for item in data.get("segments") or []:
        if isinstance(item, dict):
            speaker = item.get("speaker")
            start = item.get("start")
            end = item.get("end")
        else:
            speaker = getattr(item, "speaker", None)
            start = getattr(item, "start", None)
            end = getattr(item, "end", None)
        if speaker is None or start is None or end is None:
            continue
        try:
            turns.append({
                "speaker": str(speaker),
                "start": float(start),
                "end": float(end),
            })
        except (TypeError, ValueError):
            continue
    return turns


def default_transcribe(
    mp3_bytes: bytes,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    known_speaker_names: Optional[Sequence[str]] = None,
    known_speaker_references: Optional[Sequence[str]] = None,
) -> List[Dict[str, Any]]:
    """Call OpenAI ``/v1/audio/transcriptions`` and return speaker turns."""
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    kwargs: Dict[str, Any] = {
        "model": model,
        "file": ("loopback.mp3", io.BytesIO(mp3_bytes), "audio/mpeg"),
        "response_format": "diarized_json",
        "chunking_strategy": "auto",
    }
    names = [str(name) for name in (known_speaker_names or []) if str(name)]
    refs = [str(ref) for ref in (known_speaker_references or []) if str(ref)]
    if names and refs and len(names) == len(refs):
        kwargs["known_speaker_names"] = names
        kwargs["known_speaker_references"] = refs
    result = client.audio.transcriptions.create(**kwargs)
    return parse_diarized_result(result)


def _unique_slugs(names: Sequence[str]) -> List[str]:
    """Stable unique slugs for a list of display names."""
    seen: Dict[str, int] = {}
    slugs: List[str] = []
    for name in names:
        base = speaker_slug(name)
        count = seen.get(base, 0)
        seen[base] = count + 1
        slugs.append(base if count == 0 else f"{base}_{count + 1}"[:32])
    return slugs


def _create_participant(store: Any, display_name: str) -> Optional[str]:
    results = store.apply("system", "diarizer", [{
        "op": "upsert_participant",
        "display_name": display_name,
        "kind": "others_cluster",
        "is_provisional": True,
    }])
    if not results or not results[0].ok or not results[0].effect:
        logger.warning("Cloud speaker pass could not create %s", display_name)
        return None
    participant = results[0].effect.get("participant") or {}
    return participant.get("id")


def _apply_reassigns(
    store: Any,
    assignments: Sequence[Tuple[str, str]],
) -> int:
    applied = 0
    batch: List[Dict[str, Any]] = []
    for segment_id, participant_id in assignments:
        batch.append({
            "op": "reassign_segment_speaker",
            "segment_id": segment_id,
            "participant_id": participant_id,
        })
        if len(batch) >= REASSIGN_BATCH:
            results = store.apply("system", "diarizer", batch)
            applied += sum(1 for result in results if result.ok)
            batch = []
    if batch:
        results = store.apply("system", "diarizer", batch)
        applied += sum(1 for result in results if result.ok)
    return applied


def _anchor_clips_from_window(
    frames: np.ndarray,
    sample_rate: int,
    origin_s: float,
    speaker_to_segment_ids: Dict[str, List[str]],
    segments_by_id: Dict[str, Dict[str, Any]],
    speaker_to_pid: Dict[str, Optional[str]],
    participants_by_id: Dict[str, Dict[str, Any]],
    *,
    limit: int = MAX_KNOWN_SPEAKERS,
) -> List[Tuple[str, str, str]]:
    """Build (slug, data_url, participant_id) anchors for later windows."""
    scored: List[Tuple[float, str]] = []
    for speaker, segment_ids in speaker_to_segment_ids.items():
        duration = 0.0
        for segment_id in segment_ids:
            duration += _row_duration(segments_by_id.get(segment_id) or {})
        scored.append((duration, speaker))
    scored.sort(reverse=True)
    anchors: List[Tuple[str, str, str]] = []
    for _duration, speaker in scored:
        if len(anchors) >= limit:
            break
        participant_id = speaker_to_pid.get(speaker)
        if not participant_id:
            continue
        best = _longest_row(speaker_to_segment_ids.get(speaker) or [], segments_by_id)
        if best is None:
            continue
        clip = cut_reference_clip(
            frames, sample_rate,
            float(best.get("start_s") or 0.0),
            float(best.get("end_s") or 0.0),
            origin_s=origin_s,
        )
        if not clip:
            continue
        participant = participants_by_id.get(participant_id) or {}
        label = speaker_slug(
            str(participant.get("display_name") or speaker)
        )
        anchors.append((label, clip, participant_id))
    return anchors


def _longest_row(
    segment_ids: Sequence[str], segments_by_id: Dict[str, Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """First row of maximal positive duration, or None."""
    best = None
    best_dur = 0.0
    for segment_id in segment_ids:
        row = segments_by_id.get(segment_id) or {}
        dur = _row_duration(row)
        if dur > best_dur:
            best_dur = dur
            best = row
    return best


def _turns_on_meeting_clock(
    raw_turns: Sequence[Any], window_origin: float,
) -> List[Dict[str, Any]]:
    """Shift window-relative turns onto the meeting clock.

    Injectable ``transcribe_fn`` results (and the OpenAI API) use times
    relative to the uploaded audio. If a caller already returned
    meeting-clock times (``start >= window_origin``), they are left as-is.
    """
    parsed = raw_turns
    if raw_turns and not isinstance(raw_turns[0], dict):
        parsed = parse_diarized_result({"segments": list(raw_turns)})
    turns: List[Dict[str, Any]] = []
    origin = float(window_origin)
    for turn in parsed:
        if not isinstance(turn, dict):
            continue
        speaker = str(turn.get("speaker") or "").strip()
        try:
            start = float(turn["start"])
            end = float(turn["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < origin - 1e-3:
            start += origin
            end += origin
        turns.append({"speaker": speaker, "start": start, "end": end})
    return turns


def run_cloud_speaker_pass(
    repository: Any,
    meeting_id: str,
    store: Any,
    spool_dir: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    transcribe_fn: Optional[TranscribeFn] = None,
    progress_cb: Optional[ProgressCb] = None,
    bitrate: int = DEFAULT_BITRATE,
    byte_budget: int = DEFAULT_BYTE_BUDGET,
) -> Dict[str, Any]:
    """Relabel loopback speakers from one OpenAI diarize pass.

    Args:
        repository: A ``MeetingRepository``.
        meeting_id: Meeting whose loopback channel is labeled.
        store: A ``MeetingStateStore`` with a segment handler.
        spool_dir: Directory holding session WAVs / chunks.
        api_key: OpenAI API key (ignored when ``transcribe_fn`` is set).
        model: Transcription model id.
        transcribe_fn: Injectable ``(mp3_bytes, **kwargs) -> turns``.
        progress_cb: Optional ``cb(detail, current, total)``.
        bitrate: MP3 bitrate for the upload.
        byte_budget: Encoded-size cap that triggers windowing.

    Returns:
        ``{ok, applied, created, windows, error}``. Failures are reported
        here, not raised.
    """
    from meeting.asr.offline import load_channel_session

    chunks = _audio_chunks(repository, meeting_id)
    frames, rate, origin_s = load_channel_session(spool_dir, CHANNEL_LOOPBACK, chunks)
    if frames is None or getattr(frames, "size", 0) == 0:
        return _failed("No system-audio recording is available.")

    try:
        segments = _loopback_segments(repository, meeting_id)
    except Exception as exc:
        logger.exception("Could not load segments for cloud speaker pass")
        return _failed(str(exc))
    if not segments:
        return _failed("No system-audio transcript segments to label.")

    speaker_pass = _LoopbackSpeakerPass(
        store, segments, frames, rate, origin_s, bitrate=bitrate, model=model,
    )
    known = speaker_pass.named_references()

    windows = plan_windows(
        int(frames.size), int(rate),
        byte_budget=byte_budget, bitrate=bitrate,
        overlap_s=DEFAULT_WINDOW_OVERLAP_S,
    )
    if not windows:
        return _failed("System-audio recording is empty.")

    decoder = transcribe_fn or (
        lambda mp3_bytes, **kwargs: default_transcribe(
            mp3_bytes, api_key=api_key, model=model, **kwargs,
        )
    )

    references = _References(list(known.names), list(known.clips), dict(known.named_map))
    for index, (start, end) in enumerate(windows, 1):
        _report_window_progress(progress_cb, index, len(windows))
        raw_turns, failure = speaker_pass.decode_window(
            decoder, index, start, end, len(windows), references,
        )
        if failure is not None:
            return failure
        speaker_pass.add_window_turns(raw_turns, start)
        if index < len(windows):
            references = speaker_pass.next_references(known, references.named_map)

    speaker_to_pid = speaker_pass.final_speaker_map(references.named_map)
    created = _create_missing_participants(store, speaker_to_pid)
    reassigns = speaker_pass.reassignments(speaker_to_pid)

    try:
        applied = _apply_reassigns(store, reassigns)
    except Exception as exc:
        logger.exception("Cloud speaker reassignment failed")
        return _failed(str(exc), windows=len(windows), created=created)
    return {
        "ok": True,
        "applied": applied,
        "created": created,
        "windows": len(windows),
        "error": None,
    }


def _failed(error: str, *, windows: int = 0, created: int = 0) -> Dict[str, Any]:
    return {
        "ok": False, "applied": 0, "created": created,
        "windows": windows, "error": error,
    }


def _audio_chunks(repository: Any, meeting_id: str) -> List[Dict[str, Any]]:
    try:
        return repository.get_audio_chunks(meeting_id)
    except Exception:
        logger.exception("Could not load audio chunks for cloud speaker pass")
        return []


def _loopback_segments(repository: Any, meeting_id: str) -> List[Dict[str, Any]]:
    return [
        row for row in repository.get_segments(meeting_id)
        if str(row.get("channel") or "") == CHANNEL_LOOPBACK
    ]


def _report_window_progress(progress_cb: Optional[ProgressCb], index: int, total: int) -> None:
    if progress_cb is None:
        return
    try:
        progress_cb(
            f"Identifying speakers in window {index}/{total}",
            index, total,
        )
    except Exception:
        logger.exception("Cloud speaker progress callback failed")


class _References(NamedTuple):
    """Known-speaker names and clips sent with a window, and the participant each name is."""

    names: List[str]
    clips: List[str]
    named_map: Dict[str, str]


def _merge_anchor_references(
    known: _References, anchors: Sequence[Tuple[str, str, str]],
) -> _References:
    """Human-named clips stay first; fill remaining slots with anchors."""
    merged_names = list(known.names)
    merged_clips = list(known.clips)
    merged_map = dict(known.named_map)
    for slug, clip, participant_id in anchors:
        if participant_id in merged_map.values():
            continue
        if len(merged_names) >= MAX_KNOWN_SPEAKERS:
            break
        if slug in merged_map:
            slug = f"{slug}_{len(merged_names) + 1}"[:32]
        merged_names.append(slug)
        merged_clips.append(clip)
        merged_map[slug] = participant_id
    return _References(merged_names, merged_clips, merged_map)


def _create_missing_participants(store: Any, speaker_to_pid: Dict[str, Optional[str]]) -> int:
    """Create "Speaker N" participants for unmapped speakers, in place; returns how many."""
    created = 0
    existing_names = store.with_state(
        lambda state: {p.display_name for p in state.participants.values()}
    )
    next_number = 1
    while f"Speaker {next_number}" in existing_names:
        next_number += 1
    for speaker, participant_id in list(speaker_to_pid.items()):
        if participant_id:
            continue
        created_id = _create_participant(store, f"Speaker {next_number}")
        if created_id is None:
            continue
        speaker_to_pid[speaker] = created_id
        created += 1
        existing_names.add(f"Speaker {next_number}")
        next_number += 1
    return created


class _LoopbackSpeakerPass:
    """Loopback audio, transcript rows and the turn assignments gathered so far."""

    def __init__(
        self,
        store: Any,
        segments: List[Dict[str, Any]],
        frames: np.ndarray,
        rate: int,
        origin_s: float,
        *,
        bitrate: int,
        model: str,
    ) -> None:
        self.store = store
        self.segments = segments
        self.frames = frames
        self.rate = rate
        self.origin_s = origin_s
        self.bitrate = bitrate
        self.model = model
        self.segments_by_id = {str(row["id"]): row for row in segments if row.get("id")}
        self.all_assignments: Dict[str, str] = {}
        self.speaker_segments: Dict[str, List[str]] = defaultdict(list)

    def named_references(self) -> _References:
        """Reference clips for human-named participants, at most one per slug."""
        participants = self.store.with_state(
            lambda state: [p.to_dict() for p in state.participants.values()]
        )
        named_pairs = pick_reference_segments(participants, self.segments)
        named_slugs = _unique_slugs([
            str(participant.get("display_name") or "")
            for participant, _row in named_pairs
        ])
        named_refs: List[str] = []
        named_map: Dict[str, str] = {}
        for slug, (participant, row) in zip(named_slugs, named_pairs):
            clip = cut_reference_clip(
                self.frames, self.rate,
                float(row.get("start_s") or 0.0),
                float(row.get("end_s") or 0.0),
                origin_s=self.origin_s,
                bitrate=self.bitrate,
            )
            if not clip:
                continue
            named_refs.append(clip)
            named_map[slug] = str(participant["id"])
        known_names = [slug for slug in named_map]
        known_clips = named_refs[:len(known_names)]
        return _References(known_names, known_clips, named_map)

    def decode_window(
        self,
        decoder: TranscribeFn,
        index: int,
        start: int,
        end: int,
        total: int,
        references: _References,
    ) -> Tuple[Any, Optional[Dict[str, Any]]]:
        """Encode and diarize one window: ``(raw_turns, None)`` or ``(None, failure)``."""
        window = self.frames[start:end]
        try:
            mp3 = encode_mp3(window, self.rate, self.bitrate)
        except Exception as exc:
            logger.exception("Failed to encode loopback window %s", index)
            return None, _failed(str(exc), windows=total)
        try:
            raw_turns = decoder(
                mp3,
                known_speaker_names=references.names or None,
                known_speaker_references=references.clips or None,
            )
        except Exception as exc:
            if openai_retirement.is_model_gone_error(exc):
                logger.warning("OpenAI no longer serves %s", self.model)
                return None, {
                    "ok": False, "applied": 0, "created": 0,
                    "windows": total, "retired": True,
                    "error": openai_retirement.SPEAKER_MODEL_RETIRED_MESSAGE,
                }
            logger.exception("Cloud diarize request failed for window %s", index)
            return None, _failed(str(exc), windows=total)
        return raw_turns, None

    def add_window_turns(self, raw_turns: Sequence[Any], start: int) -> None:
        window_origin = float(self.origin_s) + start / float(self.rate)
        turns = _turns_on_meeting_clock(raw_turns, window_origin)
        mapped = map_turns_to_segments(self.segments, turns)
        for segment_id, speaker in mapped.items():
            self.all_assignments[segment_id] = speaker
            self.speaker_segments[speaker].append(segment_id)

    def next_references(self, known: _References, window_named_map: Dict[str, str]) -> _References:
        """References for the next window: named clips plus anchors of voices heard so far."""
        speaker_to_pid = resolve_speaker_participants(
            {key: list(ids) for key, ids in self.speaker_segments.items()},
            self.segments_by_id, window_named_map,
        )
        participants_by_id = {
            str(p.get("id")): p for p in self.store.with_state(
                lambda state: [part.to_dict() for part in state.participants.values()]
            )
        }
        anchors = _anchor_clips_from_window(
            self.frames, self.rate, self.origin_s, dict(self.speaker_segments),
            self.segments_by_id, speaker_to_pid, participants_by_id,
            limit=MAX_KNOWN_SPEAKERS,
        )
        return _merge_anchor_references(known, anchors)

    def final_speaker_map(self, named_map: Dict[str, str]) -> Dict[str, Optional[str]]:
        return resolve_speaker_participants(
            {key: list(dict.fromkeys(ids)) for key, ids in self.speaker_segments.items()},
            self.segments_by_id, named_map,
        )

    def reassignments(self, speaker_to_pid: Dict[str, Optional[str]]) -> List[Tuple[str, str]]:
        """Relabels for unpinned rows whose participant actually changes."""
        reassigns: List[Tuple[str, str]] = []
        for segment_id, speaker in self.all_assignments.items():
            participant_id = speaker_to_pid.get(speaker)
            if not participant_id:
                continue
            row = self.segments_by_id.get(segment_id) or {}
            if bool(row.get("speaker_pinned")):
                continue
            if row.get("speaker_participant_id") == participant_id:
                continue
            reassigns.append((segment_id, participant_id))
        return reassigns
