"""Request and result types for transcribing several uploaded files as one job.

Kept free of Qt, settings, and the OpenAI client so the Upload File tab, the
settings resolvers, and the transcription runtime can all import it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final, Optional, Sequence, Tuple

from config import config


class BatchRelation:
    """How the files in one upload relate; persisted in settings."""

    SEPARATE: Final[str] = "separate"
    SEQUENTIAL: Final[str] = "sequential"
    CUSTOM: Final[str] = "custom"
    ALL: Final[Tuple[str, ...]] = (SEPARATE, SEQUENTIAL, CUSTOM)


@dataclass(frozen=True)
class BatchItem:
    audio_path: str
    duration_seconds: Optional[float] = None

    @property
    def source_name(self) -> str:
        return os.path.basename(self.audio_path)


@dataclass(frozen=True)
class BatchUploadRequest:
    items: Tuple[BatchItem, ...]
    relation: str = BatchRelation.SEPARATE
    custom_instructions: str = ""
    custom_combine: bool = False

    @property
    def combine(self) -> bool:
        """Whether the files become one transcript rather than one each."""
        if self.relation == BatchRelation.SEQUENTIAL:
            return True
        if self.relation == BatchRelation.CUSTOM:
            return self.custom_combine
        return False

    @property
    def source_names(self) -> Tuple[str, ...]:
        return tuple(item.source_name for item in self.items)

    def batch_context(self, item: Optional[BatchItem] = None) -> Optional[str]:
        """The user-facing description handed to the cleanup model.

        Returns None when cleanup should run exactly as it does for a single
        file: the Separate preset, or a Custom preset with nothing typed.
        """
        count = len(self.items)
        names = ", ".join(self.source_names)
        if self.relation == BatchRelation.SEQUENTIAL:
            return config.TRANSCRIPT_BATCH_PRESET_SEQUENTIAL.format(
                count=count, names=names
            )
        if self.relation != BatchRelation.CUSTOM:
            return None
        text = self.custom_instructions.strip()
        if not text:
            return None
        if self.combine:
            note = config.TRANSCRIPT_BATCH_CUSTOM_COMBINED_NOTE.format(
                count=count, names=names
            )
        else:
            name = item.source_name if item is not None else "one file"
            note = config.TRANSCRIPT_BATCH_CUSTOM_SEPARATE_NOTE.format(
                name=name, count=count
            )
        return f"{note}\n{text}"


@dataclass(frozen=True)
class BatchItemResult:
    item: BatchItem
    #: Cleaned text in per-file mode; raw ASR text in combined mode.
    text: str = ""
    #: The raw ASR text when cleanup changed it (per-file mode only).
    raw_text: Optional[str] = None
    cleanup_provider: Optional[str] = None
    cleanup_model: Optional[str] = None
    elapsed_s: float = 0.0
    file_size: Optional[int] = None
    error: Optional[str] = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class BatchResult:
    request: BatchUploadRequest
    #: Every item that was attempted, in request order.
    items: Tuple[BatchItemResult, ...]
    combined_text: Optional[str] = None
    combined_raw_text: Optional[str] = None
    combined_cleanup_provider: Optional[str] = None
    combined_cleanup_model: Optional[str] = None
    #: Set when the combined cleanup was attempted and fell back to raw text.
    cleanup_error: Optional[str] = None
    canceled: bool = False
    total_elapsed_s: float = 0.0

    @property
    def total_duration_seconds(self) -> float:
        return sum(r.item.duration_seconds or 0.0 for r in self.items)

    @property
    def total_file_size(self) -> int:
        return sum(r.file_size or 0 for r in self.items)


def compose_batch_cleanup_prompt(base_prompt: str, batch_context: str) -> str:
    """Append the user's description of the files to a finished cleanup prompt.

    Applied after the learned rules so they stay intact. The guard follows the
    description because the transcript is untrusted text while the description
    is the user's own.
    """
    return (
        f"{base_prompt}\n\n"
        f"{config.TRANSCRIPT_BATCH_CONTEXT_HEADER}\n{batch_context}\n\n"
        f"{config.TRANSCRIPT_BATCH_CONTEXT_GUARD}"
    )


def join_raw_parts(parts: Sequence[str]) -> str:
    """Join per-file ASR text for one combined cleanup.

    A blank line is the seam. Both backends collapse whitespace, so every part
    is a single line and the seam is unambiguous, and nothing leaks into the
    result if cleanup fails and the joined text is shown as-is.
    """
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def format_batch_transcript(sections: Sequence[Tuple[str, str]]) -> str:
    """Render per-file transcripts as markdown sections headed by file name."""
    return "\n\n".join(f"## {name}\n\n{body.strip()}" for name, body in sections)


def batch_source_name(
    names: Sequence[str],
    limit: int = config.TRANSCRIPT_BATCH_SOURCE_NAME_MAX_CHARS,
) -> str:
    """History label for a combined transcript, e.g. ``3 files: a.mp3, b.mp3``.

    Trailing names are dropped, not truncated mid-name, and replaced with a
    ``+N more`` count once the label would exceed ``limit``.
    """
    names = list(names)
    prefix = f"{len(names)} files: "
    shown: list[str] = []
    for index, name in enumerate(names):
        candidate = shown + [name]
        remaining = len(names) - len(candidate)
        suffix = f", +{remaining} more" if remaining else ""
        if shown and len(prefix + ", ".join(candidate) + suffix) > limit:
            break
        shown = candidate
    remaining = len(names) - len(shown)
    label = prefix + ", ".join(shown)
    if remaining:
        label += f", +{remaining} more"
    return label
