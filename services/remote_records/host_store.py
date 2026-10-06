"""Keep paired computers' records on this computer.

A record arrives as files over the remote engine connection
(``records_begin``/``records_put``/``records_commit``, see
services/remote_asr/protocol.py). Each device stages its uploads in its own
folder, every file is checked against the size and SHA-256 the client
declared, and only a complete record is imported, by the handler for its
kind, into this computer's own history or meetings with the device as its
origin. From then on it shows up here like any other. That device, or a new
pairing explicitly assigned by the host owner, can access it again.

Nothing a client sends becomes a path without passing ``safe_name``, and
record ids are checked the same way, so a device can't write outside its
staging folder or reach another device's records.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from typing import Dict, Optional, Protocol

from services.remote_asr.protocol import RECORD_CHUNK_BYTES

logger = logging.getLogger(__name__)

RECORD_KINDS = ("dictation", "meeting")
MANIFEST = "record.json"
_RECORD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_NAME_DEPTH = 4
MAX_RECORD_FILES = 50_000
#: One record at most; a long meeting's audio is a few GB.
MAX_RECORD_BYTES = 64 * 1024 ** 3
#: record.json is read into memory, so it gets a tighter bound.
MAX_MANIFEST_BYTES = 256 * 1024 * 1024
#: Free space an upload must leave on this computer's disk.
FREE_SPACE_MARGIN = 1024 ** 3
#: An export made for ``records_fetch`` is dropped after this long unused.
EXPORT_TTL_S = 15 * 60
#: An upload nobody has touched for this long was abandoned (the record was
#: deleted, or the computer was unpaired); its staged files are dropped.
STAGING_TTL_S = 7 * 24 * 3600
MAX_LIST = 500


class RecordKind(Protocol):
    """How one kind of record is stored here (services/remote_records/kinds.py).

    Every method is per device. ``export`` writes record.json into
    ``dest_dir`` and returns the record's other files as ``{name: path}``,
    which are served from where they are rather than copied.
    """

    def import_record(self, record_id: str, files_dir: str, record: dict, device: dict) -> dict: ...

    def list(self, device_id: str, query: str, limit: int) -> list: ...

    def export(self, device_id: str, record_id: str, dest_dir: str) -> Dict[str, str]: ...

    def stored_files(self, device_id: str, record_id: str) -> Dict[str, int]: ...

    def delete(self, device_id: str, record_id: str) -> bool: ...

    def delete_all(self, device_id: str) -> int: ...

    def rename_owner(self, device_id: str, name: str) -> int: ...

    def summary(self, device_id: str) -> dict: ...

    def owners(self) -> list: ...

    def owns(self, device_id: str, record_id: str) -> bool: ...


def safe_name(name) -> str:
    """A client-supplied file name as a relative path, or ValueError.

    Forward slashes only, at most ``MAX_NAME_DEPTH`` levels, and each part
    starts with a letter or digit, so ``..``, dot files, drive letters and
    absolute paths can't be expressed.
    """
    if not isinstance(name, str) or not name or len(name) > 400:
        raise ValueError("Invalid file name in record.")
    parts = name.split("/")
    if len(parts) > MAX_NAME_DEPTH or not all(_SEGMENT.match(part) for part in parts):
        raise ValueError(f"Invalid file name in record: {name[:80]!r}")
    return "/".join(parts)


def safe_record_id(record_id) -> str:
    if not isinstance(record_id, str) or not _RECORD_ID.match(record_id):
        raise ValueError("Invalid record id.")
    return record_id


def safe_kind(kind) -> str:
    if kind not in RECORD_KINDS:
        raise ValueError("Unknown kind of record.")
    return kind


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _int(value, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise ValueError(f"Invalid {what}.")
    return value


class HostRecordStore:
    """Staging, verification and the ``records_*`` operations for this host.

    ``root`` holds ``staging/<device>/<kind>-<id>/`` for uploads under way
    and ``exports/<device>/<kind>-<id>/`` for records being fetched back.
    The records themselves live wherever each kind keeps its own.
    """

    def __init__(self, root: str, kinds: Dict[str, RecordKind]):
        self.root = os.path.abspath(root)
        self.kinds = dict(kinds)
        # One lock per upload keeps a resumed upload on a second connection
        # from interleaving with the first.
        self._locks: Dict[tuple, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ---- paths ----

    def _device_dir(self, area: str, device_id: str) -> str:
        if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
            raise ValueError("Unknown device.")
        return os.path.join(self.root, area, device_id)

    def _upload_dir(self, area: str, device_id: str, kind: str, record_id: str) -> str:
        return os.path.join(self._device_dir(area, device_id), f"{kind}-{record_id}")

    def _lock(self, *key) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    # ---- dispatch ----

    def handle(self, op: str, header: dict, payload: bytes, device: dict) -> dict:
        device_id = device.get("id")
        # Only the host service supplies these grants, after the host owner
        # assigns an unpaired computer's records to a fresh pairing.
        owners = list(dict.fromkeys([device_id, *device.get("record_owner_ids", [])]))
        for owner in owners:
            self._device_dir("staging", owner)
        if op == "records_list":
            kind = safe_kind(header.get("kind"))
            query = header.get("query") if isinstance(header.get("query"), str) else ""
            limit = header.get("limit", 100)
            limit = limit if isinstance(limit, int) and not isinstance(limit, bool) else 100
            limit = max(1, min(limit, MAX_LIST))
            records = [item for owner in owners
                       for item in self.kinds[kind].list(owner, query[:200], limit)]
            records.sort(key=lambda item: item.get("timestamp") or item.get("started_at") or "", reverse=True)
            return {"records": records[:limit]}
        if op == "records_clear":
            kind = safe_kind(header.get("kind"))
            return {"deleted": sum(self.kinds[kind].delete_all(owner) for owner in owners)}
        kind = safe_kind(header.get("kind"))
        record_id = safe_record_id(header.get("record_id"))
        owner_id = device_id
        if len(owners) > 1 and op in (
            "records_stat", "records_commit", "records_open", "records_fetch", "records_delete",
        ):
            owner_id = next((owner for owner in owners if self.kinds[kind].owns(owner, record_id)), device_id)
        if op == "records_stat":
            # What an earlier copy of this record left here, so a sender can
            # skip files that haven't changed (a meeting's audio, after an edit).
            return {"stored": self.kinds[kind].stored_files(owner_id, record_id)}
        if op == "records_begin":
            return self.begin(device_id, kind, record_id, header)
        if op == "records_put":
            return self.put(device_id, kind, record_id, header, payload)
        if op == "records_commit":
            return self.commit(device, kind, record_id, owner_id=owner_id)
        if op == "records_abort":
            self.abort(device_id, kind, record_id)
            return {}
        if op == "records_open":
            return self.open(owner_id, kind, record_id)
        if op == "records_fetch":
            return self.fetch(owner_id, kind, record_id, header)
        if op == "records_delete":
            deleted = self.kinds[kind].delete(owner_id, record_id)
            shutil.rmtree(self._upload_dir("exports", owner_id, kind, record_id), ignore_errors=True)
            return {"deleted": bool(deleted)}
        raise ValueError(f"Unknown operation: {op!r}")

    def delete_device(self, device_id: str) -> int:
        """Delete everything a device stored here, for when it's removed."""
        removed = 0
        for kind, handler in self.kinds.items():
            removed += handler.delete_all(device_id)
        for area in ("staging", "exports"):
            shutil.rmtree(self._device_dir(area, device_id), ignore_errors=True)
        return removed

    def rename_device(self, device_id: str, name: str) -> int:
        """Badge what a device stored here with its new name; how many records changed."""
        renamed = 0
        for kind, handler in self.kinds.items():
            try:
                renamed += handler.rename_owner(device_id, name)
            except Exception:
                logger.warning("Could not rename a device's %s records", kind, exc_info=True)
        return renamed

    def summary(self, device_id: str) -> dict:
        """What ``device_id`` has stored here, per kind: count and bytes."""
        result = {}
        for kind, handler in self.kinds.items():
            try:
                result[kind] = handler.summary(device_id)
            except Exception:
                logger.warning("Could not summarize %s records for a device", kind, exc_info=True)
        return result

    # ---- uploads ----

    def _manifest_path(self, upload_dir: str) -> str:
        return os.path.join(upload_dir, "upload.json")

    def _read_manifest(self, upload_dir: str) -> Optional[dict]:
        try:
            with open(self._manifest_path(upload_dir), "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            return manifest if isinstance(manifest, dict) else None
        except (OSError, ValueError):
            return None

    def _write_manifest(self, upload_dir: str, manifest: dict) -> None:
        path = self._manifest_path(upload_dir)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle)
        os.replace(tmp, path)

    def begin(self, device_id: str, kind: str, record_id: str, header: dict) -> dict:
        files = _int(header.get("files"), 1, MAX_RECORD_FILES, "file count")
        total = _int(header.get("bytes"), 0, MAX_RECORD_BYTES, "record size")
        upload_dir = self._upload_dir("staging", device_id, kind, record_id)
        self._prune("staging", STAGING_TTL_S, keep=upload_dir)
        with self._lock(device_id, kind, record_id):
            manifest = self._read_manifest(upload_dir)
            if manifest is None or manifest.get("files") != files or manifest.get("bytes") != total:
                # A different record under the same id (it was edited since):
                # what was staged for the old one is useless.
                shutil.rmtree(upload_dir, ignore_errors=True)
                os.makedirs(os.path.join(upload_dir, "files"), exist_ok=True)
                manifest = {"kind": kind, "record_id": record_id, "files": files,
                            "bytes": total, "declared": {}, "done": [],
                            "started": time.time()}
                self._write_manifest(upload_dir, manifest)
            received = {}
            for name in manifest["declared"]:
                path = os.path.join(upload_dir, "files", *name.split("/"))
                received[name] = os.path.getsize(path) if os.path.isfile(path) else 0
            still_needed = total - sum(received.values())
            os.makedirs(self.root, exist_ok=True)
            free = shutil.disk_usage(self.root).free
            if still_needed + FREE_SPACE_MARGIN > free:
                raise RuntimeError(
                    "Not enough free space on the host to keep this record "
                    f"({still_needed // (1024 ** 2)} MB needed)."
                )
            return {"received": received, "done": list(manifest["done"])}

    def put(self, device_id: str, kind: str, record_id: str, header: dict, payload: bytes) -> dict:
        name = safe_name(header.get("name"))
        size = _int(header.get("size"), 0, MAX_RECORD_BYTES, "file size")
        sha = header.get("sha256")
        if not isinstance(sha, str) or not _SHA256.match(sha):
            raise ValueError("Invalid file hash.")
        offset = _int(header.get("offset"), 0, MAX_RECORD_BYTES, "offset")
        if len(payload) > RECORD_CHUNK_BYTES:
            raise ValueError("Record chunk is too large.")
        if name == MANIFEST and size > MAX_MANIFEST_BYTES:
            raise ValueError("record.json is too large.")
        upload_dir = self._upload_dir("staging", device_id, kind, record_id)
        with self._lock(device_id, kind, record_id):
            manifest = self._read_manifest(upload_dir)
            if manifest is None:
                raise LookupError("Start the upload again.")
            declared = manifest["declared"]
            if name not in declared:
                if len(declared) >= manifest["files"]:
                    raise ValueError("More files than the record declared.")
                if sum(item["size"] for item in declared.values()) + size > manifest["bytes"]:
                    raise ValueError("Files larger than the record declared.")
                declared[name] = {"size": size, "sha256": sha}
                self._write_manifest(upload_dir, manifest)
            elif declared[name] != {"size": size, "sha256": sha}:
                raise ValueError(f"{name} changed during the upload; start again.")
            path = os.path.join(upload_dir, "files", *name.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            have = os.path.getsize(path) if os.path.isfile(path) else 0
            if name in manifest["done"]:
                return {"received": size, "done": True}
            if offset != have:
                # The client resumes from what's here; tell it where that is.
                return {"received": have, "done": False, "resume": True}
            if have + len(payload) > size:
                raise ValueError(f"{name} is longer than declared.")
            with open(path, "ab") as handle:
                handle.write(payload)
                if have + len(payload) >= size:
                    # Once per file: a chunk torn by a crash fails the hash
                    # below and the file is sent again.
                    handle.flush()
                    os.fsync(handle.fileno())
            have += len(payload)
            os.utime(upload_dir)
            if have < size:
                return {"received": have, "done": False}
            if file_sha256(path) != sha:
                os.remove(path)
                raise ValueError(f"{name} arrived damaged; it will be sent again.")
            manifest["done"].append(name)
            self._write_manifest(upload_dir, manifest)
            return {"received": have, "done": True}

    def commit(self, device: dict, kind: str, record_id: str, *, owner_id=None) -> dict:
        device_id = device.get("id")
        upload_dir = self._upload_dir("staging", device_id, kind, record_id)
        with self._lock(device_id, kind, record_id):
            manifest = self._read_manifest(upload_dir)
            if manifest is None:
                raise LookupError("Nothing was uploaded for this record.")
            done = set(manifest["done"])
            if len(done) != manifest["files"] or set(manifest["declared"]) != done:
                raise ValueError("The record's upload isn't complete.")
            if sum(item["size"] for item in manifest["declared"].values()) != manifest["bytes"]:
                raise ValueError("The record's size doesn't match what was declared.")
            if MANIFEST not in done:
                raise ValueError("The record has no record.json.")
            files_dir = os.path.join(upload_dir, "files")
            try:
                with open(os.path.join(files_dir, MANIFEST), "r", encoding="utf-8") as handle:
                    record = json.load(handle)
            except (OSError, ValueError) as exc:
                raise ValueError("The record's record.json can't be read.") from exc
            if not isinstance(record, dict) or record.get("kind") != kind:
                raise ValueError("The record's record.json is for another kind of record.")
            owner = dict(device, id=owner_id or device_id)
            result = self.kinds[kind].import_record(record_id, files_dir, record, owner)
            shutil.rmtree(upload_dir, ignore_errors=True)
            logger.info("Stored %s record %s for paired computer %s",
                        kind, record_id, device.get("name"))
            return {"stored": True, **(result or {})}

    def abort(self, device_id: str, kind: str, record_id: str) -> None:
        with self._lock(device_id, kind, record_id):
            shutil.rmtree(self._upload_dir("staging", device_id, kind, record_id), ignore_errors=True)

    # ---- reading back ----

    def _prune(self, area: str, ttl_s: float, keep: str = "") -> None:
        """Drop ``area``'s per-record folders untouched for ``ttl_s``."""
        base = os.path.join(self.root, area)
        cutoff = time.time() - ttl_s
        try:
            devices = os.listdir(base)
        except OSError:
            return
        for device in devices:
            device_dir = os.path.join(base, device)
            try:
                entries = os.listdir(device_dir)
            except OSError:
                continue
            for entry in entries:
                path = os.path.join(device_dir, entry)
                try:
                    if path != keep and os.path.getmtime(path) < cutoff:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    pass

    def open(self, device_id: str, kind: str, record_id: str) -> dict:
        """Snapshot a record's record.json for fetching and list its files.

        The audio is served from where it is kept, through the index
        written here, so bringing back a long meeting copies nothing first.
        """
        self._prune("exports", EXPORT_TTL_S)
        export_dir = self._upload_dir("exports", device_id, kind, record_id)
        with self._lock("export", device_id, kind, record_id):
            shutil.rmtree(export_dir, ignore_errors=True)
            os.makedirs(export_dir, exist_ok=True)
            try:
                paths = dict(self.kinds[kind].export(device_id, record_id, export_dir))
                paths[MANIFEST] = os.path.join(export_dir, MANIFEST)
                with open(os.path.join(export_dir, "index.json"), "w", encoding="utf-8") as handle:
                    json.dump(paths, handle)
            except BaseException:
                shutil.rmtree(export_dir, ignore_errors=True)
                raise
            files = [{"name": name, "size": os.path.getsize(path)} for name, path in paths.items()]
            files.sort(key=lambda item: (item["name"] != MANIFEST, item["name"]))
            return {"files": files, "bytes": sum(item["size"] for item in files)}

    def fetch(self, device_id: str, kind: str, record_id: str, header: dict) -> dict:
        name = safe_name(header.get("name"))
        offset = _int(header.get("offset"), 0, MAX_RECORD_BYTES, "offset")
        export_dir = self._upload_dir("exports", device_id, kind, record_id)
        try:
            with open(os.path.join(export_dir, "index.json"), "r", encoding="utf-8") as handle:
                paths = json.load(handle)
        except (OSError, ValueError):
            raise LookupError("Open the record again; its export has expired.") from None
        path = paths.get(name) if isinstance(paths, dict) else None
        if not isinstance(path, str) or not os.path.isfile(path):
            raise LookupError(f"{name} isn't part of this record anymore.")
        os.utime(export_dir)
        size = os.path.getsize(path)
        with open(path, "rb") as handle:
            handle.seek(offset)
            data = handle.read(RECORD_CHUNK_BYTES)
        return {
            "data": base64.b64encode(data).decode("ascii"),
            "offset": offset,
            "size": size,
            "eof": offset + len(data) >= size,
        }
