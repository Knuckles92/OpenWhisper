"""Translate raw exceptions into concise, user-friendly UI error messages.

Technical exception details are logged (or tucked behind ``setDetailedText`` on
a ``QMessageBox``) instead of being dumped directly into status banners and
dialog bodies.
"""
from __future__ import annotations

import errno
import logging
import sqlite3
from typing import Optional

from PyQt6.QtWidgets import QMessageBox, QWidget

logger = logging.getLogger(__name__)

_TECHNICAL_MARKERS = (
    "traceback",
    "sqlite3.",
    "errno ",
    "[winerror",
    "winerror",
    "0x",
    "stacktrace",
    "sqlalchemy",
    "urllib",
    "httpconnectionpool",
    "httpsconnectionpool",
    "sslerror",
    "certificate_verify_failed",
    "eof occurred",
    "broken pipe",
    "connection refused",
    "connection reset",
    "getaddrinfo failed",
    "name or service not known",
)


def friendly_error_message(
    exc: BaseException | str,
    fallback: str = "Something went wrong. Please try again.",
) -> str:
    """Return a clean user-facing message for ``exc``.

    Maps common filesystem, database, permission, and network errors to plain
    language. Short, already-readable domain messages (such as validation
    ``ValueError``s) are preserved; raw trace/OS/driver strings fall back to
    ``fallback``.
    """
    if isinstance(exc, PermissionError):
        return "Permission denied. Check folder or app permissions and try again."
    if isinstance(exc, FileNotFoundError):
        return "The file or folder could not be found."
    if isinstance(exc, IsADirectoryError):
        return "Expected a file, but found a folder instead."
    if isinstance(exc, TimeoutError):
        return "The operation timed out. Please try again."
    if isinstance(exc, ConnectionError):
        return "Could not connect. Check your network or host and try again."
    if isinstance(exc, sqlite3.OperationalError):
        lowered = str(exc).lower()
        if "locked" in lowered or "busy" in lowered:
            return "The database is busy right now. Please try again in a moment."
        if "disk" in lowered or "full" in lowered:
            return "Not enough free disk space to save changes."
        return "Could not read or write local data. Please try again."
    if isinstance(exc, sqlite3.DatabaseError):
        return "Local data could not be read. Please try again."
    if isinstance(exc, OSError):
        err_no = getattr(exc, "errno", None)
        if err_no == errno.ENOSPC:
            return "Not enough free disk space to complete this action."
        if err_no == errno.EROFS:
            return "The destination is read-only."
        if err_no in (errno.EACCES, errno.EPERM):
            return "Permission denied. Check file permissions and try again."
        if err_no == errno.ENOENT:
            return "The file or folder could not be found."
        return fallback

    raw = str(exc).strip()
    if not raw:
        return fallback

    first_line = raw.splitlines()[0].strip()
    lowered = first_line.lower()
    if any(marker in lowered for marker in _TECHNICAL_MARKERS):
        if "timed out" in lowered or "timeout" in lowered:
            return "The operation timed out. Please try again."
        if (
            "connection" in lowered
            or "getaddrinfo" in lowered
            or "name or service not known" in lowered
            or "httpconnectionpool" in lowered
            or "httpsconnectionpool" in lowered
        ):
            return "Could not connect. Check your network or host and try again."
        if "permission" in lowered or "access is denied" in lowered:
            return "Permission denied. Check permissions and try again."
        if "no space left" in lowered or "disk full" in lowered:
            return "Not enough free disk space to complete this action."
        return fallback

    if len(first_line) > 200:
        return fallback
    return first_line


def format_action_error(
    prefix: str,
    exc: BaseException | str,
    *,
    fallback: Optional[str] = None,
) -> str:
    """Format ``f"{prefix}: <friendly>"`` without duplicating punctuation."""
    reason = friendly_error_message(
        exc,
        fallback=fallback or "Please try again.",
    )
    clean_prefix = prefix.rstrip(":. ")
    if not reason:
        return clean_prefix
    return f"{clean_prefix}: {reason}"


def show_warning_dialog(
    parent: Optional[QWidget],
    title: str,
    exc: BaseException | str,
    *,
    fallback: str = "Something went wrong. Please try again.",
) -> None:
    """Show a warning ``QMessageBox`` with friendly copy and optional details."""
    friendly = friendly_error_message(exc, fallback=fallback)
    raw = str(exc).strip()
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(friendly)
    if raw and raw != friendly:
        box.setDetailedText(raw)
    try:
        box.exec()
    finally:
        box.deleteLater()
