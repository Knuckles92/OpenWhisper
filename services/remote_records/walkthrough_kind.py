"""Walkthrough records as files, and back again.

A walkthrough is a folder containing ``walkthrough.json`` plus its rendered
Markdown/PDF/Word documents, optional AI write-ups, and PNG screenshots.
``WalkthroughRecords`` implements the same ``RecordKind`` contract as
``DictationRecords`` and ``MeetingRecords``:

* Local walkthroughs live directly under ``captures/<record_id>/``.
* Paired computers' walkthroughs stored on a host live under
  ``captures/devices/<device_id>/<record_id>/``, outside the top-level
  folder layout so the host's own library listing and retention never touch
  them.
* Every imported file name passes ``safe_name``, must live directly inside
  the walkthrough folder with an allowed extension (``.md``, ``.pdf``,
  ``.docx``, ``.png``, ``.json``), and is verified never to escape the
  walkthrough library root.
"""
from __future__ import annotations

import builtins
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Callable

from services.remote_records.host_store import (
    MANIFEST,
    file_sha256,
    safe_name,
    safe_record_id,
)
from services.remote_records.kinds import FORMAT, Bundle, _move, _origin_name
from services.walkthrough.storage import MANIFEST_NAME

logger = logging.getLogger(__name__)

#: File extensions permitted inside a synced walkthrough folder.
ALLOWED_EXTENSIONS = frozenset({".md", ".pdf", ".docx", ".png", ".json"})

#: Files skipped when bundling and refused on import (in-progress drafts / audio).
_SKIP_FILES = frozenset({"draft.json", "audio.wav", "owner.json"})
_SKIP_SUFFIXES = (".tmp", ".bak", ".pcm", ".orphan")
_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_OWNER_META = "owner.json"
_MAX_MANIFEST_JSON_BYTES = 8 * 1024 * 1024
_MAX_WALKTHROUGH_FILES = 2000


def _is_within(root: str, path: str) -> bool:
    """True when ``path`` is strictly inside ``root`` (resolved, no traversal)."""
    root_real = os.path.realpath(root)
    path_real = os.path.realpath(path)
    try:
        return os.path.commonpath([root_real, path_real]) == root_real and path_real != root_real
    except ValueError:
        return False


def _is_direct_child(parent: str, child: str) -> bool:
    """True when ``child`` is directly inside ``parent`` with no symlink escape."""
    parent_abs = os.path.abspath(parent)
    child_abs = os.path.abspath(child)
    if os.path.dirname(child_abs) != parent_abs:
        return False
    if os.path.islink(child_abs):
        return False
    return _is_within(parent_abs, child_abs)


def _allowed_filename(filename: str) -> bool:
    """True for a safe walkthrough asset name with an allowed extension."""
    if not filename or filename.startswith(".") or filename in _SKIP_FILES:
        return False
    if filename.endswith(_SKIP_SUFFIXES):
        return False
    _, ext = os.path.splitext(filename)
    return ext.lower() in ALLOWED_EXTENSIONS


def _safe_device_id(device_id: str) -> str:
    if not isinstance(device_id, str) or not _DEVICE_ID.match(device_id):
        raise ValueError("Invalid device id.")
    return device_id


def _read_walkthrough_manifest(folder: str) -> dict:
    """Read and validate ``walkthrough.json`` inside ``folder``."""
    path = os.path.join(folder, MANIFEST_NAME)
    if not _is_direct_child(folder, path) or not os.path.isfile(path):
        raise ValueError("The walkthrough is missing walkthrough.json.")
    if os.path.getsize(path) > _MAX_MANIFEST_JSON_BYTES:
        raise ValueError("walkthrough.json is too large.")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ValueError("walkthrough.json can't be read.") from exc
    if not isinstance(data, dict):
        raise ValueError("walkthrough.json must be an object.")
    return data


