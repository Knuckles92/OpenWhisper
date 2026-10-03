"""Shared display-title contract for history, dashboards, and MCP."""

MAX_TITLE_LENGTH = 200
TITLE_ERROR_MESSAGE = (
    f"Supply a title of 1–{MAX_TITLE_LENGTH} characters without control characters."
)


def title_error(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "invalid_text"
    if len(value) > MAX_TITLE_LENGTH:
        return "text_too_long"
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return "invalid_text"
    return None


def normalize_title(value: object) -> str:
    if not isinstance(value, str) or title_error(value):
        raise ValueError(TITLE_ERROR_MESSAGE)
    return value.strip()
