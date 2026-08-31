"""Bulk export assembly for transcription history.

Selection criteria and document assembly stay pure (standard library plus
display formatters). The only I/O is ``write_per_entry_files``, which owns
its output directory. The Qt export dialog owns threading and path picking.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from services.format_utils import (
    format_audio_duration,
    format_file_size,
    format_timestamp,
)

FORMAT_MARKDOWN = "markdown"
FORMAT_TXT = "txt"
FORMAT_JSON = "json"
EXPORT_FORMATS = (FORMAT_MARKDOWN, FORMAT_TXT, FORMAT_JSON)
FORMAT_VERSION = 1

_FILE_STEM_MAX_TITLE = 60
_JSON_FIELDS = (
    "id",
    "timestamp",
    "model",
    "text",
    "raw_text",
    "cleanup_provider",
    "cleanup_model",
    "audio_file",
    "transcription_time",
    "audio_duration",
    "file_size",
    "source_name",
)


def serialize_history_entry(entry: Any) -> Dict[str, Any]:
    """Turn a history row into a JSON-safe export dict."""
    timestamp = getattr(entry, "timestamp", "") or ""
    text = getattr(entry, "text", "") or ""
    audio_file = getattr(entry, "audio_file", None)
    return {
        "id": getattr(entry, "id", "") or "",
        "timestamp": timestamp,
        "model": getattr(entry, "model", "") or "",
        "text": text,
        "raw_text": getattr(entry, "raw_text", None),
        "cleanup_provider": getattr(entry, "cleanup_provider", None),
        "cleanup_model": getattr(entry, "cleanup_model", None),
        "audio_file": audio_file,
        "transcription_time": getattr(entry, "transcription_time", None),
        "audio_duration": getattr(entry, "audio_duration", None),
        "file_size": getattr(entry, "file_size", None),
        "source_name": getattr(entry, "source_name", None),
        "formatted_timestamp": format_timestamp(timestamp) if timestamp else "",
        "preview_text": _preview_text(text),
        "has_audio": bool(audio_file),
    }


def list_export_entries(manager: Any = None) -> List[Dict[str, Any]]:
    """Return history entries newest first, ready for the export dialog."""
    if manager is None:
        from services.history_manager import history_manager as manager
    return [serialize_history_entry(entry) for entry in manager.get_history()]


def filter_export_entries(
    entries: List[Dict[str, Any]],
    *,
    from_dt: Optional[datetime] = None,
    to_dt: Optional[datetime] = None,
    only_with_audio: bool = False,
) -> List[Dict[str, Any]]:
    """Apply the export criteria to serialized history rows.

    Date bounds are naive local wall-clock values (matching a date-only
    picker); stored timestamps are converted to local time before
    comparing. Entries whose timestamp cannot be parsed are excluded
    whenever a date bound is set.
    """
    filtered: List[Dict[str, Any]] = []
    for entry in entries:
        if from_dt is not None or to_dt is not None:
            stamped = _as_local(entry.get("timestamp"))
            if stamped is None:
                continue
            stamped = stamped.replace(tzinfo=None)
            if from_dt is not None and stamped < from_dt:
                continue
            if to_dt is not None and stamped > to_dt:
                continue
        if only_with_audio and not entry.get("has_audio"):
            continue
        filtered.append(entry)
    return filtered


def render_entry_document(
    entry: Dict[str, Any],
    fmt: str,
    *,
    include_cleaned: bool = True,
    include_raw: bool = True,
) -> str:
    """Render one history entry in the requested format.

    The cleaned and raw toggles only apply to Markdown; the txt format is a
    transcript by definition and the JSON export is the complete record.
    """
    if fmt == FORMAT_JSON:
        return json.dumps(
            _json_payload(entry), indent=2, ensure_ascii=False
        )
    if fmt == FORMAT_TXT:
        return _render_txt(entry)
    return _render_markdown(
        entry, include_cleaned=include_cleaned, include_raw=include_raw
    )


def render_export_document(
    entries: List[Dict[str, Any]],
    fmt: str,
    *,
    include_cleaned: bool = True,
    include_raw: bool = True,
) -> str:
    """Render all entries as one combined document."""
    if fmt == FORMAT_JSON:
        envelope = {
            "format_version": FORMAT_VERSION,
            "exported_at": datetime.now().isoformat(timespec="seconds"),
            "entries": [_json_payload(entry) for entry in entries],
        }
        return json.dumps(envelope, indent=2, ensure_ascii=False)
    if fmt == FORMAT_TXT:
        separator = "\n" + ("-" * 60) + "\n\n"
        return separator.join(_render_txt(entry) for entry in entries)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = (
        f"# Transcription History Export\n\n"
        f"{len(entries)} transcription(s) · exported {stamp}\n\n"
        "---\n\n"
    )
    body = "\n\n---\n\n".join(
        _render_markdown(
            entry, include_cleaned=include_cleaned, include_raw=include_raw
        ).rstrip()
        for entry in entries
    )
    return header + body + "\n"


def write_per_entry_files(
    entries: List[Dict[str, Any]],
    fmt: str,
    out_dir: str,
    *,
    include_cleaned: bool = True,
    include_raw: bool = True,
) -> List[str]:
    """Write one file per entry, returning the paths written in order."""
    os.makedirs(out_dir, exist_ok=True)
    used: set[str] = set()
    written: List[str] = []
    for entry in entries:
        stem = entry_file_stem(entry)
        candidate = stem
        suffix = 2
        document = render_entry_document(
            entry,
            fmt,
            include_cleaned=include_cleaned,
            include_raw=include_raw,
        )
        while True:
            filename = f"{candidate}.{fmt}"
            path = os.path.join(out_dir, filename)
            if filename in used:
                candidate = f"{stem}-{suffix}"
                suffix += 1
                continue
            try:
                # Exclusive creation also closes the check/write race if two
                # exports target the same directory at the same time.
                with open(path, "x", encoding="utf-8") as handle:
                    handle.write(document)
            except FileExistsError:
                candidate = f"{stem}-{suffix}"
                suffix += 1
                continue
            break
        used.add(filename)
        written.append(path)
    return written


def entry_file_stem(entry: Dict[str, Any]) -> str:
    """Filesystem-safe ``YYYYMMDD_HHMMSS_preview`` stem for one entry."""
    stamped = _as_local(entry.get("timestamp"))
    stamp = stamped.strftime("%Y%m%d_%H%M%S") if stamped else "unknown_date"
    title = (entry.get("preview_text") or entry.get("text") or "").strip()
    if title == "Empty transcript":
        title = ""
    slug = re.sub(r"\s+", "-", title)
    slug = re.sub(r"[^\w\-.]", "", slug).strip("-._")
    slug = slug[:_FILE_STEM_MAX_TITLE].rstrip("-._")
    return f"{stamp}_{slug}" if slug else stamp


def _json_payload(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {key: entry.get(key) for key in _JSON_FIELDS}


def _preview_text(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return "Empty transcript"
    max_len = 100
    if len(body) <= max_len:
        return body
    return body[:max_len].rsplit(" ", 1)[0] + "..."


def _as_local(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone()
    return parsed


def _was_cleaned(entry: Dict[str, Any]) -> bool:
    return bool(entry.get("cleanup_model") or entry.get("raw_text"))


def _cleanup_label(entry: Dict[str, Any]) -> str:
    model = entry.get("cleanup_model") or ""
    if not model:
        return "Cleaned"
    provider = entry.get("cleanup_provider") or ""
    return f"{provider} · {model}" if provider else model


def _render_markdown(
    entry: Dict[str, Any],
    *,
    include_cleaned: bool,
    include_raw: bool,
) -> str:
    title = entry.get("formatted_timestamp") or entry.get("timestamp") or "Untitled"
    lines = [f"## {title}", ""]
    facts = [f"- Model: {entry.get('model') or 'unknown'}"]
    if _was_cleaned(entry):
        facts.append(f"- Cleanup: {_cleanup_label(entry)}")
    source_name = entry.get("source_name")
    if source_name:
        facts.append(f"- Source: {source_name}")
    duration = entry.get("audio_duration")
    if duration is not None:
        facts.append(f"- Duration: {format_audio_duration(duration)}")
    file_size = entry.get("file_size")
    if file_size is not None:
        facts.append(f"- File size: {format_file_size(file_size)}")
    if entry.get("has_audio") and entry.get("audio_file"):
        facts.append(f"- Audio: {entry['audio_file']}")
    lines.extend(facts)
    lines.append("")
    if include_cleaned:
        text = (entry.get("text") or "").strip()
        lines.append(text if text else "_Empty transcript_")
        lines.append("")
    if include_raw and entry.get("raw_text"):
        lines.append("### Raw")
        lines.append("")
        lines.append(str(entry["raw_text"]).rstrip())
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_txt(entry: Dict[str, Any]) -> str:
    stamp = entry.get("formatted_timestamp") or entry.get("timestamp") or ""
    model = entry.get("model") or ""
    lines = [f"[{stamp}] {model}"]
    if _was_cleaned(entry):
        lines.append(f"Cleanup: {_cleanup_label(entry)}")
    text = (entry.get("text") or "").rstrip()
    if text:
        lines.append(text)
    raw = entry.get("raw_text")
    if raw:
        lines.append("")
        lines.append("Raw:")
        lines.append(str(raw).rstrip())
    return "\n".join(lines).rstrip() + "\n"
