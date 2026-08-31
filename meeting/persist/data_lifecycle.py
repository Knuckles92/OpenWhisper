"""Recoverable deletion of meeting database rows and audio spools."""
from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from typing import Any, Iterable, Optional, Set

logger = logging.getLogger(__name__)

# Live capture statuses. Past Meetings hides these, and bulk clear must
# never remove them even if a caller forgets to pass skip_ids.
_NON_HISTORICAL_STATUSES = frozenset({"active", "paused", "ending"})

# A live delete holds its tombstone only for the database transaction and one
# rmtree.  Preserve recent names for concurrent callers, but collect abandoned
# tombstones left by a crash instead of leaking meeting audio indefinitely.
DELETION_TOMBSTONE_STALE_S = 60 * 60


def _validated_spool_path(spool_dir: str, meetings_root: str) -> Optional[str]:
    """Return a direct child spool path, rejecting traversal and broad paths."""
    if not spool_dir or not meetings_root:
        return None
    root = os.path.realpath(os.path.abspath(meetings_root))
    spool = os.path.realpath(os.path.abspath(spool_dir))
    if spool == root or os.path.dirname(spool) != root:
        raise ValueError("meeting spool is outside the configured meetings root")
    return spool


def delete_meeting_data(
    repository: Any,
    meeting_id: str,
    meetings_root: str,
    *,
    delete_spool: bool = True,
) -> bool:
    """Delete a meeting and optionally its spool with rollback on database failure.

    When ``delete_spool`` is true the directory is first renamed inside its
    parent. That makes the database deletion the commit point while retaining
    a recoverable spool until the transaction succeeds. When false, only the
    database rows are removed and the spool is left on disk.
    """
    meeting = repository.get_meeting(meeting_id)
    if meeting is None:
        return False
    if not delete_spool:
        repository.delete_meeting(meeting_id)
        return True
    spool = _validated_spool_path(
        str(meeting.get("spool_dir") or ""), meetings_root
    )
    tombstone = None
    if spool and os.path.isdir(spool):
        tombstone = os.path.join(
            os.path.dirname(spool), f".deleting-{meeting_id}-{uuid.uuid4().hex}"
        )
        try:
            os.replace(spool, tombstone)
            # A rename preserves the directory's old mtime.  Refresh it so
            # orphan cleanup measures the age of this deletion transaction,
            # not the age of the meeting's last audio chunk.
            os.utime(tombstone, None)
        except Exception:
            if os.path.isdir(tombstone) and not os.path.exists(spool):
                try:
                    os.replace(tombstone, spool)
                except Exception:
                    logger.exception(
                        "Could not restore meeting spool after tombstone setup failed: %s",
                        meeting_id,
                    )
            raise
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


def _purge_orphan_spools(
    meetings_root: str, keep_spools: Iterable[str]
) -> None:
    """Remove leftover meeting directories that are not in ``keep_spools``."""
    if not meetings_root or not os.path.isdir(meetings_root):
        return
    root = os.path.realpath(os.path.abspath(meetings_root))
    keep: Set[str] = set()
    for path in keep_spools:
        if not path:
            continue
        keep.add(os.path.realpath(os.path.abspath(path)))
    try:
        names = os.listdir(root)
    except OSError:
        logger.exception("Could not list meeting spools in %s", root)
        return
    for name in names:
        # Concurrent single-meeting deletes rename the spool to this prefix
        # before the database commit.  Leave recent ones alone, but a crash can
        # strand the renamed audio forever, so collect stale tombstones.
        if name.startswith(".deleting-"):
            path = os.path.join(root, name)
            try:
                age_s = max(0.0, time.time() - os.stat(path).st_mtime)
            except OSError:
                continue
            if age_s < DELETION_TOMBSTONE_STALE_S:
                continue
            try:
                shutil.rmtree(path)
            except Exception:
                logger.exception(
                    "Could not purge abandoned deletion tombstone %s", path
                )
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        real = os.path.realpath(os.path.abspath(path))
        if real == root or os.path.dirname(real) != root:
            continue
        if real in keep:
            continue
        try:
            shutil.rmtree(real)
        except Exception:
            logger.exception("Could not purge leftover meeting spool %s", real)


def clear_meetings(
    repository: Any,
    meetings_root: str,
    *,
    delete_spools: bool,
    skip_ids: Optional[Iterable[str]] = None,
) -> int:
    """Delete historical meetings, optionally purging audio spools.

    Live capture rows (active / paused / ending) and any id in ``skip_ids``
    are left untouched. When ``delete_spools`` is true, leftover directories
    under ``meetings_root`` that are not a skipped meeting's spool are
    removed as well — including orphans from earlier keep-recording deletes.

    Returns:
        Count of meetings whose database rows were removed.
    """
    skipped = {str(item) for item in (skip_ids or ()) if item}
    keep_spools: Set[str] = set()
    removed = 0
    for meeting in list(repository.list_meetings() or []):
        meeting_id = str(meeting.get("id") or "")
        if not meeting_id:
            continue
        status = str(meeting.get("status") or "").lower()
        if meeting_id in skipped or status in _NON_HISTORICAL_STATUSES:
            spool = str(meeting.get("spool_dir") or "")
            if spool:
                keep_spools.add(spool)
            continue
        if delete_meeting_data(
            repository,
            meeting_id,
            meetings_root,
            delete_spool=delete_spools,
        ):
            removed += 1
    if delete_spools:
        _purge_orphan_spools(meetings_root, keep_spools)
    return removed
