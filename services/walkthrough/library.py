"""Persistent walkthrough documents, review, recovery and opt-in retention.

Only local, regular files directly inside a capture are accepted. Review removes
images from the document, not the original PNGs, so a correction is reversible.
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import tempfile
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from services.walkthrough.session import Shot, WalkthroughResult, WalkthroughSession
from services.walkthrough.storage import MANIFEST_NAME, MARKDOWN_NAME, PDF_NAME

if TYPE_CHECKING:
    from services.walkthrough.document import Walkthrough

DRAFT_NAME = "draft.json"
AUDIO_NAME = "audio.wav"
PREFERENCES_NAME = "retention.json"
_FOLDER = re.compile(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}(?:-\d+)?\Z")
_SHOT = re.compile(r"shot-[1-9]\d*\.png\Z")
_FIXED = {DRAFT_NAME, AUDIO_NAME, "walkthrough.json", "walkthrough.md", "walkthrough.pdf",
          "write-up.md", "write-up.pdf", "walkthrough.original.json"}


def atomic_json(path: Path, value) -> None:
    """Replace a checkpoint only after its complete contents are durable."""
    fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def publish_files(stage: Path, folder: Path, names: tuple[str, ...],
                  *, remove: tuple[str, ...] = ()) -> None:
    """Publish rendered artifacts together, rolling back a failed file replacement."""
    previous = stage / ".previous"
    previous.mkdir()
    existed = set()
    for name in (*names, *remove):
        target = folder / name
        if target.exists() or target.is_symlink():
            source = local_file(folder, name)
            shutil.copyfile(source, previous / name)
            existed.add(name)
    published = []
    removed = []
    try:
        for name in remove:
            if name in existed:
                (folder / name).unlink()
                removed.append(name)
        for name in names:
            os.replace(stage / name, folder / name)
            published.append(name)
    except OSError:
        for name in reversed(published + removed):
            if name in existed:
                os.replace(previous / name, folder / name)
            else:
                (folder / name).unlink(missing_ok=True)
        raise


def local_file(folder: Path, name: str) -> Path:
    if not isinstance(name, str) or Path(name).name != name or "/" in name or "\\" in name:
        raise ValueError("Invalid walkthrough filename")
    path = folder / name
    if folder.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing or unsafe walkthrough file: {name}")
    return path


def read_json(path: Path):
    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Unsafe or oversized walkthrough metadata")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Walkthrough metadata must be an object")
    return value


def _seconds(value) -> float:
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Invalid walkthrough time")
    return value


def load_document(folder: Path) -> Walkthrough:
    # The Qt renderer loads only here and below: recovery and retention run
    # on the startup worker, which must not import it.
    from services.walkthrough import document

    value = read_json(folder / MANIFEST_NAME)
    if value.get("version") != document.MANIFEST_VERSION:
        raise ValueError("Unsupported walkthrough version")
    sections = []
    for section in value["sections"]:
        name = section.get("shot")
        if name is not None and not _SHOT.fullmatch(name):
            raise ValueError("Invalid screenshot name")
        shot = local_file(folder, name) if name else None
        stamp = section.get("kept_at_s")
        sections.append(document.Section(str(section["text"]), shot,
                                         _seconds(stamp) if stamp is not None else None))
    return document.Walkthrough(datetime.fromisoformat(value["started_at"]),
                                _seconds(value["duration_s"]), tuple(sections))


def load_result(folder: Path, *, spoken: bool = False) -> WalkthroughResult:
    from services.walkthrough import document
    from services.walkthrough.write_up import _IMAGE

    walkthrough = load_document(folder)
    shots = [section.shot for section in walkthrough.sections if section.shot]
    written = not spoken and (folder / "write-up.pdf").is_file() and (folder / "write-up.md").is_file()
    pdf = local_file(folder, "write-up.pdf" if written else PDF_NAME)
    md = local_file(folder, "write-up.md" if written else MARKDOWN_NAME)
    # Normalize image paths from disk; never reuse another computer's paths.
    names = {shot.name for shot in shots}
    text = md.read_text(encoding="utf-8")
    def absolute(match):
        name = match[2].strip("<>")
        if name not in names:
            return ""
        return f"![{match[1]}]({document._image_target(folder / name, folder, True)})"
    text = _IMAGE.sub(absolute, text)
    parts = []
    number = 0
    for section in walkthrough.sections:
        if section.text.strip():
            parts.append(section.text.strip())
        if section.shot:
            number += 1
            parts.append(f"[Screenshot {number}]")
    history = "\n\n".join(parts)
    return WalkthroughResult(folder, pdf, md, text, history, None, len(shots),
                             walkthrough.duration_s, shots[0] if shots else None, written)


def entries(root: Path) -> list[Path]:
    if not root.exists() or root.is_symlink():
        return []
    return sorted((p for p in root.iterdir() if p.is_dir() and not p.is_symlink()
                   and _FOLDER.fullmatch(p.name)
                   and ((p / MANIFEST_NAME).is_file() or (p / DRAFT_NAME).is_file())),
                  reverse=True)


def save_review(folder: Path, texts: list[str], keep: list[bool]) -> WalkthroughResult:
    """Regenerate all three documents; keep omitted images and the original manifest."""
    from services.walkthrough import document
    from services.walkthrough.finish import document_settings

    original = load_document(folder)
    if len(texts) != len(original.sections) or len(keep) != len(texts):
        raise ValueError("The document changed; reopen Review")
    updated = replace(original, sections=tuple(
        replace(section, text=text, shot=section.shot if included else None,
                kept_at_s=section.kept_at_s if included else None)
        for section, text, included in zip(original.sections, texts, keep, strict=True)))
    fmt, page_size = document_settings()
    # Render in isolation first: a failed PDF cannot destroy the saved document.
    with tempfile.TemporaryDirectory(prefix=".review-", dir=folder) as staging:
        stage = Path(staging)
        staged = replace(updated, sections=tuple(
            replace(s, shot=stage / s.shot.name) if s.shot else s for s in updated.sections))
        for s in updated.sections:
            if s.shot:
                shutil.copyfile(s.shot, stage / s.shot.name)
        document.write_markdown(staged, stage, document_format=fmt)
        document.write_pdf(staged, stage, document_format=fmt, page_size=page_size)
        document.write_manifest(staged, stage)
        if not (folder / "walkthrough.original.json").exists():
            atomic_json(folder / "walkthrough.original.json", read_json(folder / MANIFEST_NAME))
        # A previous write-up describes the old revision. Invalidate it in
        # the same publication, before a new source document can be copied.
        publish_files(stage, folder,
                      (MARKDOWN_NAME, PDF_NAME, MANIFEST_NAME),
                      remove=("write-up.md", "write-up.pdf"))
    return load_result(folder, spoken=True)


def retry_write_up(folder: Path, *, cancel_event=None) -> WalkthroughResult:
    from services.settings import (
        resolve_walkthrough_write_up_agent,
        resolve_walkthrough_write_up_instructions,
        resolve_walkthrough_write_up_notes,
        settings_manager,
    )
    from services.walkthrough.write_up import write_up

    result = load_result(folder, spoken=True)
    settings = settings_manager.load_all_settings()
    shots = [s.shot for s in load_document(folder).sections if s.shot]
    return write_up(result, shots, agent=resolve_walkthrough_write_up_agent(settings),
                    instructions=resolve_walkthrough_write_up_instructions(settings),
                    follow_notes=resolve_walkthrough_write_up_notes(settings),
                    cancel_event=cancel_event)


def attach_recovered_audio(root: Path, recordings: Path) -> int:
    """Join a draft to its journal's idempotently recovered WAV, even on a later restart."""
    count = 0
    for folder in entries(root):
        if (folder / MANIFEST_NAME).exists() or (folder / AUDIO_NAME).exists():
            continue
        try:
            draft = read_json(folder / DRAFT_NAME)
            journal = draft.get("journal_id", "")
            if not re.fullmatch(r"[0-9a-f]{32}", journal):
                continue
            matches = list(recordings.glob(f"recording_*-recovered-{journal}.wav"))
            if len(matches) == 1:
                source = local_file(recordings, matches[0].name)
                temporary = folder / ".audio.tmp"
                shutil.copyfile(source, temporary)
                os.replace(temporary, folder / AUDIO_NAME)
                count += 1
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return count


