"""UTC storage and local display helpers for Meeting Mode timestamps."""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Any, Dict, Optional


def utc_now_iso() -> str:
    """Return the current instant as an unambiguous UTC ISO string."""
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_meeting_time(value: Any) -> Optional[datetime]:
    """Parse aware UTC/new timestamps and legacy naive local timestamps."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def as_local_time(
    value: Any,
    *,
    local_tz: Optional[tzinfo] = None,
) -> Optional[datetime]:
    """Convert aware values to local time; preserve legacy naive wall time."""
    parsed = parse_meeting_time(value)
    if parsed is None or parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(local_tz) if local_tz is not None else parsed.astimezone()


def elapsed_seconds(started_at: Any, ended_at: Any) -> Optional[float]:
    """Return elapsed wall seconds across aware and legacy timestamp pairs."""
    started = parse_meeting_time(started_at)
    ended = parse_meeting_time(ended_at)
    if started is None or ended is None:
        return None
    if (started.tzinfo is None) != (ended.tzinfo is None):
        # A mixed pair can occur across an upgrade. Python treats a naive
        # datetime passed to astimezone() as local wall time, matching the
        # legacy storage contract.
        if started.tzinfo is None:
            started = started.astimezone()
        if ended.tzinfo is None:
            ended = ended.astimezone()
    return (ended - started).total_seconds()


def seconds_since(value: Any) -> Optional[float]:
    """Return age in seconds using a clock compatible with the stored shape."""
    parsed = parse_meeting_time(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return (datetime.now() - parsed).total_seconds()
    return (
        datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    ).total_seconds()


def format_meeting_started_at(value: Any) -> str:
    """Format a meeting start time for Qt history and leftover identity."""
    started = as_local_time(value)
    return started.strftime("%b %d, %Y · %I:%M %p") if started else "Unknown date"


def format_meeting_duration(meeting: Dict[str, Any]) -> str:
    """Format elapsed meeting time minus pause credit."""
    elapsed = elapsed_seconds(
        meeting.get("started_at"), meeting.get("ended_at")
    )
    if elapsed is None:
        return ""
    try:
        seconds = max(
            0,
            int(elapsed)
            - int(float(meeting.get("paused_total_s") or 0)),
        )
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if not hours and not minutes:
        return f"{remaining_seconds} sec"
    return f"{hours}h {minutes}m" if hours else f"{minutes} min"


def format_meeting_identity_meta(meeting: Dict[str, Any]) -> str:
    """Join start time and duration for the leftover identity line."""
    started = format_meeting_started_at(meeting.get("started_at"))
    duration = format_meeting_duration(meeting)
    return f"{started} · {duration}" if duration else started
