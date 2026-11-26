"""Structured agent & user notes, tags, and external references on saved records.

Annotations live in ``record_annotations`` alongside transcription history and
meetings, keyed by ``(record_kind, record_id)`` where ``record_kind`` is one of
``transcription``, ``meeting``, or ``walkthrough``.

This gives agents and users the best of both worlds:
- Human-readable summaries, notes, and tags visible right inside OpenWhisper
  without cluttering the main transcript views or altering original speech.
- Stable ``external_refs`` pointers (``{system, id, label?, url?}``) so an
  agent that keeps its own external database (Obsidian, Notion, SQLite, etc.)
  can link back to OpenWhisper's immutable record IDs and avoid duplicate work.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from sqlalchemy import select

from services.models import RecordAnnotation

logger = logging.getLogger(__name__)

RECORD_KINDS = ("transcription", "meeting", "walkthrough")
MAX_SUMMARY_CHARS = 500
MAX_NOTES_CHARS = 16_000
MAX_AUTHOR_CHARS = 80
MAX_TAGS = 20
MAX_TAG_CHARS = 64
MAX_EXTERNAL_REFS = 10
MAX_REF_SYSTEM_CHARS = 64
MAX_REF_ID_CHARS = 256
MAX_REF_LABEL_CHARS = 120
MAX_REF_URL_CHARS = 1024
ALLOWED_URL_SCHEMES = frozenset({"http", "https", "obsidian", "notion", "linear", "file"})


class AnnotationValidationError(ValueError):
    """Raised when an annotation field fails validation."""


@dataclass(frozen=True)
class AnnotationSummary:
    """Compact summary used by sidebar cards to render unobtrusive indicators."""

    record_kind: str
    record_id: str
    summary: str
    has_notes: bool
    tags: tuple[str, ...]
    external_ref_count: int
    author: str
    updated_at: str

    @property
    def has_content(self) -> bool:
        return bool(self.summary or self.has_notes or self.tags or self.external_ref_count)

    def tooltip(self) -> str:
        parts: list[str] = []
        if self.summary:
            parts.append(self.summary)
        if self.tags:
            parts.append("Tags: " + ", ".join(f"#{t}" for t in self.tags[:6]))
        if self.external_ref_count:
            noun = "link" if self.external_ref_count == 1 else "links"
            parts.append(f"{self.external_ref_count} external {noun}")
        if self.author:
            parts.append(f"By {self.author}")
        return "\n".join(parts) or "Agent notes attached"


def _has_disallowed_control(text: str, *, allow_newlines: bool = False) -> bool:
    for ch in text:
        code = ord(ch)
        if code == 0 or code == 0x7F:
            return True
        if code < 0x20:
            if allow_newlines and ch in ("\n", "\r", "\t"):
                continue
            return True
    return False


def normalize_kind(kind: str) -> str:
    if not isinstance(kind, str) or kind not in RECORD_KINDS:
        raise AnnotationValidationError(
            "record_kind must be one of transcription, meeting, or walkthrough."
        )
    return kind


def normalize_record_id(record_id: str) -> str:
    if (
        not isinstance(record_id, str)
        or not 1 <= len(record_id.strip()) <= 200
        or _has_disallowed_control(record_id)
        or any(ch in record_id for ch in ("/", "\\", "?", "#"))
    ):
        raise AnnotationValidationError("record_id must be a valid record identifier.")
    return record_id.strip()


def normalize_summary(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnnotationValidationError("summary must be a string or null.")
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    if len(cleaned) > MAX_SUMMARY_CHARS or _has_disallowed_control(cleaned):
        raise AnnotationValidationError(
            f"summary must be at most {MAX_SUMMARY_CHARS} characters without control characters."
        )
    return cleaned


def normalize_notes(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnnotationValidationError("notes must be a string or null.")
    cleaned = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return None
    if len(cleaned) > MAX_NOTES_CHARS or _has_disallowed_control(cleaned, allow_newlines=True):
        raise AnnotationValidationError(
            f"notes must be at most {MAX_NOTES_CHARS} characters without control characters."
        )
    return cleaned


def normalize_author(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AnnotationValidationError("author must be a string or null.")
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    if len(cleaned) > MAX_AUTHOR_CHARS or _has_disallowed_control(cleaned):
        raise AnnotationValidationError(
            f"author must be at most {MAX_AUTHOR_CHARS} characters."
        )
    return cleaned


def normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise AnnotationValidationError("tags must be a list of strings.")
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, str):
            raise AnnotationValidationError("Every tag must be a string.")
        tag = " ".join(raw.strip().lstrip("#").split())
        if not tag:
            continue
        if len(tag) > MAX_TAG_CHARS or _has_disallowed_control(tag):
            raise AnnotationValidationError(
                f"Each tag must be 1–{MAX_TAG_CHARS} characters without control characters."
            )
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
        if len(result) > MAX_TAGS:
            raise AnnotationValidationError(f"At most {MAX_TAGS} tags are allowed per record.")
    return result


def normalize_external_refs(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise AnnotationValidationError("external_refs must be a list of objects.")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise AnnotationValidationError("Each external reference must be an object.")
        system = item.get("system")
        ref_id = item.get("id")
        if (
            not isinstance(system, str)
            or not 1 <= len(system.strip()) <= MAX_REF_SYSTEM_CHARS
            or _has_disallowed_control(system)
        ):
            raise AnnotationValidationError(
                f"external_refs[].system must be 1–{MAX_REF_SYSTEM_CHARS} characters."
            )
        if (
            not isinstance(ref_id, str)
            or not 1 <= len(ref_id.strip()) <= MAX_REF_ID_CHARS
            or _has_disallowed_control(ref_id)
        ):
            raise AnnotationValidationError(
                f"external_refs[].id must be 1–{MAX_REF_ID_CHARS} characters."
            )
        norm_system = system.strip()
        norm_id = ref_id.strip()
        entry: dict[str, str] = {"system": norm_system, "id": norm_id}

        label = item.get("label")
        if label is not None and label != "":
            if (
                not isinstance(label, str)
                or len(label.strip()) > MAX_REF_LABEL_CHARS
                or _has_disallowed_control(label)
            ):
                raise AnnotationValidationError(
                    f"external_refs[].label must be at most {MAX_REF_LABEL_CHARS} characters."
                )
            if label.strip():
                entry["label"] = label.strip()

        url = item.get("url")
        if url is not None and url != "":
            if (
                not isinstance(url, str)
                or len(url.strip()) > MAX_REF_URL_CHARS
                or _has_disallowed_control(url)
            ):
                raise AnnotationValidationError(
                    f"external_refs[].url must be at most {MAX_REF_URL_CHARS} characters."
                )
            clean_url = url.strip()
            scheme = urlsplit(clean_url).scheme.lower()
            if scheme not in ALLOWED_URL_SCHEMES:
                raise AnnotationValidationError(
                    "external_refs[].url must use one of: "
                    + ", ".join(sorted(ALLOWED_URL_SCHEMES))
                    + "."
                )
            entry["url"] = clean_url

        dedupe_key = (norm_system.casefold(), norm_id)
        if dedupe_key in seen:
            # Replace earlier entry with the newer metadata for the same (system, id).
            result = [
                existing
                for existing in result
                if (existing["system"].casefold(), existing["id"]) != dedupe_key
            ]
        seen.add(dedupe_key)
        result.append(entry)
        if len(result) > MAX_EXTERNAL_REFS:
            raise AnnotationValidationError(
                f"At most {MAX_EXTERNAL_REFS} external_refs are allowed per record."
            )
    return result


def parse_tags_json(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(t) for t in parsed if isinstance(t, str) and t.strip()]
    except (ValueError, TypeError):
        pass
    return []


def parse_external_refs_json(raw: str | None) -> list[dict[str, str]]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            out: list[dict[str, str]] = []
            for item in parsed:
                if isinstance(item, dict) and item.get("system") and item.get("id"):
                    cleaned = {
                        "system": str(item["system"]),
                        "id": str(item["id"]),
                    }
                    if item.get("label"):
                        cleaned["label"] = str(item["label"])
                    if item.get("url"):
                        cleaned["url"] = str(item["url"])
                    out.append(cleaned)
            return out
    except (ValueError, TypeError):
        pass
    return []


def row_to_dict(row: Mapping[str, Any] | RecordAnnotation) -> dict[str, Any]:
    """Serialize a ``record_annotations`` row or ORM object to a public dict."""
    if isinstance(row, Mapping):
        kind = row["record_kind"]
        record_id = row["record_id"]
        summary = row["summary"]
        notes = row["notes"]
        tags = parse_tags_json(row["tags_json"])
        refs = parse_external_refs_json(row["external_refs_json"])
        author = row["author"]
        updated_at = row["updated_at"]
    else:
        kind = row.record_kind
        record_id = row.record_id
        summary = row.summary
        notes = row.notes
        tags = parse_tags_json(row.tags_json)
        refs = parse_external_refs_json(row.external_refs_json)
        author = row.author
        updated_at = row.updated_at
    return {
        "exists": True,
        "record_kind": kind,
        "record_id": record_id,
        "summary": summary,
        "notes": notes,
        "tags": tags,
        "external_refs": refs,
        "author": author,
        "updated_at": updated_at,
    }


def empty_annotation(record_kind: str, record_id: str) -> dict[str, Any]:
    return {
        "exists": False,
        "record_kind": record_kind,
        "record_id": record_id,
        "summary": None,
        "notes": None,
        "tags": [],
        "external_refs": [],
        "author": None,
        "updated_at": None,
    }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---- Desktop convenience helpers backed by services.database.db --------------


def get_annotation(record_kind: str, record_id: str) -> dict[str, Any]:
    """Read one record's annotation from the app database (empty dict shape if absent)."""
    from services.database import db

    kind = normalize_kind(record_kind)
    rid = normalize_record_id(record_id)
    with db.get_session() as session:
        row = session.get(RecordAnnotation, (kind, rid))
        return row_to_dict(row) if row is not None else empty_annotation(kind, rid)


