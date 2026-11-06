"""Validation and service lookup shared by the native desktop commands.

This package deliberately imports no GUI toolkit. Commands are also usable by
tests and by other frontends with a small, explicit service context.
"""
from __future__ import annotations

import json
import math
from pathlib import Path


def settings(context):
    manager = getattr(context, "settings", None)
    if manager is None:
        from services.settings import settings_manager
        manager = settings_manager
    return manager


def history(context):
    manager = getattr(context, "history", None)
    if manager is None:
        from services.history_manager import history_manager
        manager = history_manager
    return manager


def repository(context):
    repo = getattr(context, "meetings", None)
    if repo is None:
        from meeting.persist.repository import SqlMeetingRepository
        repo = SqlMeetingRepository()
    return repo


def string(value, name, *, required=False, maximum=100_000):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"Invalid {name}.")
    return value


def identifier(params):
    return string(params.get("id"), "record ID", required=True, maximum=4096)


def boolean(value, name):
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false.")
    return value


def integer(value, name, low=0, high=10_000):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer between {low} and {high}.")
    return value


def page(params):
    return integer(params.get("offset", 0), "offset", high=1_000_000), integer(
        params.get("limit", 50), "limit", low=1, high=200
    )


def paginate(items, params):
    offset, limit = page(params)
    return {"items": items[offset:offset + limit], "total": len(items),
            "offset": offset, "limit": limit, "has_more": offset + limit < len(items)}


def changed(*methods, **extra):
    return {"ok": True, "refresh": list(methods), **extra}


def serializable(value):
    """Reject non-JSON data rather than exposing reprs of service objects."""
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def export_result(params, text, filename, mime="text/plain", *, source=None):
    result = {"filename": filename, "mime": mime}
    if source is None:
        result["text"] = text
    else:
        result["source_path"] = str(source)
    destination = params.get("path")
    if destination:
        path = Path(string(destination, "export path", maximum=4096)).expanduser()
        if not path.is_absolute() or not path.parent.is_dir():
            raise ValueError("Choose an absolute export path in an existing folder.")
        # Exclusive creation protects an existing document from an accidental
        # second click; the native save dialog can choose a different name.
        with path.open("xb") as handle:
            if source is None:
                handle.write(text.encode("utf-8"))
            else:
                with Path(source).open("rb") as incoming:
                    import shutil
                    shutil.copyfileobj(incoming, handle)
        result["path"] = str(path)
    return result


def finite(value, name, low=0, high=1_000_000):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Invalid {name}.")
    return value
