"""This computer's side of keeping records on its paired host.

Records are always saved here first, exactly as without a host, so nothing
is lost when the host is asleep or away. When the storage location
(``records_location``) is "host" or "both", saving one also adds it to an
outbox (the ``record_sync`` table), and a worker thread sends it over its
own connection, never the one dictation uses, resuming where it stopped:

* "both" copies it and keeps a row saying the host has it, so a later
  delete here removes the host's copy too, and an edited meeting is sent
  again (just its record.json when the audio is already there).
* "host" moves it: once the host has verified and stored it, the copy here
  is deleted. History and Past Meetings list it from the host.

Meetings wait until nothing more will be written to them (``ready``).
Viewing a host's meeting here downloads a temporary copy, sent back if it
was edited and dropped again on the next start. "Bring back" moves
everything the host holds for this computer back here.

Nothing here imports Qt: listeners are called on whichever thread changed
something.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from services.remote_asr import protocol
from services.remote_asr import settings as remote_settings
from services.remote_records.host_store import MANIFEST, file_sha256, safe_name

logger = logging.getLogger(__name__)

KINDS = ("dictation", "meeting")
#: The worker looks at the outbox at least this often.
IDLE_WAKE_S = 60.0
#: How often copies kept on both computers are checked for edits here.
DIGEST_CHECK_S = 10 * 60.0
#: A viewing copy of a host's meeting goes back on the next start, or once it
#: is this old and nothing here has it open.
CHECKOUT_TTL = timedelta(hours=1)
#: After a failed attempt the worker waits this long, then longer.
RETRY_BACKOFF_S = (5.0, 15.0, 60.0, 300.0)
#: Listing from the host, for History, gives up after this long.
LIST_TIMEOUT_S = 8.0
#: After the host was unreachable, History doesn't try it again for this long.
UNREACHABLE_QUIET_S = 30.0

Listener = Callable[[str], None]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class RecordsUnavailable(RuntimeError):
    """A user-facing reason the host's records can't be reached right now."""


@dataclass(frozen=True)
class SyncStatus:
    """What the storage section of Settings shows."""

    location: str
    paired: bool
    host_name: str = ""
    #: True: the host keeps records; False: its owner hasn't turned that on;
    #: None: not known yet, or a host too old to keep any.
    host_keeps: Optional[bool] = None
    host_supports: Optional[bool] = None
    pending: int = 0
    waiting: str = ""
    active: str = ""
    error: str = ""
    #: This computer's records on the host: {"dictation": {"count", "bytes"}, ...}.
    stored: Dict[str, dict] = field(default_factory=dict)
    local_counts: Dict[str, int] = field(default_factory=dict)


class RecordSync:
    """The outbox worker, plus the host-side reads History needs.

    ``kinds`` maps each kind to its handler (services/remote_records/kinds.py);
    ``connect`` opens an authenticated ``RemoteConnection`` to the paired
    host (tests pass their own).
    """

    def __init__(self, kinds: Optional[dict] = None, *,
                 connect: Optional[Callable[[], object]] = None,
                 cache_dir: Optional[str] = None):
        if kinds is None:
            from services.remote_records.kinds import DictationRecords, MeetingRecords

            kinds = {"dictation": DictationRecords(), "meeting": MeetingRecords()}
        self.kinds = dict(kinds)
        self._connect_fn = connect
        self._cache_dir = cache_dir
        self._lock = threading.RLock()
        self._transfer_lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._listeners: List[Listener] = []
        # Deletes the worker makes itself (after a move) aren't the user's.
        self._quiet_deletes: set = set()
        self._host_keeps: Optional[bool] = None
        self._host_supports: Optional[bool] = None
        self._stored: Dict[str, dict] = {}
        self._waiting = ""
        self._active = ""
        self._error = ""
        self._failures = 0
        self._retry_at = 0.0
        self._digest_at = 0.0
        self._unreachable_at = 0.0
        self._retry_failed = False
        self._progress_at = 0.0
        self._remote_items: Dict[tuple, dict] = {}

    # ---- listeners ----

    def add_listener(self, listener: Listener) -> None:
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener: Listener) -> None:
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)

    def _notify(self, kind: str = "status") -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(kind)
            except Exception:
                logger.debug("Record sync listener raised", exc_info=True)

    def _set(self, **fields) -> None:
        changed = False
        with self._lock:
            for name, value in fields.items():
                if getattr(self, f"_{name}") != value:
                    setattr(self, f"_{name}", value)
                    changed = True
        if changed:
            self._notify()

    # ---- the outbox table ----

    @staticmethod
    def _session():
        from services.database import db

        return db.get_session()

    def _row(self, kind: str, record_id: str):
        from services.models import RecordSync as Row

        with self._session() as session:
            return session.get(Row, (kind, record_id))

    def _rows(self, *, state: Optional[str] = None, action: Optional[str] = None) -> list:
        from services.models import RecordSync as Row

        with self._session() as session:
            query = session.query(Row)
            if state is not None:
                query = query.filter(Row.state == state)
            if action is not None:
                query = query.filter(Row.action == action)
            return query.order_by(Row.created_at).all()

    def _put(self, kind: str, record_id: str, **fields) -> None:
        from services.models import RecordSync as Row

        with self._session() as session:
            row = session.get(Row, (kind, record_id))
            if row is None:
                row = Row(kind=kind, record_id=record_id, created_at=_now(),
                          attempts=0, state="pending", **{"host_fingerprint": "", "action": "copy"})
                session.add(row)
            for name, value in fields.items():
                setattr(row, name, value)
            row.updated_at = _now()

    def _drop(self, kind: str, record_id: str) -> None:
        from services.models import RecordSync as Row

        with self._session() as session:
            row = session.get(Row, (kind, record_id))
            if row is not None:
                session.delete(row)

    def copies_on_host(self, kind: str, record_ids) -> set:
        """Which of ``record_ids`` the paired host also holds (kept on both)."""
        from services.models import RecordSync as Row

        ids = list(record_ids)
        pairing = remote_settings.load_client_pairing()
        if not ids or pairing is None:
            return set()
        with self._session() as session:
            rows = session.query(Row.record_id).filter(
                Row.kind == kind, Row.record_id.in_(ids),
                Row.state == "done", Row.action.in_(("copy", "move")),
                Row.host_fingerprint == pairing.fingerprint,
            ).all()
        return {record_id for (record_id,) in rows}

    # ---- settings ----

    def location(self) -> str:
        return remote_settings.records_location()

    def set_location(self, location: str) -> None:
        """Change where new records go. Records already moved stay where they are."""
        from services.settings import SettingsKey, settings_manager

        if location not in remote_settings.RECORD_LOCATIONS:
            raise ValueError(f"Unknown storage location: {location!r}")
        settings_manager.save_setting(SettingsKey.REMOTE_RECORDS_LOCATION, location)
        for row in self._rows(state="pending") + self._rows(state="failed"):
            if row.action == "delete":
                continue
            if location == "local" and not row.content_digest:
                # Never reached the host: it stays here, as asked now.
                self._drop(row.kind, row.record_id)
            elif location != "local":
                self._put(row.kind, row.record_id, action="move" if location == "host" else "copy",
                          state="pending")
        self._notify()
        self.wake()

    def _pairing(self):
        pairing = remote_settings.load_client_pairing()
        token = remote_settings.load_client_token() if pairing is not None else None
        return (pairing, token) if pairing is not None and token else (None, None)

    # ---- hooks from where records are saved and deleted ----

    def record_saved(self, kind: str, record_id: str) -> None:
        """A record was saved here; queue it if it belongs on the host too."""
        try:
            location = self.location()
            if location == "local":
                return
            pairing = remote_settings.load_client_pairing()
            if pairing is None:
                return
            self._put(kind, record_id, host_fingerprint=pairing.fingerprint,
                      action="move" if location == "host" else "copy", state="pending",
                      attempts=0, last_error=None)
        except Exception:
            logger.warning("Could not queue a %s record for the host", kind, exc_info=True)
            return
        self._notify()
        self.wake()

    def record_deleted(self, kind: str, record_id: str) -> None:
        """A record was deleted here; delete the host's copy of it too."""
        with self._lock:
            if (kind, record_id) in self._quiet_deletes:
                return
        try:
            row = self._row(kind, record_id)
            if row is None or row.action == "delete":
                return
            if row.state == "done" or row.content_digest:
                self._put(kind, record_id, action="delete", state="pending", attempts=0,
                          last_error=None)
            else:
                self._drop(kind, record_id)
        except Exception:
            logger.warning("Could not queue deleting the host's copy of a %s record", kind, exc_info=True)
            return
        self._notify()
        self.wake()

    #: The outbox row that stands for "delete all of a kind on the host".
    ALL = "*"

    def cleared(self, kind: str, keep=()) -> None:
        """History (or Past Meetings) was cleared here: clear it on the host too.

        Its records there would otherwise reappear in the list, which shows
        both computers' records. ``keep`` are records the clear left alone
        (a meeting still running), which still go to the host later.
        """
        pairing = remote_settings.load_client_pairing()
        if pairing is None:
            return
        keep = set(keep or ())
        try:
            for row in self._rows():
                if row.kind == kind and row.record_id not in keep:
                    self._drop(row.kind, row.record_id)
            self._put(kind, self.ALL, host_fingerprint=pairing.fingerprint, action="delete",
                      state="pending", attempts=0, last_error=None)
        except Exception:
            logger.warning("Could not queue clearing the host's %s records", kind, exc_info=True)
            return
        self._notify()
        self.wake()

    def send_existing(self) -> int:
        """Queue every record kept only here: moved or copied as the location says."""
        location = self.location()
        pairing = remote_settings.load_client_pairing()
        if location == "local" or pairing is None:
            return 0
        action = "move" if location == "host" else "copy"
        queued = 0
        for kind, handler in self.kinds.items():
            for record_id in handler.local_ids():
                row = self._row(kind, record_id)
                if row is not None and row.action == "delete":
                    continue
                if row is not None and row.state == "done":
                    if action == "move":
                        # The host has it already; send any edits, then let go.
                        self._put(kind, record_id, action="move", state="pending")
                        queued += 1
                    continue
                self._put(kind, record_id, host_fingerprint=pairing.fingerprint,
                          action=action, state="pending", attempts=0, last_error=None)
                queued += 1
        self._notify()
        self.wake()
        return queued

    # ---- lifecycle ----

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="record-sync", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            thread, self._thread = self._thread, None
        self._stop.set()
        self._wake.set()
        if thread is not None:
            thread.join(timeout)

    def wake(self, *, now: bool = False) -> None:
        """Look at the outbox soon; ``now`` also skips a retry wait and
        retries records that failed too often to be tried on their own."""
        if now:
            self._retry_at = 0.0
            self._unreachable_at = 0.0
            self._retry_failed = True
        self._wake.set()

    def _run(self) -> None:
        self._clear_cache()
        first = True
        while not self._stop.is_set():
            if not first:
                self._wake.wait(IDLE_WAKE_S)
                self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.run_once(startup=first)
            except Exception:
                logger.exception("Record sync failed")
            first = False

    # ---- the work ----

    def status(self) -> SyncStatus:
        pairing = remote_settings.load_client_pairing()
        pending = [row for row in self._rows(state="pending") + self._rows(state="failed")]
        with self._lock:
            return SyncStatus(
                location=self.location(),
                paired=pairing is not None,
                host_name=pairing.host_name if pairing is not None else "",
                host_keeps=self._host_keeps,
                host_supports=self._host_supports,
                pending=len(pending),
                waiting=self._waiting,
                active=self._active,
                error=self._error,
                stored=dict(self._stored),
            )

    def _connect(self):
        if self._connect_fn is not None:
            return self._connect_fn()
        from services.remote_asr.client import RemoteConnection

        pairing, token = self._pairing()
        if pairing is None:
            raise RecordsUnavailable("Pair with a host first.")
        return RemoteConnection(pairing.host, pairing.port, token, pairing.fingerprint,
                                alternates=pairing.alternates)

    def _open(self):
        """A connected connection, with what ``ready`` says about records noted."""
        connection = self._connect()
        try:
            ready = connection.connect() if not getattr(connection, "ready", None) else connection.ready
        except Exception:
            try:
                connection.close()
            except Exception:
                pass
            raise
        capabilities = ready.get("capabilities") if isinstance(ready.get("capabilities"), dict) else {}
        supports = "records" in capabilities
        self._set(host_supports=supports,
                  host_keeps=(capabilities.get("records") is True) if supports else None,
                  stored=dict(ready.get("records") or {}) if supports else {})
        return connection

    def run_once(self, *, startup: bool = False) -> None:
        with self._transfer_lock:
            self._run_once(startup=startup)

    @contextmanager
    def paused_for_backup(self):
        if not self._transfer_lock.acquire(timeout=30):
            raise RuntimeError("Records are still moving to or from the paired host. Try backup again after sync finishes.")
        try:
            yield
        finally:
            self._transfer_lock.release()

    def _run_once(self, *, startup: bool = False) -> None:
        """One pass over the outbox. Blocking; the worker thread's body."""
        pairing, _token = self._pairing()
        if pairing is None:
            self._set(waiting="", active="", error="")
            return
        self._retarget(pairing.fingerprint)
        due_digest = startup or time.monotonic() - self._digest_at >= DIGEST_CHECK_S
        if due_digest:
            self._digest_at = time.monotonic()
            self._check_edits(pairing.fingerprint)
        rows = self._rows(state="pending")
        if self._retry_failed:
            self._retry_failed = False
            rows += self._rows(state="failed")
        checkouts = self._due_checkouts(pairing.fingerprint, everything=startup)
        if not rows and not checkouts and not startup:
            return
        if time.monotonic() < self._retry_at:
            return
        try:
            connection = self._open()
        except Exception as exc:
            self._failed_attempt(str(exc) or "Couldn't reach the host.")
            return
        try:
            if not self._host_supports and (rows or checkouts):
                self._set(waiting=f"Update OpenWhisper on {pairing.host_name} to keep records there.")
                return
            for row in rows:
                if self._stop.is_set():
                    return
                self._process(connection, row, pairing.host_name)
            for row in checkouts:
                if self._stop.is_set():
                    return
                self._return_checkout(connection, row)
            self._failures = 0
            self._retry_at = 0.0
        except RecordsUnavailable as exc:
            self._failed_attempt(str(exc))
        finally:
            self._set(active="")
            try:
                connection.close()
            except Exception:
                pass

    def _failed_attempt(self, message: str) -> None:
        delay = RETRY_BACKOFF_S[min(self._failures, len(RETRY_BACKOFF_S) - 1)]
        self._failures += 1
        self._retry_at = time.monotonic() + delay
        self._set(error=message, active="")

    def _retarget(self, fingerprint: str) -> None:
        """Pending records follow the pairing: to the host paired now."""
        for row in self._rows(state="pending") + self._rows(state="failed"):
            if row.host_fingerprint != fingerprint and not row.content_digest:
                self._put(row.kind, row.record_id, host_fingerprint=fingerprint)

    def _check_edits(self, fingerprint: str) -> None:
        """Queue meetings kept on both computers that were edited here since."""
        handler = self.kinds.get("meeting")
        if handler is None:
            return
        for row in self._rows(state="done", action="copy"):
            if row.kind != "meeting" or row.host_fingerprint != fingerprint:
                continue
            try:
                if not handler.exists(row.record_id):
                    continue
                if handler.digest(row.record_id) != row.content_digest:
                    self._put(row.kind, row.record_id, state="pending")
            except Exception:
                logger.debug("Could not check a meeting for edits", exc_info=True)

    def _due_checkouts(self, fingerprint: str, *, everything: bool) -> list:
        """Viewing copies to send back to the host paired now; others wait for theirs."""
        cutoff = (datetime.now(timezone.utc) - CHECKOUT_TTL).isoformat()
        due = []
        for row in self._rows(state="done", action="move"):
            handler = self.kinds.get(row.kind)
            if row.host_fingerprint != fingerprint:
                continue
            if not everything and row.updated_at >= cutoff:
                continue
            if handler is not None and handler.in_use(row.record_id):
                continue
            due.append(row)
        return due

    def _process(self, connection, row, host_name: str) -> None:
        handler = self.kinds.get(row.kind)
        if handler is None:
            self._drop(row.kind, row.record_id)
            return
        if row.action == "delete":
            try:
                if row.record_id == self.ALL:
                    connection.request("records_clear", kind=row.kind, timeout=300)
                else:
                    connection.request("records_delete", kind=row.kind, record_id=row.record_id,
                                       timeout=60)
            except Exception as exc:
                self._record_failure(row, exc)
                return
            self._drop(row.kind, row.record_id)
            self._notify("records")
            return
        if not handler.exists(row.record_id):
            self._drop(row.kind, row.record_id)
            return
        reason = handler.ready(row.record_id)
        if reason:
            self._set(waiting=f"Waiting to send a {row.kind}: {reason}.")
            return
        if self._host_keeps is not True:
            self._set(waiting=(
                f"{host_name} isn't keeping records for other computers yet. Turn on "
                "\"Keep records for paired computers\" in its Settings → Remote engine."
            ))
            raise RecordsUnavailable(self._waiting)
        try:
            self.upload(connection, row.kind, row.record_id)
        except RecordsUnavailable:
            raise
        except Exception as exc:
            self._record_failure(row, exc)
            return
        self._uploaded(row.kind, row.record_id, row.action)
        self._set(waiting="", error="")

    def _uploaded(self, kind: str, record_id: str, action: str) -> None:
        handler = self.kinds[kind]
        try:
            digest = handler.digest(record_id)
        except Exception:
            digest = "sent"
        # A moved record open here (a meeting's dashboard) stays as a viewing
        # copy until it's closed; edits made meanwhile are sent back then.
        self._put(kind, record_id, state="done", content_digest=digest,
                  attempts=0, last_error=None)
        if action == "move" and not handler.in_use(record_id):
            self._delete_quietly(kind, record_id)
            self._drop(kind, record_id)
        self._notify("records")

    def _delete_quietly(self, kind: str, record_id: str) -> None:
        with self._lock:
            self._quiet_deletes.add((kind, record_id))
        try:
            self.kinds[kind].delete_local(record_id)
        finally:
            with self._lock:
                self._quiet_deletes.discard((kind, record_id))

    def _record_failure(self, row, exc: Exception) -> None:
        from services.remote_asr.client import RemoteEngineError

        message = str(exc) or type(exc).__name__
        code = getattr(exc, "code", None)
        if code == "forbidden":
            self._set(host_keeps=False)
            raise RecordsUnavailable(message) from exc
        if code == "busy":
            # A backup pause must not exhaust a record's retry budget or
            # cause host-only storage to discard the client's only copy.
            raise RecordsUnavailable(message) from exc
        if isinstance(exc, RemoteEngineError):
            # The connection dropped; everything after this fails the same way.
            raise RecordsUnavailable(message) from exc
        attempts = (row.attempts or 0) + 1
        self._put(row.kind, row.record_id, attempts=attempts, last_error=message[:500],
                  state="failed" if attempts >= 5 else "pending")
        logger.warning("Could not send %s record %s to the host: %s", row.kind, row.record_id, message)
        self._set(error=message)

    def _return_checkout(self, connection, row) -> None:
        """Send a viewing copy's edits back, then drop it here."""
        handler = self.kinds.get(row.kind)
        try:
            if handler is not None and handler.exists(row.record_id):
                if handler.digest(row.record_id) != row.content_digest:
                    self.upload(connection, row.kind, row.record_id)
                self._delete_quietly(row.kind, row.record_id)
            self._drop(row.kind, row.record_id)
            self._notify("records")
        except RecordsUnavailable:
            raise
        except Exception as exc:
            logger.warning("Could not return a viewing copy to the host: %s", exc)

    # ---- sending one record ----

    def _retention(self) -> dict:
        from services.settings import (
            resolve_max_saved_recordings,
            resolve_max_saved_recordings_bytes,
            settings_manager,
        )

        settings = settings_manager.load_all_settings()
        return {"max_recordings": resolve_max_saved_recordings(settings),
                "max_bytes": resolve_max_saved_recordings_bytes(settings)}

    def upload(self, connection, kind: str, record_id: str) -> None:
        """Send one record, resuming what the host already has, and commit it."""
        handler = self.kinds[kind]
        if kind == "dictation":
            bundle = handler.bundle(None, record_id, retention=self._retention())
        else:
            bundle = handler.bundle(None, record_id)
        files = dict(bundle.files)
        if kind == "meeting":
            stored = connection.request("records_stat", kind=kind, record_id=record_id,
                                        timeout=30).get("stored") or {}
            unchanged = {name for name, path in files.items()
                         if stored.get(name) == os.path.getsize(path)}
            if unchanged:
                bundle.record["update"] = True
                files = {name: path for name, path in files.items() if name not in unchanged}
        manifest = bundle.manifest_bytes()
        items = [(name, path, os.path.getsize(path)) for name, path in sorted(files.items())]
        items.append((MANIFEST, None, len(manifest)))
        total = sum(size for _name, _path, size in items)
        for attempt in range(2):
            try:
                self._send(connection, kind, record_id, items, manifest, total)
                break
            except Exception as exc:
                if attempt or getattr(exc, "code", None) != "bad_request":
                    raise
                # A file changed since a partial upload: start this one over.
                connection.request("records_abort", kind=kind, record_id=record_id, timeout=30)
        connection.request("records_commit", kind=kind, record_id=record_id, timeout=300)

    def _send(self, connection, kind, record_id, items, manifest: bytes, total: int) -> None:
        begin = connection.request("records_begin", kind=kind, record_id=record_id,
                                   files=len(items), bytes=total, timeout=60)
        received = begin.get("received") if isinstance(begin.get("received"), dict) else {}
        done = set(begin.get("done") or [])
        sent = sum(size for name, _path, size in items if name in done)
        sent += sum(int(received.get(name) or 0) for name, _path, _size in items if name not in done)
        label = "meeting" if kind == "meeting" else "dictation"
        for name, path, size in items:
            if name in done:
                continue
            if self._stop.is_set():
                raise RecordsUnavailable("Stopped.")
            sha = file_sha256(path) if path else hashlib.sha256(manifest).hexdigest()
            offset = int(received.get(name) or 0)
            handle = open(path, "rb") if path else None
            try:
                while True:
                    if handle is not None:
                        handle.seek(offset)
                        data = handle.read(protocol.RECORD_CHUNK_BYTES)
                    else:
                        data = manifest[offset:offset + protocol.RECORD_CHUNK_BYTES]
                    reply = connection.request(
                        "records_put", kind=kind, record_id=record_id, name=name, size=size,
                        sha256=sha, offset=offset, payload=data, timeout=120,
                    )
                    if reply.get("done"):
                        sent += size - offset
                        break
                    new_offset = int(reply.get("received") or 0)
                    sent += new_offset - offset
                    offset = new_offset
                    self._progress(label, sent, total)
            finally:
                if handle is not None:
                    handle.close()
            self._progress(label, sent, total)

    def _progress(self, label: str, sent: int, total: int) -> None:
        """At most four updates a second: each one refreshes Settings."""
        now = time.monotonic()
        if not total or now - self._progress_at < 0.25:
            return
        self._progress_at = now
        self._set(active=f"Sending a {label} · {min(99, sent * 100 // total)}%")

    # ---- reading the host's records ----

    def refresh_summary(self) -> None:
        """Ask the host what it keeps for this computer. Blocking; off the UI thread."""
        if self._pairing()[0] is None:
            return
        try:
            connection = self._open()
        except Exception:
            logger.debug("Could not reach the host for its record summary", exc_info=True)
            return
        connection.close()

    def listing_wanted(self, kind: str) -> bool:
        """Whether History should ask the host: it may hold records of this computer's."""
        if self._pairing()[0] is None:
            return False
        with self._lock:
            stored = self._stored.get(kind) if isinstance(self._stored, dict) else None
            known = self._host_supports is not None
        if self.location() != "local":
            return True
        # Kept here now, but earlier records may still be on the host.
        return not known or bool(isinstance(stored, dict) and stored.get("count"))

    def list_remote(self, kind: str, query: str = "", limit: int = 100) -> List[dict]:
        """This computer's records of ``kind`` on the host, newest first.

        Raises RecordsUnavailable with the reason when the host can't be
        asked right now; skips trying for a while after it was unreachable.
        """
        pairing, _token = self._pairing()
        if pairing is None:
            return []
        if time.monotonic() - self._unreachable_at < UNREACHABLE_QUIET_S:
            raise RecordsUnavailable(f"{pairing.host_name} isn't reachable right now.")
        try:
            connection = self._open()
        except Exception as exc:
            self._unreachable_at = time.monotonic()
            raise RecordsUnavailable(f"{pairing.host_name} isn't reachable right now.") from exc
        try:
            if not self._host_supports:
                return []
            reply = connection.request("records_list", kind=kind, query=query, limit=limit,
                                       timeout=LIST_TIMEOUT_S)
        except Exception as exc:
            raise RecordsUnavailable(str(exc) or "The host didn't answer.") from exc
        finally:
            connection.close()
        items = [item for item in reply.get("records") or [] if isinstance(item, dict)
                 and isinstance(item.get("id"), str)]
        with self._lock:
            for item in items:
                self._remote_items[(kind, item["id"])] = dict(item, stored_on=pairing.host_name)
        return [dict(item, stored_on=pairing.host_name) for item in items]

    def remote_item(self, kind: str, record_id: str) -> Optional[dict]:
        """The last listing's copy of one host record, for opening it."""
        with self._lock:
            item = self._remote_items.get((kind, record_id))
            return dict(item) if item is not None else None

    def delete_remote(self, kind: str, record_id: str) -> bool:
        """Delete the host's copy now (History's delete of a host record)."""
        connection = self._open()
        try:
            reply = connection.request("records_delete", kind=kind, record_id=record_id, timeout=60)
        finally:
            connection.close()
        self._drop(kind, record_id)
        with self._lock:
            self._remote_items.pop((kind, record_id), None)
        self._notify("records")
        return bool(reply.get("deleted"))

    def _fetch(self, connection, kind: str, record_id: str, dest_dir: str,
               progress: Optional[Callable[[int, int], None]] = None) -> dict:
        """Download one record into ``dest_dir``; returns its record.json."""
        import json

        opened = connection.request("records_open", kind=kind, record_id=record_id, timeout=120)
        files = [item for item in opened.get("files") or [] if isinstance(item, dict)]
        total = sum(int(item.get("size") or 0) for item in files)
        got = 0
        for item in files:
            name = safe_name(item.get("name"))
            size = int(item.get("size") or 0)
            path = os.path.join(dest_dir, *name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                offset = 0
                while True:
                    reply = connection.request("records_fetch", kind=kind, record_id=record_id,
                                               name=name, offset=offset, timeout=120)
                    data = base64.b64decode(reply.get("data") or "")
                    handle.write(data)
                    offset += len(data)
                    got += len(data)
                    if progress is not None:
                        progress(got, total)
                    if reply.get("eof") or not data:
                        break
            if os.path.getsize(path) != size:
                raise RuntimeError(f"{name} came back incomplete.")
        with open(os.path.join(dest_dir, MANIFEST), "r", encoding="utf-8") as handle:
            record = json.load(handle)
        if not isinstance(record, dict) or record.get("kind") != kind:
            raise RuntimeError("The host sent a record this version can't read.")
        return record

    def _cache_root(self) -> str:
        if self._cache_dir is not None:
            return self._cache_dir
        from config import user_data_path

        return os.path.join(user_data_path("remote_records"), "cache")

    def _clear_cache(self) -> None:
        shutil.rmtree(self._cache_root(), ignore_errors=True)

    def audio_for(self, record_id: str) -> str:
        """A local path to a host-kept history entry's recording (cached until restart)."""
        folder = os.path.join(self._cache_root(), safe_name(record_id))
        cached = os.path.join(folder, "audio")
        if os.path.isdir(cached) and os.listdir(cached):
            return os.path.join(cached, sorted(os.listdir(cached))[0])
        connection = self._open()
        try:
            shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder, exist_ok=True)
            record = self._fetch(connection, "dictation", record_id, folder)
        finally:
            connection.close()
        audio = record.get("audio")
        if not isinstance(audio, str):
            raise RecordsUnavailable("That entry has no recording on the host.")
        return os.path.join(folder, *safe_name(audio).split("/"))

    def check_out(self, kind: str, record_id: str,
                  progress: Optional[Callable[[int, int], None]] = None) -> None:
        """Download a host record here to open it.

        Kept on both computers with "both"; otherwise a viewing copy, sent
        back if edited and dropped again on the next start.
        """
        connection = self._open()
        try:
            with tempfile.TemporaryDirectory(dir=self._staging_parent()) as folder:
                record = self._fetch(connection, kind, record_id, folder, progress)
                self.kinds[kind].import_record(record_id, folder, record, None)
        finally:
            connection.close()
        pairing, _token = self._pairing()
        action = "copy" if self.location() == "both" else "move"
        try:
            digest = self.kinds[kind].digest(record_id)
        except Exception:
            digest = "fetched"
        self._put(kind, record_id, host_fingerprint=pairing.fingerprint if pairing else "",
                  action=action, state="done", content_digest=digest, attempts=0, last_error=None)
        self._notify("records")

    def _staging_parent(self) -> str:
        from config import user_data_path

        parent = user_data_path("remote_records")
        os.makedirs(parent, exist_ok=True)
        return parent

    def bring_back(self, progress: Optional[Callable[[str], None]] = None) -> int:
        with self._transfer_lock:
            return self._bring_back(progress)

    def _bring_back(self, progress: Optional[Callable[[str], None]] = None) -> int:
        """Move every record the host holds for this computer back here. Blocking."""
        moved = 0
        connection = self._open()
        try:
            for kind in KINDS:
                handler = self.kinds.get(kind)
                if handler is None:
                    continue
                skipped = set()
                while not self._stop.is_set():
                    items = [item for item in connection.request(
                        "records_list", kind=kind, limit=100, timeout=60,
                    ).get("records") or [] if item.get("id") not in skipped]
                    if not items:
                        break
                    for item in items:
                        record_id = str(item["id"])
                        if progress is not None:
                            progress(f"Bringing back {moved + 1}…")
                        try:
                            with tempfile.TemporaryDirectory(dir=self._staging_parent()) as folder:
                                record = self._fetch(connection, kind, record_id, folder)
                                handler.import_record(record_id, folder, record, None)
                        except Exception:
                            logger.warning("Could not bring back %s %s", kind, record_id, exc_info=True)
                            skipped.add(record_id)
                            continue
                        connection.request("records_delete", kind=kind, record_id=record_id, timeout=60)
                        self._drop(kind, record_id)
                        moved += 1
        finally:
            connection.close()
            self._notify("records")
        return moved


class _LazyRecordSync:
    def __init__(self) -> None:
        self._instance: Optional[RecordSync] = None
        self._lock = threading.Lock()

    def _get(self) -> RecordSync:
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    self._instance = RecordSync()
        return self._instance

    def __getattr__(self, name: str):
        return getattr(self._get(), name)


record_sync = _LazyRecordSync()