def save_annotation(
    record_kind: str,
    record_id: str,
    *,
    summary: str | None = None,
    notes: str | None = None,
    tags: Iterable[str] | None = None,
    external_refs: Iterable[Mapping[str, Any]] | None = None,
    author: str | None = "User",
) -> dict[str, Any]:
    """Create, replace, or delete (when all fields are empty) one annotation."""
    from services.database import db

    kind = normalize_kind(record_kind)
    rid = normalize_record_id(record_id)
    norm_summary = normalize_summary(summary)
    norm_notes = normalize_notes(notes)
    norm_tags = normalize_tags(list(tags) if tags is not None else [])
    norm_refs = normalize_external_refs(list(external_refs) if external_refs is not None else [])
    norm_author = normalize_author(author)

    if not (norm_summary or norm_notes or norm_tags or norm_refs):
        delete_annotation(kind, rid)
        return empty_annotation(kind, rid)

    now = utc_now_iso()
    with db.get_session() as session:
        row = session.get(RecordAnnotation, (kind, rid))
        if row is None:
            row = RecordAnnotation(
                record_kind=kind,
                record_id=rid,
                summary=norm_summary,
                notes=norm_notes,
                tags_json=json.dumps(norm_tags, ensure_ascii=False),
                external_refs_json=json.dumps(norm_refs, ensure_ascii=False),
                author=norm_author,
                updated_at=now,
            )
            session.add(row)
        else:
            row.summary = norm_summary
            row.notes = norm_notes
            row.tags_json = json.dumps(norm_tags, ensure_ascii=False)
            row.external_refs_json = json.dumps(norm_refs, ensure_ascii=False)
            row.author = norm_author
            row.updated_at = now
        session.flush()
        return row_to_dict(row)


