"""Shared display-formatting helpers."""
from datetime import datetime


def format_timestamp(iso_timestamp: str) -> str:
    """Format an ISO-8601 timestamp, returning raw input if parsing fails."""
    try:
        dt = datetime.fromisoformat(iso_timestamp)
        return dt.strftime("%b %d, %Y %I:%M %p")
    except Exception:
        return iso_timestamp


def format_size_bytes(size_bytes: int) -> str:
    """Format advertised/download sizes with decimal units."""
    if size_bytes >= 1_000_000_000:
        return f"{size_bytes / 1_000_000_000:.2f} GB"
    if size_bytes >= 1_000_000:
        return f"{size_bytes / 1_000_000:.0f} MB"
    if size_bytes >= 1_000:
        return f"{size_bytes / 1_000:.0f} KB"
    return f"{size_bytes} B"


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