def recovery_session(folder: Path) -> WalkthroughSession:
    draft = read_json(folder / DRAFT_NAME)
    if draft.get("version") != 1:
        raise ValueError("Unsupported draft version")
    local_file(folder, AUDIO_NAME)
    session = WalkthroughSession(started_at=datetime.fromisoformat(draft["started_at"]),
                                  make_folder=lambda _: folder)
    # Reconstitute only successfully written screenshots. No writer is needed.
    from concurrent.futures import Future
    last = 0.0
    for item in draft["shots"]:
        name = item["shot"]
        if not _SHOT.fullmatch(name):
            raise ValueError("Invalid screenshot name")
        stamp = _seconds(item["kept_at_s"])
        if stamp < last:
            raise ValueError("Unordered screenshot times")
        last = stamp
        try:
            path = local_file(folder, name)
        except ValueError:
            continue  # a crash may precede the PNG write
        from PyQt6.QtGui import QImage
        if QImage(str(path)).isNull():
            continue
        session._shots.append(Shot(len(session._shots) + 1, path, stamp))
        saved = Future()
        saved.set_result(True)
        session._saves.append(saved)
    session._folder = folder
    session.close()
    return session


def retention_days(root: Path) -> int:
    try:
        value = read_json(root / PREFERENCES_NAME)["days"]
        return value if type(value) is int and 0 <= value <= 3650 else 0
    except (OSError, ValueError, KeyError, TypeError):
        return 0


def set_retention(root: Path, days: int) -> None:
    if type(days) is not int or not 0 <= days <= 3650:
        raise ValueError("Keep captures for 0–3650 days; 0 means forever")
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(root / PREFERENCES_NAME, {"days": days})


def expired_entries(root: Path, *, now: float | None = None) -> list[Path]:
    days = retention_days(root)
    if not days:
        return []
    cutoff = (time.time() if now is None else now) - days * 86400
    return [folder for folder in entries(root)
            if not (folder / DRAFT_NAME).exists()
            and (folder / MANIFEST_NAME).is_file()
            and (folder / MANIFEST_NAME).stat().st_mtime < cutoff]


def delete_entry(folder: Path) -> None:
    """Refuse links and unexpected files instead of recursively deleting user data."""
    if folder.is_symlink() or not _FOLDER.fullmatch(folder.name):
        raise ValueError("Not a walkthrough folder")
    files = list(folder.iterdir())
    if any(p.is_symlink() or not p.is_file()
           or (p.name not in _FIXED and not _SHOT.fullmatch(p.name)) for p in files):
        raise ValueError("Folder contains extra files; remove it in your file manager")
    for path in files:
        path.unlink()
    folder.rmdir()


def prune(root: Path) -> int:
    removed = 0
    for folder in expired_entries(root):
        try:
            delete_entry(folder)
            removed += 1
        except (OSError, ValueError):
            continue
    return removed
