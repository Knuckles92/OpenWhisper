"""Shared display-formatting helpers."""
from datetime import datetime


def format_timestamp(iso_timestamp: str) -> str:
    """Format an ISO-8601 timestamp for the user's local timezone.

    Aware UTC values are converted with ``astimezone()``. Naive legacy values
    are treated as local wall time and are not shifted.
    """
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%b %d, %Y %I:%M %p")
    except Exception:
        return iso_timestamp


def format_audio_duration(seconds: float) -> str:
    """Format an audio duration without integer-truncating short clips.

    Args:
        seconds: Duration in seconds.

    Returns:
        Display string like ``"3.7s"``, ``"2m 5s"``, or ``"1h 3m"``.
    """
    if seconds is None:
        return "0.0s"
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "0.0s"
    if value < 0:
        value = 0.0
    if value < 60:
        return f"{value:.1f}s"
    total_seconds = int(round(value))
    hours, rem = divmod(total_seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        if minutes:
            return f"{hours}h {minutes}m"
        return f"{hours}h"
    return f"{minutes}m {secs}s"


def format_size_bytes(size_bytes: int) -> str:
    """Format advertised/download sizes with decimal units."""
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:.2f} GB"
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.0f} MB"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.0f} KB"
    return f"{size_bytes} B"


def format_sample_rate(hertz: int) -> str:
    """Format a sample rate the way audio tools label it (``44.1 kHz``)."""
    try:
        value = int(hertz)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    if value % 1000 == 0:
        return f"{value // 1000} kHz"
    return f"{value / 1000:.1f} kHz"


def format_file_size(size_bytes: float) -> str:
    """Format local file sizes with binary units."""
    if size_bytes < 1024:
        return f"{int(size_bytes)} B"
    size = size_bytes / 1024
    for unit in ("KB", "MB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