def delete_annotation(record_kind: str, record_id: str) -> bool:
    """Remove an annotation row without touching the underlying record."""
    from services.database import db

    kind = normalize_kind(record_kind)
    rid = normalize_record_id(record_id)
    with db.get_session() as session:
        row = session.get(RecordAnnotation, (kind, rid))
        if row is None:
            return False
        session.delete(row)
        return True


def annotation_summaries_for_kind(
    record_kind: str, record_ids: Iterable[str] | None = None
) -> dict[str, AnnotationSummary]:
    """Batch-load compact annotation indicators for sidebar rendering (one query)."""
    from services.database import db

    kind = normalize_kind(record_kind)
    ids = [str(rid) for rid in record_ids] if record_ids is not None else None
    if ids is not None and not ids:
        return {}
    try:
        with db.get_session() as session:
            stmt = select(
                RecordAnnotation.record_id,
                RecordAnnotation.summary,
                RecordAnnotation.notes,
                RecordAnnotation.tags_json,
                RecordAnnotation.external_refs_json,
                RecordAnnotation.author,
                RecordAnnotation.updated_at,
            ).where(RecordAnnotation.record_kind == kind)
            if ids is not None:
                stmt = stmt.where(RecordAnnotation.record_id.in_(ids))
            rows = session.execute(stmt).mappings().all()
    except Exception:
        logger.debug("Could not read record annotations for %s", kind, exc_info=True)
        return {}

    result: dict[str, AnnotationSummary] = {}
    for row in rows:
        tags = tuple(parse_tags_json(row["tags_json"]))
        refs = parse_external_refs_json(row["external_refs_json"])
        summary = AnnotationSummary(
            record_kind=kind,
            record_id=row["record_id"],
            summary=row["summary"] or "",
            has_notes=bool(row["notes"]),
            tags=tags,
            external_ref_count=len(refs),
            author=row["author"] or "",
            updated_at=row["updated_at"] or "",
        )
        if summary.has_content:
            result[row["record_id"]] = summary
    return result
