"""Turn a stopped walkthrough into its document; job worker thread.

Each section is transcribed on its own, through the same call a dictation
uses, so every engine works and no engine needs to report timestamps.
"""

from __future__ import annotations

import logging
import tempfile
import wave
from pathlib import Path
from typing import Callable, Optional, Sequence

from services.walkthrough.sections import split_recording
from services.walkthrough.session import Shot, WalkthroughResult, WalkthroughSession

logger = logging.getLogger(__name__)


def transcribe_sections(
    audio_path: str,
    shots: Sequence[Shot],
    transcribe: Callable[[str], str],
    *,
    before_each: Callable[[int, int], None] = lambda index, total: None,
) -> list[str]:
    """The words of each section, one more than there are screenshots.

    Args:
        audio_path: The whole recording.
        shots: The screenshots kept, in order.
        transcribe: Decodes one WAV path to text.
        before_each: Called with (index, total) before each section is
            decoded; it may raise to stop (a Cancel).
    """
    with tempfile.TemporaryDirectory(prefix="openwhisper-walkthrough-") as folder:
        try:
            clips = split_recording(Path(audio_path), [shot.kept_at_s for shot in shots], Path(folder))
        except (OSError, ValueError, wave.Error):
            # Every word above the first screenshot beats losing them.
            logger.warning("Could not cut the walkthrough audio; transcribing it whole", exc_info=True)
            before_each(1, 1)
            return [transcribe(audio_path)] + [""] * len(shots)
        texts = []
        for index, clip in enumerate(clips, start=1):
            before_each(index, len(clips))
            texts.append(transcribe(str(clip.path)) if clip.path is not None else "")
        return texts


def marked_text(texts: Sequence[str], shots: int) -> str:
    """The sections' words with ``[Screenshot N]`` where each screenshot sits."""
    parts = []
    for index, text in enumerate(texts):
        if (text or "").strip():
            parts.append(text.strip())
        if index < shots:
            parts.append(f"[Screenshot {index + 1}]")
    return "\n\n".join(parts)


def build_documents(
    session: WalkthroughSession,
    shots: Sequence[Shot],
    texts: Sequence[str],
    raw_texts: Sequence[str],
    *,
    duration_s: float,
) -> WalkthroughResult:
    """Write the walkthrough's Markdown, PDF and manifest into its folder."""
    from services.walkthrough.document import (
        Section,
        Walkthrough,
        markdown,
        write_manifest,
        write_markdown,
        write_pdf,
    )

    sections = [
        Section(text=texts[index], shot=shot.path, kept_at_s=shot.kept_at_s)
        for index, shot in enumerate(shots)
    ]
    sections.append(Section(text=texts[len(shots)]))
    walkthrough = Walkthrough(
        started_at=session.started_at, duration_s=duration_s, sections=tuple(sections),
    )
    folder = session.folder()
    markdown_path = write_markdown(walkthrough, folder)
    pdf = write_pdf(walkthrough, folder)
    write_manifest(walkthrough, folder)
    text = marked_text(texts, len(shots))
    raw = marked_text(raw_texts, len(shots))
    return WalkthroughResult(
        folder=folder,
        pdf=pdf,
        markdown=markdown_path,
        clipboard_text=markdown(walkthrough, folder=folder, absolute_images=True),
        history_text=text,
        history_raw=raw if raw != text else None,
        shots=len(shots),
        duration_s=duration_s,
        first_shot=shots[0].path if shots else None,
    )


def kept_note(session: Optional[WalkthroughSession]) -> str:
    """Where a failed walkthrough's screenshots are, for its error status, or ""."""
    if session is None or session.shot_count == 0:
        return ""
    return f" — screenshots kept in {session.folder()}"
