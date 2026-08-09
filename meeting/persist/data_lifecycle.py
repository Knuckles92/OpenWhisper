"""Recoverable deletion of meeting database rows and audio spools."""
from __future__ import annotations

import logging
import os
import shutil
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _validated_spool_path(spool_dir: str, meetings_root: str) -> Optional[str]:
    """Return a direct child spool path, rejecting traversal and broad paths."""
    if not spool_dir or not meetings_root:
        return None
    root = os.path.realpath(os.path.abspath(meetings_root))
    spool = os.path.realpath(os.path.abspath(spool_dir))
    if spool == root or os.path.dirname(spool) != root:
        raise ValueError("meeting spool is outside the configured meetings root")
    return spool


def delete_meeting_data(repository: Any, meeting_id: str,
                        meetings_root: str) -> bool:
    """Delete a meeting and its spool with rollback on database failure.

    The directory is first renamed inside its parent. That makes the database
    deletion the commit point while retaining a recoverable spool until the
    transaction succeeds.
    """
    meeting = repository.get_meeting(meeting_id)
    if meeting is None:
        return False
    spool = _validated_spool_path(
        str(meeting.get("spool_dir") or ""), meetings_root
    )
    tombstone = None
    if spool and os.path.isdir(spool):
        tombstone = os.path.join(
            os.path.dirname(spool), f".deleting-{meeting_id}-{uuid.uuid4().hex}"
        )
        os.replace(spool, tombstone)
    try:
        repository.delete_meeting(meeting_id)
    except Exception:
        if tombstone and os.path.isdir(tombstone) and not os.path.exists(spool):
            try:
                os.replace(tombstone, spool)
            except Exception:
                logger.exception("Could not restore meeting spool %s", meeting_id)
        raise
    if tombstone:
        try:
            shutil.rmtree(tombstone)
        except Exception:
            logger.exception("Could not purge tombstoned spool for %s", meeting_id)
    return True