def _extract_metadata(folder: str, record_id: str, manifest: dict) -> dict:
    """Lightweight summary metadata for listing and search without loading PNGs."""
    sections = manifest.get("sections")
    if not isinstance(sections, list):
        sections = []
    shots = sum(
        1 for sec in sections
        if isinstance(sec, dict) and isinstance(sec.get("shot"), str) and sec["shot"]
    )
    texts = [
        str(sec.get("text") or "").strip()
        for sec in sections
        if isinstance(sec, dict) and str(sec.get("text") or "").strip()
    ]
    preview = " ".join(texts)[:300]
    written_up = (
        os.path.isfile(os.path.join(folder, "write-up.md"))
        and os.path.isfile(os.path.join(folder, "write-up.pdf"))
    )
    title = ""
    md_candidate = "write-up.md" if written_up else "walkthrough.md"
    md_path = os.path.join(folder, md_candidate)
    if _is_direct_child(folder, md_path) and os.path.isfile(md_path):
        try:
            with open(md_path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped.startswith("# "):
                        title = stripped[2:].strip()[:160]
                        break
                    if stripped:
                        break
        except OSError:
            pass
    started_at = str(manifest.get("started_at") or "")
    raw_duration = manifest.get("duration_s")
    duration_s = (
        float(raw_duration)
        if isinstance(raw_duration, (int, float)) and not isinstance(raw_duration, bool)
        else 0.0
    )
    raw_number = manifest.get("number")
    number = raw_number if type(raw_number) is int and raw_number >= 1 else None
    return {
        "id": record_id,
        "started_at": started_at,
        "timestamp": started_at or record_id,
        "duration_s": duration_s,
        "number": number,
        "written_up": written_up,
        "shot_count": shots,
        "title": title,
        "preview": preview,
    }


def _declared_walkthrough_files(declared) -> dict[str, int]:
    """Validate ``record["files"]`` -> ``{basename: size}``."""
    if not isinstance(declared, dict) or not declared:
        raise ValueError("The walkthrough's file list is missing.")
    if len(declared) > _MAX_WALKTHROUGH_FILES:
        raise ValueError("The walkthrough has too many files.")
    sizes: dict[str, int] = {}
    for raw_name, size in declared.items():
        name = safe_name(raw_name)
        parts = name.split("/")
        if len(parts) != 2 or parts[0] != "files":
            raise ValueError("Walkthrough files must be placed under files/.")
        filename = parts[1]
        if not _allowed_filename(filename):
            raise ValueError(f"Walkthrough file type is not allowed: {filename!r}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError("A walkthrough file's size is invalid.")
        sizes[filename] = size
    if MANIFEST_NAME not in sizes:
        raise ValueError("The walkthrough record must include walkthrough.json.")
    return sizes


def _check_staged_files(
    sizes: dict[str, int], staged: str, kept_folder: str | None
) -> None:
    """Verify every declared file is present in ``staged`` or unchanged in ``kept_folder``."""
    for name, size in sizes.items():
        source = os.path.join(staged, name)
        if os.path.isfile(source) and not os.path.islink(source):
            if os.path.getsize(source) != size:
                raise ValueError(f"The walkthrough's {name} size does not match.")
            continue
        kept = os.path.join(kept_folder, name) if kept_folder else ""
        if (
            not kept
            or os.path.islink(kept)
            or not os.path.isfile(kept)
            or os.path.getsize(kept) != size
        ):
            raise ValueError(f"The walkthrough's {name} is missing.")


class _FolderSwap:
    """Atomically install or update a walkthrough folder, with rollback on failure."""

    def __init__(
        self,
        parent_dir: str,
        record_id: str,
        staged: str,
        dest: str,
        old_folder: str | None,
    ) -> None:
        self.parent_dir = parent_dir
        self.record_id = record_id
        self.staged = staged
        self.dest = dest
        self.old_folder = old_folder
        self.tombstone: str | None = None
        self.placed = False

    def install(self, sizes: dict[str, int], *, update: bool) -> None:
        os.makedirs(self.parent_dir, exist_ok=True)
        if update and self.old_folder and os.path.isdir(self.old_folder):
            for name in sizes:
                source = os.path.join(self.staged, name)
                if os.path.isfile(source) and not os.path.islink(source):
                    target = os.path.join(self.dest, name)
                    if not _is_direct_child(self.dest, target) and os.path.exists(target):
                        raise ValueError(f"Unsafe walkthrough target: {name}")
                    _move(source, target)
            return

        if self.old_folder and os.path.isdir(self.old_folder):
            self.tombstone = os.path.join(
                self.parent_dir, f".deleting-{self.record_id}-{uuid.uuid4().hex}"
            )
            os.replace(self.old_folder, self.tombstone)
        os.makedirs(self.dest, exist_ok=True)
        self.placed = True
        for name in sizes:
            source = os.path.join(self.staged, name)
            if os.path.isfile(source) and not os.path.islink(source):
                _move(source, os.path.join(self.dest, name))

    def prune(self, keep_names: set) -> None:
        if not os.path.isdir(self.dest):
            return
        for entry in list(os.listdir(self.dest)):
            if entry in keep_names:
                continue
            path = os.path.join(self.dest, entry)
            try:
                if os.path.isfile(path) and not os.path.islink(path):
                    os.remove(path)
            except OSError:
                logger.debug("Could not prune stale walkthrough file %s", path, exc_info=True)

    def roll_back(self) -> None:
        if self.placed:
            shutil.rmtree(self.dest, ignore_errors=True)
        if self.tombstone and self.old_folder and not os.path.exists(self.old_folder):
            try:
                os.replace(self.tombstone, self.old_folder)
                self.tombstone = None
            except OSError:
                logger.exception("Could not restore walkthrough folder after failed import")

    def discard_old(self) -> None:
        if self.tombstone:
            shutil.rmtree(self.tombstone, ignore_errors=True)


class WalkthroughRecords:
    """Walkthrough folders: their manifests, rendered documents, and PNG screenshots.

    Implements the ``RecordKind`` protocol (``services/remote_records/host_store.py``)
    and client sync helpers (``exists``, ``local_ids``, ``ready``, ``in_use``,
    ``digest``, ``delete_local``).
    """

    kind = "walkthrough"

    def __init__(
        self,
        captures_root: str | None = None,
        *,
        in_use: Callable[[str], bool] | None = None,
    ) -> None:
        self._root = captures_root
        self.in_use_check = in_use or (lambda _record_id: False)

    @property
    def root(self) -> str:
        if self._root is None:
            from services.walkthrough.storage import captures_root

            return os.path.abspath(str(captures_root()))
        return os.path.abspath(self._root)

    def _devices_root(self) -> str:
        return os.path.join(self.root, "devices")

    def _device_folder(self, device_id: str) -> str:
        safe_dev = _safe_device_id(device_id)
        folder = os.path.abspath(os.path.join(self._devices_root(), safe_dev))
        if not _is_within(self.root, folder):
            raise ValueError("Unsafe walkthrough device folder.")
        return folder

    def _record_folder(self, origin: str | None, record_id: str) -> str:
        rid = safe_record_id(record_id)
        parent = self._device_folder(origin) if origin is not None else self.root
        folder = os.path.abspath(os.path.join(parent, rid))
        if os.path.dirname(folder) != os.path.abspath(parent) or not _is_within(self.root, folder):
            raise ValueError("Unsafe walkthrough folder path.")
        if os.path.islink(folder):
            raise ValueError("Symlinked walkthrough folders are not permitted.")
        return folder

    def _existing_folder(self, origin: str | None, record_id: str) -> str:
        folder = self._record_folder(origin, record_id)
        manifest = os.path.join(folder, MANIFEST_NAME)
        if not os.path.isdir(folder) or not os.path.isfile(manifest) or os.path.islink(manifest):
            raise LookupError("That walkthrough isn't here anymore.")
        return folder

    def _owner_meta_path(self, device_id: str) -> str:
        return os.path.join(self._device_folder(device_id), _OWNER_META)

    def _read_owner_name(self, device_id: str) -> str:
        path = self._owner_meta_path(device_id)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict) and isinstance(data.get("name"), str) and data["name"]:
                return data["name"][:120]
        except (OSError, ValueError):
            pass
        return "Unnamed computer"

    def _write_owner_name(self, device_id: str, name: str | None) -> None:
        if not name:
            return
        folder = self._device_folder(device_id)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, _OWNER_META)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"id": device_id, "name": name[:120]}, handle, ensure_ascii=False)
        os.replace(tmp, path)

    def _iter_device_folders(self, device_id: str) -> builtins.list[tuple[str, str]]:
        """Return ``[(record_id, folder_path), ...]`` stored for ``device_id``."""
        try:
            base = self._device_folder(device_id)
        except ValueError:
            return []
        if not os.path.isdir(base) or os.path.islink(base):
            return []
        found: list[tuple[str, str]] = []
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            return []
        for entry in entries:
            try:
                rid = safe_record_id(entry)
            except ValueError:
                continue
            path = os.path.join(base, rid)
            if (
                os.path.isdir(path)
                and not os.path.islink(path)
                and os.path.isfile(os.path.join(path, MANIFEST_NAME))
                and not os.path.islink(os.path.join(path, MANIFEST_NAME))
            ):
                found.append((rid, path))
        return found

    def _collect_files(self, folder: str) -> dict[str, str]:
        """Collect all allowed, safe files directly inside ``folder``."""
        files: dict[str, str] = {}
        for entry in sorted(os.listdir(folder)):
            if not _allowed_filename(entry):
                continue
            path = os.path.join(folder, entry)
            if not _is_direct_child(folder, path) or not os.path.isfile(path):
                continue
            try:
                key = safe_name(f"files/{entry}")
            except ValueError:
                continue
            files[key] = path
        return files

    # ---- host ownership ----

    def owns(self, device_id: str, record_id: str) -> bool:
        try:
            self._existing_folder(device_id, record_id)
            return True
        except (LookupError, ValueError):
            return False

    def owners(self) -> list:
        base = self._devices_root()
        if not os.path.isdir(base) or os.path.islink(base):
            return []
        result = []
        try:
            device_ids = sorted(os.listdir(base))
        except OSError:
            return []
        for device_id in device_ids:
            if not _DEVICE_ID.match(device_id):
                continue
            count = len(self._iter_device_folders(device_id))
            if count > 0:
                result.append({
                    "id": device_id,
                    "name": self._read_owner_name(device_id),
                    "count": count,
                })
        return result

    # ---- this computer's own ----

    def exists(self, record_id: str) -> bool:
        try:
            self._existing_folder(None, record_id)
            return True
        except (LookupError, ValueError):
            return False

    def local_ids(self) -> builtins.list[str]:
        if not os.path.isdir(self.root) or os.path.islink(self.root):
            return []
        ids: list[str] = []
        try:
            names = sorted(os.listdir(self.root), reverse=True)
        except OSError:
            return []
        for entry in names:
            if entry in ("devices", "screenshots"):
                continue
            try:
                rid = safe_record_id(entry)
            except ValueError:
                continue
            path = os.path.join(self.root, rid)
            if (
                os.path.isdir(path)
                and not os.path.islink(path)
                and os.path.isfile(os.path.join(path, MANIFEST_NAME))
                and not os.path.exists(os.path.join(path, "draft.json"))
            ):
                ids.append(rid)
        return ids

    def ready(self, record_id: str) -> str | None:
        """None when the walkthrough is finished and ready to sync, else why to wait."""
        folder = self._record_folder(None, record_id)
        if not os.path.isdir(folder):
            raise LookupError("That walkthrough isn't here anymore.")
        if os.path.exists(os.path.join(folder, "draft.json")):
            return "it hasn't finished"
        if not os.path.isfile(os.path.join(folder, MANIFEST_NAME)):
            return "it hasn't finished"
        if self.in_use(record_id):
            return "it is currently open"
        return None

    def in_use(self, record_id: str) -> bool:
        return bool(self.in_use_check(record_id))

    def digest(self, record_id: str) -> str:
        """Changes whenever any file in the walkthrough changes (reviews, write-ups)."""
        bundle = self.bundle(None, record_id)
        hashes = {
            name: file_sha256(path)
            for name, path in sorted(bundle.files.items())
        }
        payload = {"record": bundle.record, "sha256": hashes}
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def delete_local(self, record_id: str) -> None:
        folder = self._record_folder(None, record_id)
        if not os.path.isdir(folder):
            return
        if not _is_within(self.root, folder) or os.path.islink(folder):
            raise ValueError("Refusing to delete outside walkthrough root.")
        for entry in os.listdir(folder):
            path = os.path.join(folder, entry)
            if os.path.islink(path) or not os.path.isfile(path):
                raise ValueError("Walkthrough folder contains unexpected entries.")
        shutil.rmtree(folder)

    # ---- both sides ----

    def bundle(self, origin: str | None, record_id: str) -> Bundle:
        folder = self._existing_folder(origin, record_id)
        manifest = _read_walkthrough_manifest(folder)
        files = self._collect_files(folder)
        if f"files/{MANIFEST_NAME}" not in files:
            raise LookupError("Walkthrough manifest is missing.")
        metadata = _extract_metadata(folder, record_id, manifest)
        record = {
            "kind": self.kind,
            "format": FORMAT,
            "update": False,
            "id": record_id,
            "metadata": metadata,
            "files": {name: os.path.getsize(path) for name, path in files.items()},
        }
        return Bundle(record, files)

    def export(self, device_id: str, record_id: str, dest_dir: str) -> dict[str, str]:
        bundle = self.bundle(device_id, record_id)
        with open(os.path.join(dest_dir, MANIFEST), "wb") as handle:
            handle.write(bundle.manifest_bytes())
        return bundle.files

    def stored_files(self, device_id: str, record_id: str) -> dict[str, int]:
        try:
            folder = self._existing_folder(device_id, record_id)
        except (LookupError, ValueError):
            return {}
        files = self._collect_files(folder)
        return {name: os.path.getsize(path) for name, path in files.items()}

    def import_record(
        self,
        record_id: str,
        files_dir: str,
        record: dict,
        device: dict | None,
    ) -> dict:
        rid = safe_record_id(record_id)
        if not isinstance(record, dict) or record.get("kind") != self.kind:
            raise ValueError("Record manifest is not a walkthrough.")
        if record.get("id") != rid:
            raise ValueError("The walkthrough's id doesn't match the record.")
        sizes = _declared_walkthrough_files(record.get("files"))
        update = record.get("update") is True

        origin = device.get("id") if device else None
        if origin is not None:
            origin = _safe_device_id(origin)
        dest = self._record_folder(origin, rid)
        old_folder = dest if os.path.isdir(dest) else None
        if update and old_folder is None:
            raise LookupError("Send the whole walkthrough; this computer doesn't have it yet.")

        staged = os.path.join(files_dir, "files")
        _check_staged_files(sizes, staged, old_folder if update else None)

        # Validate walkthrough.json before touching the destination directory.
        manifest_source_dir = (
            staged
            if os.path.isfile(os.path.join(staged, MANIFEST_NAME))
            else (old_folder or staged)
        )
        _read_walkthrough_manifest(manifest_source_dir)

        parent_dir = self._device_folder(origin) if origin is not None else self.root
        swap = _FolderSwap(parent_dir, rid, staged, dest, old_folder)
        try:
            swap.install(sizes, update=update)
            # Re-verify installed walkthrough.json inside destination.
            _read_walkthrough_manifest(dest)
        except BaseException:
            swap.roll_back()
            raise
        swap.discard_old()
        if update:
            swap.prune(set(sizes))
        if origin is not None:
            self._write_owner_name(origin, _origin_name(device))
        return {"files": len(sizes)}

    def list(self, device_id: str, query: str, limit: int) -> list:
        needle = (query or "").strip().casefold()
        items = []
        for rid, folder in self._iter_device_folders(device_id):
            try:
                manifest = _read_walkthrough_manifest(folder)
            except ValueError:
                continue
            meta = _extract_metadata(folder, rid, manifest)
            files = self._collect_files(folder)
            meta["bytes"] = sum(os.path.getsize(path) for path in files.values())
            meta["file_count"] = len(files)
            if needle:
                haystack = " ".join(
                    (
                        meta["id"],
                        meta["title"],
                        meta["preview"],
                        meta["started_at"],
                    )
                ).casefold()
                if needle not in haystack:
                    continue
            items.append(meta)
        items.sort(key=lambda item: (item.get("timestamp") or "", item.get("id") or ""), reverse=True)
        return items[: max(1, int(limit))]

    def delete(self, device_id: str, record_id: str) -> bool:
        try:
            folder = self._existing_folder(device_id, record_id)
        except (LookupError, ValueError):
            return False
        if not _is_within(self._device_folder(device_id), folder):
            raise ValueError("Refusing to delete outside device walkthrough root.")
        shutil.rmtree(folder)
        return True

    def delete_all(self, device_id: str) -> int:
        folders = self._iter_device_folders(device_id)
        removed = 0
        for rid, _folder in folders:
            removed += bool(self.delete(device_id, rid))
        device_dir = self._device_folder(device_id)
        if os.path.isdir(device_dir) and _is_within(self.root, device_dir):
            shutil.rmtree(device_dir, ignore_errors=True)
        return removed

    def rename_owner(self, device_id: str, name: str) -> int:
        folders = self._iter_device_folders(device_id)
        if not folders:
            return 0
        self._write_owner_name(device_id, (name or "")[:120] or "Unnamed computer")
        return len(folders)

    def summary(self, device_id: str) -> dict:
        folders = self._iter_device_folders(device_id)
        total_bytes = 0
        for _rid, folder in folders:
            for path in self._collect_files(folder).values():
                try:
                    total_bytes += os.path.getsize(path)
                except OSError:
                    pass
        return {"count": len(folders), "bytes": total_bytes}
