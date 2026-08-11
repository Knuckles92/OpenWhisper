"""Time-aware word-error metrics for long multiparty meetings."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

from benchmarks.meeting_mode.ami import ReferenceWord

_TOKEN_RE = re.compile(r"[\w]+(?:'[\w]+)?", re.UNICODE)


@dataclass(frozen=True)
class EditCounts:
    """Levenshtein operation counts against a reference token sequence."""

    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0

    @property
    def errors(self) -> int:
        """Return total edit operations."""
        return self.substitutions + self.deletions + self.insertions

    def __add__(self, other: "EditCounts") -> "EditCounts":
        return EditCounts(
            self.substitutions + other.substitutions,
            self.deletions + other.deletions,
            self.insertions + other.insertions,
        )


def normalize_tokens(text: str) -> list[str]:
    """Apply conservative case/punctuation normalization for ASR scoring."""
    normalized = unicodedata.normalize("NFKC", text or "").lower()
    normalized = normalized.replace("’", "'").replace("`", "'")
    # AMI writes spoken acronyms as ``A_S_R_`` and inflected forms such as
    # ``C_D_s``.  Whisper emits the ordinary written forms (``asr``, ``cds``),
    # so annotation separators are not lexical characters for scoring.
    normalized = normalized.replace("_", "")
    return _TOKEN_RE.findall(normalized)


def reference_overlap_stats(
    reference_words: Sequence[ReferenceWord],
) -> dict[str, Any]:
    """Count reference words that overlap speech from another speaker.

    A headset mix contains all participants in one mono stream. Concurrent
    words are therefore intrinsically ambiguous for a single-output ASR
    system, so this statistic distinguishes corpus difficulty from ordinary
    recognition errors without excluding anything from strict WER.

    Args:
        reference_words: Timed, speaker-attributed manual words.

    Returns:
        Counts and the fraction of reference words overlapping another speaker.
    """
    ordered = sorted(reference_words, key=lambda word: (word.start_s, word.end_s))
    overlapping: set[int] = set()
    for index, word in enumerate(ordered):
        if word.end_s <= word.start_s:
            continue
        for other_index in range(index + 1, len(ordered)):
            other = ordered[other_index]
            if other.start_s >= word.end_s:
                break
            if (
                other.speaker != word.speaker
                and other.end_s > other.start_s
                and other.end_s > word.start_s
            ):
                overlapping.add(index)
                overlapping.add(other_index)
    total = len(ordered)
    return {
        "reference_words": total,
        "overlapping_reference_words": len(overlapping),
        "overlap_rate": len(overlapping) / total if total else 0.0,
    }


def edit_counts(reference: Sequence[str], hypothesis: Sequence[str]) -> EditCounts:
    """Return exact word-level Levenshtein operation counts.

    The implementation stores only two dynamic-programming rows. A stable
    tie-break (substitution, deletion, insertion) makes diagnostic counts
    deterministic while leaving total WER unchanged.
    """
    # Cell tuple: total errors, substitutions, deletions, insertions.
    previous = [(index, 0, index, 0) for index in range(len(reference) + 1)]
    for hyp_index, hyp_word in enumerate(hypothesis, start=1):
        current = [(hyp_index, 0, 0, hyp_index)]
        for ref_index, ref_word in enumerate(reference, start=1):
            if ref_word == hyp_word:
                current.append(previous[ref_index - 1])
                continue
            diagonal = previous[ref_index - 1]
            delete = current[ref_index - 1]
            insert = previous[ref_index]
            candidates = (
                (diagonal[0] + 1, diagonal[1] + 1, diagonal[2], diagonal[3], 0),
                (delete[0] + 1, delete[1], delete[2] + 1, delete[3], 1),
                (insert[0] + 1, insert[1], insert[2], insert[3] + 1, 2),
            )
            best = min(candidates)
            current.append(best[:4])
        previous = current
    _, substitutions, deletions, insertions = previous[-1]
    return EditCounts(substitutions, deletions, insertions)


def _segment_value(segment: Any, name: str, default: Any = None) -> Any:
    if isinstance(segment, dict):
        return segment.get(name, default)
    return getattr(segment, name, default)


def score_timed_transcript(
    reference_words: Sequence[ReferenceWord],
    hypothesis_segments: Sequence[Any],
    window_s: float = 300.0,
) -> dict[str, Any]:
    """Score a long transcript in fixed meeting-time windows.

    Windowing prevents a duplicated or missing early passage from shifting the
    alignment for the rest of an hour-long meeting. It also bounds exact edit
    distance memory and runtime. Hypothesis segments are assigned by start
    time; reference tokens already have word timestamps.
    """
    if window_s <= 0:
        raise ValueError("window_s must be positive")
    evaluation_start_s = min((word.start_s for word in reference_words), default=0.0)
    evaluation_end_s = max((word.end_s for word in reference_words), default=0.0)
    window_count = max(1, int(evaluation_end_s // window_s) + 1)
    in_scope_segments = [
        segment
        for segment in hypothesis_segments
        if float(_segment_value(segment, "end_s", 0.0) or 0.0)
        > evaluation_start_s
        and float(_segment_value(segment, "start_s", 0.0) or 0.0)
        < evaluation_end_s
    ]
    ignored_hypothesis_words = sum(
        len(normalize_tokens(str(_segment_value(segment, "text", ""))))
        for segment in hypothesis_segments
        if segment not in in_scope_segments
    )
    totals = EditCounts()
    total_reference = 0
    total_hypothesis = 0
    windows: list[dict[str, Any]] = []

    for window_index in range(window_count):
        start_s = window_index * window_s
        end_s = start_s + window_s
        reference = [
            token
            for word in reference_words
            if start_s <= word.start_s < end_s
            for token in normalize_tokens(word.text)
        ]
        hypothesis = [
            token
            for segment in in_scope_segments
            if start_s
            <= max(
                evaluation_start_s,
                float(_segment_value(segment, "start_s", 0.0) or 0.0),
            )
            < end_s
            for token in normalize_tokens(str(_segment_value(segment, "text", "")))
        ]
        counts = edit_counts(reference, hypothesis)
        totals += counts
        total_reference += len(reference)
        total_hypothesis += len(hypothesis)
        windows.append(
            {
                "start_s": start_s,
                "end_s": end_s,
                "reference_words": len(reference),
                "hypothesis_words": len(hypothesis),
                **asdict(counts),
                "wer": counts.errors / len(reference) if reference else (
                    0.0 if not hypothesis else 1.0
                ),
            }
        )

    denominator = max(1, total_reference)
    return {
        "metric": f"tcWER-{int(window_s)}s",
        "evaluation_start_s": evaluation_start_s,
        "evaluation_end_s": evaluation_end_s,
        "out_of_reference_hypothesis_words": ignored_hypothesis_words,
        "reference_words": total_reference,
        "hypothesis_words": total_hypothesis,
        **asdict(totals),
        "wer": totals.errors / denominator,
        "substitution_rate": totals.substitutions / denominator,
        "deletion_rate": totals.deletions / denominator,
        "insertion_rate": totals.insertions / denominator,
        "windows": windows,
    }


def aggregate_scores(scores: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Micro-average meeting scores by summing edit counts and words."""
    items = list(scores)
    reference_words = sum(int(item["reference_words"]) for item in items)
    hypothesis_words = sum(int(item["hypothesis_words"]) for item in items)
    substitutions = sum(int(item["substitutions"]) for item in items)
    deletions = sum(int(item["deletions"]) for item in items)
    insertions = sum(int(item["insertions"]) for item in items)
    errors = substitutions + deletions + insertions
    denominator = max(1, reference_words)
    return {
        "meetings": len(items),
        "reference_words": reference_words,
        "hypothesis_words": hypothesis_words,
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "wer": errors / denominator,
        "substitution_rate": substitutions / denominator,
        "deletion_rate": deletions / denominator,
        "insertion_rate": insertions / denominator,
        "macro_wer": sum(float(item["wer"]) for item in items) / len(items)
        if items
        else 0.0,
    }
