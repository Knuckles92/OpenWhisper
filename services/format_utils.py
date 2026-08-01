"""Shared display-formatting helpers.

Single home for human-readable formatting used by both the data layer
(history models) and UI widgets, so timestamps and sizes render identically
everywhere.
"""
from datetime import datetime


def format_timestamp(iso_timestamp: str) -> str:
    """Format an ISO-8601 timestamp for display.

    Args:
        iso_timestamp: Timestamp string as stored (``datetime.isoformat()``).

    Returns:
        Display string like ``"Jun 28, 2026 01:42 PM"``, or the raw input if
        it cannot be parsed.
    """
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        return dt.strftime("%b %d, %Y %I:%M %p")
    except Exception:
        return iso_timestamp


def format_size_bytes(size_bytes: int) -> str:
    """Return a human-readable size for an actual on-disk byte count.

    Uses decimal units, matching how model and download sizes are advertised.
    Prefer this for anything the user compares against a published size;
    :func:`format_file_size` uses binary units and suits local file listings.

    Args:
        size_bytes: Size in bytes.

    Returns:
        A string like ``"1.53 GB"`` / ``"145 MB"`` / ``"12 KB"``.
    """
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:.2f} GB"
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.0f} MB"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.0f} KB"
    return f"{size_bytes} B"


def format_file_size(size_bytes: float) -> str:
    """Format a byte count for display.

    Args:
        size_bytes: Size in bytes.

    Returns:
        Display string like ``"512 B"``, ``"1.2 KB"``, ``"3.4 MB"`` or
        ``"1.1 GB"``.
    """
    if size_bytes < 1024:
        return f"{int(size_bytes)} B"
    size = size_bytes / 1024
    for unit in ("KB", "MB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
