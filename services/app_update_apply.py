"""Native update preparation and the crash-safe directory-swap commit.

Stdlib-only (plus ``winreg`` on Windows) so ``OpenWhisperUpdater.exe`` can
import this module. Qt, settings, and GitHub live in ``app_update.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Set, Tuple

from services.update_contract import (
    APP_EXE_NAME,
    APP_ID,
    APP_MUTEX_NAMES,
    APP_NAME,
    ARCHITECTURE,
    CLEANUP_ARG,
    HEALTH_ARG,
    INTERNAL_DIRNAME,
    MANAGED_TOP_LEVEL_DIRS,
    MANAGED_TOP_LEVEL_FILES,
    MANIFEST_NAME,
    MANIFEST_SCHEMA_VERSION,
    MINIMUM_UPDATER_VERSION,
    NVIDIA_RELATIVE,
    PARENT_PID_ARG,
    RECOVER_ARG,
    RUNONCE_VALUE_NAME,
    SENTINEL_NAME,
    SETUP_MUTEX_NAME,
    TOPOLOGY_REVISION,
    TRANSACTION_ARG,
    UNINSTALL_KEY_PATH,
    UPDATE_MUTEX_NAMES,
    UPDATER_EXE_NAME,
    ApplyMode,
    TransactionState,
    apply_error_path,
    health_path,
    is_newer_version,
    journal_path,
    local_app_dir,
    parse_strict_version,
    transaction_dir,
    updates_root,
    validate_transaction_id,
)

ProgressCallback = Callable[[str, int, int], None]

_CHUNK_BYTES = 1 << 20
_MAX_MEMBERS = 50_000
_MAX_DEPTH = 20
_MAX_NAME_LEN = 240
_DEFAULT_UNPACKED_CAP = 2 * 1024 * 1024 * 1024
_SPACE_MARGIN = 1.15
_PARENT_WAIT_S = 120
_HEALTH_WAIT_S = 180
_ERROR_LIMIT = 2000
_PARENT_STILL_RUNNING = (
    "OpenWhisper did not close, so the update was not installed. Your current "
    "version is unchanged — close OpenWhisper completely and try the update "
    "again."
)
_APPLICATION_MUTEX_HANDLES: List[int] = []
_RESERVED_WINDOWS_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
)
_UNSAFE_TAR_TYPES = {
    getattr(tarfile, name)
    for name in (
        "LNKTYPE",
        "SYMTYPE",
        "FIFOTYPE",
        "CHRTYPE",
        "BLKTYPE",
        "CONTTYPE",
    )
    if hasattr(tarfile, name)
}


class UpdateApplyError(Exception):
    """User-facing native-update failure."""


class UpdateCanceled(UpdateApplyError):
    """The user cancelled download or preparation."""


@dataclass(frozen=True)
class InstallRegistration:
    """Inno Setup Add/Remove Programs entry for this product."""

    hive: str
    key_path: str
    install_location: str
    uninstall_string: str
    display_version: str
    estimated_size_kb: Optional[int] = None
    install_date: str = ""
    view: int = 0


@dataclass
class UpdateJournal:
    """On-disk transaction. The helper receives only ``transaction_id``."""

    transaction_id: str
    state: str
    app_dir: str
    candidate_dir: str
    rollback_dir: str
    old_version: str
    new_version: str
    old_display_version: str
    old_estimated_size_kb: Optional[int]
    old_install_date: str
    old_exe_sha256: str
    new_exe_sha256: str
    parent_pid: int
    parent_exe: str
    parent_creation_time: int
    health_token: str
    uninstaller_files: List[str] = field(default_factory=list)
    compatibility_files: Dict[str, Dict[str, object]] = field(default_factory=dict)
    nvidia_preserved: bool = False
    appdata: str = ""
    archive_path: str = ""

    def to_dict(self) -> Dict:
        return {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "state": self.state,
            "app_dir": self.app_dir,
            "candidate_dir": self.candidate_dir,
            "rollback_dir": self.rollback_dir,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "old_display_version": self.old_display_version,
            "old_estimated_size_kb": self.old_estimated_size_kb,
            "old_install_date": self.old_install_date,
            "old_exe_sha256": self.old_exe_sha256,
            "new_exe_sha256": self.new_exe_sha256,
            "parent_pid": self.parent_pid,
            "parent_exe": self.parent_exe,
            "parent_creation_time": self.parent_creation_time,
            "health_token": self.health_token,
            "uninstaller_files": list(self.uninstaller_files),
            "compatibility_files": dict(self.compatibility_files),
            "nvidia_preserved": self.nvidia_preserved,
            "appdata": self.appdata,
            "archive_path": self.archive_path,
        }

    @classmethod
    def from_dict(cls, payload: Dict) -> "UpdateJournal":
        required = (
            "transaction_id",
            "state",
            "app_dir",
            "candidate_dir",
            "rollback_dir",
            "old_version",
            "new_version",
            "old_display_version",
            "old_estimated_size_kb",
            "old_install_date",
            "old_exe_sha256",
            "new_exe_sha256",
            "parent_pid",
            "parent_exe",
            "parent_creation_time",
            "health_token",
            "compatibility_files",
        )
        missing = [key for key in required if key not in payload]
        if missing:
            raise UpdateApplyError(
                "The update journal is incomplete: " + ", ".join(missing)
            )
        transaction_id = validate_transaction_id(str(payload["transaction_id"]))
        state = str(payload["state"])
        if state not in {
            TransactionState.PREPARED,
            TransactionState.OLD_MOVED,
            TransactionState.NEW_ACTIVE,
            TransactionState.HEALTHY,
            TransactionState.ROLLED_BACK,
        }:
            raise UpdateApplyError("The update journal has an invalid state.")
        return cls(
            transaction_id=transaction_id,
            state=state,
            app_dir=str(payload["app_dir"]),
            candidate_dir=str(payload["candidate_dir"]),
            rollback_dir=str(payload["rollback_dir"]),
            old_version=str(payload["old_version"]),
            new_version=str(payload["new_version"]),
            old_display_version=str(payload["old_display_version"]),
            old_estimated_size_kb=(
                int(payload["old_estimated_size_kb"])
                if payload["old_estimated_size_kb"] is not None
                else None
            ),
            old_install_date=str(payload["old_install_date"]),
            old_exe_sha256=str(payload["old_exe_sha256"]),
            new_exe_sha256=str(payload["new_exe_sha256"]),
            parent_pid=int(payload["parent_pid"]),
            parent_exe=str(payload["parent_exe"]),
            parent_creation_time=int(payload["parent_creation_time"]),
            health_token=str(payload["health_token"]),
            uninstaller_files=[
                str(name) for name in (payload.get("uninstaller_files") or [])
            ],
            compatibility_files={
                str(name): dict(metadata)
                for name, metadata in dict(payload["compatibility_files"]).items()
            },
            nvidia_preserved=bool(payload.get("nvidia_preserved")),
            appdata=str(payload.get("appdata") or ""),
            archive_path=str(payload.get("archive_path") or ""),
        )


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_atomic(path: str, payload: Dict) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    _replace_path_write_through(tmp, path, replace_existing=True)


def _fsync_directory(path: str) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _replace_path_write_through(
    source: str, destination: str, *, replace_existing: bool
) -> None:
    if sys.platform == "win32":
        import ctypes

        flags = 0x8  # MOVEFILE_WRITE_THROUGH
        if replace_existing:
            flags |= 0x1  # MOVEFILE_REPLACE_EXISTING
        if not _kernel32().MoveFileExW(source, destination, flags):
            raise ctypes.WinError(ctypes.get_last_error())
        return
    if replace_existing:
        os.replace(source, destination)
    else:
        os.rename(source, destination)
    _fsync_directory(os.path.dirname(os.path.abspath(destination)))


def canonical_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path))).rstrip("\\/")


def paths_equal(left: str, right: str) -> bool:
    return canonical_path(left) == canonical_path(right)


def has_reparse_point(path: str) -> bool:
    if not os.path.lexists(path):
        return False
    if os.path.islink(path):
        return True
    if os.name != "nt":
        return False
    try:
        attrs = os.lstat(path).st_file_attributes  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def reject_reparse(path: str, label: str) -> None:
    if has_reparse_point(path):
        raise UpdateApplyError(f"{label} is a reparse point and cannot be used.")


def reject_reparse_chain(path: str, label: str) -> None:
    """Reject a reparse point anywhere from an existing path to its root."""
    current = os.path.abspath(path)
    while current and not os.path.lexists(current):
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent
    while current:
        reject_reparse(current, label)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent


def _is_reserved_windows_name(segment: str) -> bool:
    stem = segment.split(".", 1)[0].upper()
    return stem in _RESERVED_WINDOWS_NAMES


def validate_archive_member_name(name: str) -> str:
    """Return a normalized relative member path, or raise."""
    if "\\" in name:
        raise UpdateApplyError(f"Archive contains an unsafe path: {name}")
    raw = name
    if not raw or raw.endswith("/"):
        directory = raw.rstrip("/")
        if not directory:
            raise UpdateApplyError("Archive contains an empty path.")
        raw = directory
    if raw.startswith("/") or raw.startswith("\\"):
        raise UpdateApplyError(f"Archive contains an unsafe path: {name}")
    if re.match(r"^[A-Za-z]:", raw) or raw.startswith("//"):
        raise UpdateApplyError(f"Archive contains an unsafe path: {name}")
    if ":" in raw:
        raise UpdateApplyError(f"Archive contains an unsafe path: {name}")
    if len(raw) > _MAX_NAME_LEN:
        raise UpdateApplyError(f"Archive path is too long: {name}")
    parts = raw.split("/")
    if len(parts) > _MAX_DEPTH:
        raise UpdateApplyError(f"Archive path is too deep: {name}")
    for part in parts:
        if part in ("", ".", ".."):
            raise UpdateApplyError(f"Archive contains an unsafe path: {name}")
        if part.endswith(" ") or part.endswith("."):
            raise UpdateApplyError(f"Archive contains an unsafe path: {name}")
        if _is_reserved_windows_name(part):
            raise UpdateApplyError(f"Archive contains a reserved name: {name}")
        if any(ord(char) < 32 for char in part):
            raise UpdateApplyError(f"Archive contains an unsafe path: {name}")
    return "/".join(parts)


def _safe_join(root: str, relative: str) -> str:
    target = os.path.abspath(os.path.join(root, *relative.split("/")))
    root_abs = os.path.abspath(root)
    if canonical_path(target) != canonical_path(root_abs) and not canonical_path(
        target
    ).startswith(canonical_path(root_abs) + os.sep):
        raise UpdateApplyError(f"Archive escaped the extract directory: {relative}")
    return target


def iter_managed_files(root: str) -> List[str]:
    """Relative POSIX paths of regular files under a prepared tree."""
    files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        reject_reparse(dirpath, dirpath)
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        dirnames[:] = sorted(dirnames)
        for name in dirnames:
            reject_reparse(os.path.join(dirpath, name), os.path.join(dirpath, name))
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            reject_reparse(full, full)
            if not os.path.isfile(full) or os.path.islink(full):
                raise UpdateApplyError(f"Refusing a non-regular file: {full}")
            relative = name if not rel_dir else os.path.join(rel_dir, name)
            files.append(relative.replace("\\", "/"))
    return files


def validate_managed_topology(paths: Set[str]) -> None:
    """Require the fixed onedir roots that native updates may replace."""
    allowed_files = set(MANAGED_TOP_LEVEL_FILES)
    allowed_dirs = set(MANAGED_TOP_LEVEL_DIRS)
    for relative in paths:
        normalized = validate_archive_member_name(relative)
        parts = normalized.split("/")
        if len(parts) == 1:
            if normalized not in allowed_files:
                raise UpdateApplyError(
                    f"Native update contains an unmanaged top-level file: {relative}"
                )
            continue
        if parts[0] not in allowed_dirs:
            raise UpdateApplyError(
                f"Native update contains an unmanaged top-level path: {relative}"
            )


def build_update_manifest(
    dist_dir: str,
    version: str,
    *,
    unpacked_bytes: Optional[int] = None,
) -> Dict:
    """Build the per-file manifest for a frozen onedir."""
    files: Dict[str, Dict[str, object]] = {}
    total = 0
    skip = {MANIFEST_NAME, SENTINEL_NAME}
    for relative in iter_managed_files(dist_dir):
        if relative in skip:
            continue
        full = os.path.join(dist_dir, *relative.split("/"))
        size = os.path.getsize(full)
        total += size
        files[relative] = {"size": size, "sha256": file_sha256(full)}
    validate_managed_topology(set(files) | {MANIFEST_NAME})
    for required in (APP_EXE_NAME, UPDATER_EXE_NAME):
        if required not in files:
            raise UpdateApplyError(f"Bundle is missing required file: {required}")
    if not os.path.isdir(os.path.join(dist_dir, INTERNAL_DIRNAME)):
        raise UpdateApplyError("Bundle is missing the _internal directory.")
    parse_strict_version(version)
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "product": APP_NAME,
        "app_id": APP_ID,
        "version": version,
        "architecture": ARCHITECTURE,
        "topology_revision": TOPOLOGY_REVISION,
        "minimum_updater_version": MINIMUM_UPDATER_VERSION,
        "unpacked_bytes": unpacked_bytes if unpacked_bytes is not None else total,
        "member_count": len(files) + 1,
        "files": files,
    }


def load_manifest(path: str) -> Dict:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise UpdateApplyError("The update manifest is not an object.")
    return payload


def validate_manifest(
    manifest: Dict,
    *,
    expected_version: Optional[str] = None,
    current_updater_version: Optional[str] = None,
) -> None:
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise UpdateApplyError("This update uses an unsupported manifest schema.")
    if manifest.get("product") != APP_NAME or manifest.get("app_id") != APP_ID:
        raise UpdateApplyError("The update package is not for OpenWhisper.")
    if manifest.get("architecture") != ARCHITECTURE:
        raise UpdateApplyError("The update package is not for this architecture.")
    if manifest.get("topology_revision") != TOPOLOGY_REVISION:
        raise UpdateApplyError(
            "This update changes the install layout and must use the installer."
        )
    version = str(manifest.get("version") or "")
    parse_strict_version(version)
    if expected_version and version != expected_version:
        raise UpdateApplyError("The update package version does not match the release.")
    minimum = str(manifest.get("minimum_updater_version") or MINIMUM_UPDATER_VERSION)
    if current_updater_version and parse_strict_version(
        minimum
    ) > parse_strict_version(current_updater_version):
        raise UpdateApplyError(
            "This update needs a newer updater and must use the installer."
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise UpdateApplyError("The update manifest lists no files.")
    declared_total = manifest.get("unpacked_bytes")
    member_count = manifest.get("member_count")
    if (
        not isinstance(declared_total, int)
        or declared_total < 0
        or declared_total > _DEFAULT_UNPACKED_CAP
        or not isinstance(member_count, int)
        or member_count != len(files) + 1
        or member_count > _MAX_MEMBERS
    ):
        raise UpdateApplyError("The update manifest has invalid size metadata.")
    calculated_total = 0
    for relative, metadata in files.items():
        if (
            not isinstance(relative, str)
            or not isinstance(metadata, dict)
            or not isinstance(metadata.get("size"), int)
            or metadata["size"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("sha256") or ""))
        ):
            raise UpdateApplyError("The update manifest has an invalid file entry.")
        calculated_total += metadata["size"]
    if calculated_total != declared_total:
        raise UpdateApplyError("The update manifest size does not match its files.")
    for required in (APP_EXE_NAME, UPDATER_EXE_NAME):
        if required not in files:
            raise UpdateApplyError(f"The update package is missing {required}.")
    validate_managed_topology(set(str(path) for path in files) | {MANIFEST_NAME})


def verify_tree_against_manifest(
    root: str,
    manifest: Dict,
    *,
    allowed_extra_files: Optional[Set[str]] = None,
    allowed_extra_prefixes: Optional[Tuple[str, ...]] = None,
) -> None:
    validate_manifest(manifest)
    files = manifest["files"]
    ignorable = {MANIFEST_NAME, SENTINEL_NAME}
    present = set(iter_managed_files(root)) - ignorable
    expected = set(files)
    allowed = set(allowed_extra_files or ())
    prefixes = tuple(
        prefix.rstrip("/") + "/" for prefix in (allowed_extra_prefixes or ())
    )
    extra = sorted(
        path
        for path in present - expected
        if path not in allowed and not path.startswith(prefixes)
    )
    missing = sorted(expected - present)
    if extra or missing:
        raise UpdateApplyError(
            "The extracted update does not match its manifest."
        )
    for relative, meta in files.items():
        full = os.path.join(root, *str(relative).split("/"))
        size = int(meta.get("size") or 0)
        digest = str(meta.get("sha256") or "")
        if os.path.getsize(full) != size or file_sha256(full) != digest:
            raise UpdateApplyError(
                f"Extracted file failed verification: {relative}"
            )


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise UpdateCanceled("The update was cancelled.")


def read_manifest_from_tar_xz(archive_path: str) -> Dict:
    """Read the bounded root manifest without enumerating the whole archive."""
    found: Optional[Dict] = None
    with tarfile.open(archive_path, "r:xz") as archive:
        for count, member in enumerate(archive, start=1):
            if count > _MAX_MEMBERS:
                raise UpdateApplyError("The update archive has too many entries.")
            name = validate_archive_member_name(member.name)
            if name != MANIFEST_NAME:
                continue
            if found is not None or not member.isreg() or member.size > (4 << 20):
                raise UpdateApplyError("The update archive has an invalid manifest.")
            source = archive.extractfile(member)
            if source is None:
                raise UpdateApplyError("The update manifest could not be read.")
            try:
                payload = json.loads(source.read(member.size + 1).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise UpdateApplyError("The update manifest could not be read.") from exc
            if not isinstance(payload, dict):
                raise UpdateApplyError("The update manifest is not an object.")
            found = payload
    if found is None:
        raise UpdateApplyError("The update package is missing its manifest.")
    return found


def safe_extract_tar_xz(
    archive_path: str,
    destination: str,
    *,
    manifest: Optional[Dict] = None,
    progress: Optional[ProgressCallback] = None,
    cancel=None,
) -> None:
    """Extract only regular files/directories from a tar.xz into ``destination``."""
    if progress is None:
        progress = lambda _phase, _done, _total: None
    unpacked_cap = _DEFAULT_UNPACKED_CAP
    if manifest is not None:
        unpacked_cap = int(manifest.get("unpacked_bytes") or unpacked_cap)
        # The root manifest is not one of its own managed files.
        unpacked_cap += 4 << 20

    os.makedirs(destination, exist_ok=True)
    reject_reparse(destination, "Extract directory")
    seen_fold: Set[str] = set()
    written = 0
    total_members = int(manifest.get("member_count") or 0) if manifest else 0
    manifest_files = manifest.get("files") if manifest else None
    index = 0

    with tarfile.open(archive_path, "r:xz") as archive:
        for index, member in enumerate(archive, start=1):
            if index > _MAX_MEMBERS:
                raise UpdateApplyError("The update archive has too many entries.")
            _check_cancel(cancel)
            if member.isdir():
                name = validate_archive_member_name(member.name)
                fold = name.lower()
                if fold in seen_fold:
                    raise UpdateApplyError(f"Archive contains a duplicate path: {name}")
                seen_fold.add(fold)
                target = _safe_join(destination, name)
                os.makedirs(target, exist_ok=True)
                progress("extracting", index, total_members)
                continue
            if member.issym() or member.islnk() or member.type in _UNSAFE_TAR_TYPES:
                raise UpdateApplyError(
                    f"Archive contains an unsupported entry: {member.name}"
                )
            if not member.isreg():
                # Pax/global headers are consumed by tarfile; anything else is unsafe.
                if member.type in (b"x", b"g", "x", "g"):
                    continue
                raise UpdateApplyError(
                    f"Archive contains an unsupported entry: {member.name}"
                )
            name = validate_archive_member_name(member.name)
            fold = name.lower()
            if fold in seen_fold:
                raise UpdateApplyError(f"Archive contains a duplicate path: {name}")
            seen_fold.add(fold)
            if member.size < 0 or written + member.size > unpacked_cap:
                raise UpdateApplyError("The update archive is larger than allowed.")
            if manifest_files is not None and name != MANIFEST_NAME:
                metadata = manifest_files.get(name)
                if (
                    not isinstance(metadata, dict)
                    or metadata.get("size") != member.size
                ):
                    raise UpdateApplyError(
                        f"Archive member is not authorized by the manifest: {name}"
                    )
            target = _safe_join(destination, name)
            parent = os.path.dirname(target)
            os.makedirs(parent, exist_ok=True)
            reject_reparse(parent, parent)
            if os.path.lexists(target):
                raise UpdateApplyError(f"Archive would overwrite {name}")
            source = archive.extractfile(member)
            if source is None:
                raise UpdateApplyError(f"Could not read archive member: {name}")
            with source, open(target, "wb") as out:
                remaining = member.size
                while remaining:
                    _check_cancel(cancel)
                    block = source.read(min(_CHUNK_BYTES, remaining))
                    if not block:
                        raise UpdateApplyError(
                            f"Archive member ended early: {name}"
                        )
                    out.write(block)
                    remaining -= len(block)
                out.flush()
                os.fsync(out.fileno())
            written += member.size
            progress("extracting", index, total_members)
    if total_members and index != total_members:
        raise UpdateApplyError("The update archive member count is incorrect.")


def tree_size_bytes(root: str) -> int:
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            full = os.path.join(dirpath, name)
            try:
                total += os.path.getsize(full)
            except OSError:
                continue
    return total


def check_free_space(path: str, required_bytes: int) -> None:
    if required_bytes <= 0:
        return
    os.makedirs(path, exist_ok=True)
    free = shutil.disk_usage(path).free
    needed = int(required_bytes * _SPACE_MARGIN)
    if free < needed:
        raise UpdateApplyError(
            f"Not enough disk space: {needed} bytes needed, {free} free."
        )


def _winreg():
    if sys.platform != "win32":
        raise UpdateApplyError("Native updates are only available on Windows.")
    import winreg

    return winreg


def _registry_views() -> List[int]:
    winreg = _winreg()
    views = [0]
    for name in ("KEY_WOW64_64KEY", "KEY_WOW64_32KEY"):
        value = getattr(winreg, name, 0)
        if value and value not in views:
            views.append(value)
    return views


def _read_uninstall_key(hive, view: int) -> Optional[InstallRegistration]:
    winreg = _winreg()
    access = winreg.KEY_READ | view
    try:
        handle = winreg.OpenKey(hive, UNINSTALL_KEY_PATH, 0, access)
    except OSError:
        return None
    try:
        def _value(name: str, default: str = "") -> str:
            try:
                value, _typ = winreg.QueryValueEx(handle, name)
            except OSError:
                return default
            return str(value)

        location = _value("InstallLocation")
        uninstall = _value("UninstallString")
        version = _value("DisplayVersion")
        size_raw = _value("EstimatedSize")
        install_date = _value("InstallDate")
        size_kb: Optional[int]
        try:
            size_kb = int(size_raw) if size_raw else None
        except ValueError:
            size_kb = None
        hive_name = "HKCU" if hive == winreg.HKEY_CURRENT_USER else "HKLM"
        if not location or not uninstall:
            return None
        return InstallRegistration(
            hive=hive_name,
            key_path=UNINSTALL_KEY_PATH,
            install_location=os.path.normpath(location),
            uninstall_string=uninstall,
            display_version=version,
            estimated_size_kb=size_kb,
            install_date=install_date,
            view=view,
        )
    finally:
        winreg.CloseKey(handle)


def discover_install_registration() -> Optional[InstallRegistration]:
    """Find the Inno ARP entry. HKCU is preferred; HKLM is setup-only."""
    if sys.platform != "win32":
        return None
    winreg = _winreg()
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in _registry_views():
            found = _read_uninstall_key(hive, view)
            if found is not None:
                return found
    return None


def running_app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(sys.argv[0] or os.getcwd()))


def _registered_uninstaller_command_path(
    registration: InstallRegistration,
) -> str:
    command = registration.uninstall_string.strip()
    lowered = command.lower()
    marker = lowered.find(".exe")
    return (
        command[: marker + 4].strip().strip('"')
        if marker >= 0
        else command.strip('"')
    )


def uninstaller_basenames(registration: InstallRegistration) -> List[str]:
    """Companion Inno files next to the registered uninstall exe."""
    exe = _registered_uninstaller_command_path(registration)
    base = os.path.basename(exe)
    stem, ext = os.path.splitext(base)
    if not stem.lower().startswith("unins"):
        return []
    names = [base]
    for suffix in (".dat", ".msg"):
        names.append(stem + suffix)
    return names


def registered_uninstaller_path(
    registration: InstallRegistration, app_dir: str
) -> Optional[str]:
    names = uninstaller_basenames(registration)
    if not names:
        return None
    candidate = os.path.join(app_dir, names[0])
    registered = _registered_uninstaller_command_path(registration)
    if not os.path.isabs(registered) or not paths_equal(registered, candidate):
        return None
    return candidate if os.path.isfile(candidate) and not has_reparse_point(candidate) else None


def _is_under_program_files(path: str) -> bool:
    target = canonical_path(path)
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        root = os.environ.get(variable)
        if root:
            canonical = canonical_path(root)
            if target == canonical or target.startswith(canonical + os.sep):
                return True
    return False


def native_apply_eligible(
    *,
    registration: Optional[InstallRegistration] = None,
    app_dir: Optional[str] = None,
    helper_present: Optional[bool] = None,
) -> bool:
    """True only for a validated, non-elevated HKCU Inno install."""
    if sys.platform != "win32" and registration is None:
        return False
    if registration is None:
        registration = discover_install_registration()
    if registration is None or registration.hive != "HKCU":
        return False
    try:
        parse_strict_version(registration.display_version)
    except ValueError:
        return False
    if is_process_elevated():
        return False
    target = app_dir if app_dir is not None else running_app_dir()
    if _is_under_program_files(target):
        return False
    if not paths_equal(registration.install_location, target):
        return False
    try:
        reject_reparse_chain(target, "Install directory")
    except (OSError, UpdateApplyError):
        return False
    if registered_uninstaller_path(registration, target) is None:
        return False
    if helper_present is None:
        helper_present = os.path.isfile(os.path.join(target, UPDATER_EXE_NAME))
    return bool(helper_present)


def resolve_apply_mode(
    *,
    frozen: bool,
    platform_name: str,
    native_ready: bool,
    setup_ready: bool,
    registration: Optional[InstallRegistration] = None,
    app_dir: Optional[str] = None,
    helper_present: Optional[bool] = None,
) -> str:
    if not frozen or platform_name != "win32":
        return ApplyMode.NOTIFY_ONLY
    if native_ready and native_apply_eligible(
        registration=registration,
        app_dir=app_dir,
        helper_present=helper_present,
    ):
        return ApplyMode.NATIVE
    if setup_ready:
        return ApplyMode.SETUP
    return ApplyMode.NOTIFY_ONLY


def save_journal(journal: UpdateJournal) -> None:
    validate_journal_binding(journal, journal.appdata or None)
    reject_reparse_chain(
        transaction_dir(journal.transaction_id, journal.appdata or None),
        "Update transaction",
    )
    write_json_atomic(
        journal_path(journal.transaction_id, journal.appdata or None),
        journal.to_dict(),
    )


def validate_journal_binding(
    journal: UpdateJournal, appdata: Optional[str] = None
) -> None:
    token = validate_transaction_id(journal.transaction_id)
    expected_appdata = canonical_path(appdata if appdata is not None else local_app_dir())
    if canonical_path(journal.appdata) != expected_appdata:
        raise UpdateApplyError("The update journal has an invalid data root.")
    app_dir = canonical_path(journal.app_dir)
    expected_candidate = canonical_path(
        app_dir.rstrip("\\/") + f".new-{token[:8]}"
    )
    expected_rollback = canonical_path(
        app_dir.rstrip("\\/") + f".old-{token[:8]}"
    )
    if canonical_path(journal.candidate_dir) != expected_candidate:
        raise UpdateApplyError("The update journal has an invalid candidate path.")
    if canonical_path(journal.rollback_dir) != expected_rollback:
        raise UpdateApplyError("The update journal has an invalid rollback path.")
    if not re.fullmatch(r"[0-9a-f]{32}", journal.health_token):
        raise UpdateApplyError("The update journal has an invalid health token.")
    parse_strict_version(journal.old_version)
    parse_strict_version(journal.new_version)
    parse_strict_version(journal.old_display_version)
    if not is_newer_version(journal.old_version, journal.new_version):
        raise UpdateApplyError("The update journal has an invalid version transition.")
    if (
        journal.old_estimated_size_kb is not None
        and journal.old_estimated_size_kb < 0
    ):
        raise UpdateApplyError("The update journal has an invalid installed size.")
    for digest in (journal.old_exe_sha256, journal.new_exe_sha256):
        if digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise UpdateApplyError("The update journal has an invalid file digest.")
    for name in journal.uninstaller_files:
        if (
            os.path.basename(name) != name
            or not re.fullmatch(r"unins\d+\.(?:exe|dat|msg)", name, re.IGNORECASE)
        ):
            raise UpdateApplyError(
                "The update journal has an invalid compatibility file."
            )
    nvidia_prefix = NVIDIA_RELATIVE.replace("\\", "/").rstrip("/") + "/"
    for relative, metadata in journal.compatibility_files.items():
        normalized = validate_archive_member_name(relative)
        if normalized not in journal.uninstaller_files and not normalized.startswith(
            nvidia_prefix
        ):
            raise UpdateApplyError(
                "The update journal has an unauthorized compatibility file."
            )
        if (
            not isinstance(metadata, dict)
            or not isinstance(metadata.get("size"), int)
            or metadata["size"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("sha256") or ""))
        ):
            raise UpdateApplyError(
                "The update journal has invalid compatibility metadata."
            )
    if journal.archive_path:
        archive = canonical_path(journal.archive_path)
        if canonical_path(os.path.dirname(archive)) != canonical_path(
            updates_root(expected_appdata)
        ):
            raise UpdateApplyError("The update journal has an invalid archive path.")


def load_journal(
    transaction_id: str, appdata: Optional[str] = None
) -> UpdateJournal:
    token = validate_transaction_id(transaction_id)
    tx_dir = transaction_dir(token, appdata)
    reject_reparse_chain(tx_dir, "Update transaction")
    path = journal_path(token, appdata)
    if not os.path.isfile(path):
        raise UpdateApplyError("The update transaction was not found.")
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise UpdateApplyError("The update journal is not an object.")
    try:
        journal = UpdateJournal.from_dict(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateApplyError("The update journal is invalid.") from exc
    if journal.transaction_id != token:
        raise UpdateApplyError("The update journal id does not match its directory.")
    validate_journal_binding(journal, appdata)
    return journal


def write_apply_error(message: str, appdata: Optional[str] = None) -> None:
    text = (message or "The update failed.").strip()[:_ERROR_LIMIT]
    path = apply_error_path(appdata)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text + "\n")


def consume_apply_error(appdata: Optional[str] = None) -> Optional[str]:
    path = apply_error_path(appdata)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read(_ERROR_LIMIT + 1)
    except OSError:
        return None
    try:
        os.unlink(path)
    except OSError:
        pass
    text = text.strip()
    if not text:
        return None
    return text[:_ERROR_LIMIT]


def _copy_preserve(src_root: str, dest_root: str, relative: str) -> None:
    source = os.path.join(src_root, relative)
    if not os.path.lexists(source):
        return
    reject_reparse(source, source)
    destination = os.path.join(dest_root, relative)
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    if os.path.isdir(source):
        if os.path.exists(destination):
            return
        shutil.copytree(source, destination, symlinks=False, dirs_exist_ok=False)
        return
    if os.path.isfile(source):
        shutil.copy2(source, destination)


def preserve_compat_files(
    app_dir: str,
    candidate_dir: str,
    registration: InstallRegistration,
) -> Tuple[List[str], bool]:
    """Copy Inno uninstall files and a leftover NVIDIA tree into the candidate."""
    preserved: List[str] = []
    for name in uninstaller_basenames(registration):
        source = os.path.join(app_dir, name)
        if os.path.isfile(source) and not has_reparse_point(source):
            shutil.copy2(source, os.path.join(candidate_dir, name))
            preserved.append(name)
    nvidia_src = os.path.join(app_dir, NVIDIA_RELATIVE)
    nvidia_dst = os.path.join(candidate_dir, NVIDIA_RELATIVE)
    copied_nvidia = False
    if os.path.isdir(nvidia_src) and not os.path.exists(nvidia_dst):
        reject_reparse(nvidia_src, "Legacy NVIDIA directory")
        shutil.copytree(nvidia_src, nvidia_dst, symlinks=False)
        copied_nvidia = True
    return preserved, copied_nvidia


def build_compatibility_file_manifest(
    candidate_dir: str, manifest: Dict
) -> Dict[str, Dict[str, object]]:
    """Hash every preserved file not covered by the release manifest."""
    release_files = set(manifest["files"])
    skip = release_files | {MANIFEST_NAME, SENTINEL_NAME}
    result: Dict[str, Dict[str, object]] = {}
    for relative in iter_managed_files(candidate_dir):
        if relative in skip:
            continue
        full = os.path.join(candidate_dir, *relative.split("/"))
        result[relative] = {
            "size": os.path.getsize(full),
            "sha256": file_sha256(full),
        }
    return result


def write_completion_sentinel(candidate_dir: str, version: str) -> None:
    path = os.path.join(candidate_dir, SENTINEL_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(version)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_tree(root: str) -> None:
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            with open(os.path.join(dirpath, name), "rb+") as handle:
                os.fsync(handle.fileno())


def prepare_candidate(
    archive_path: str,
    *,
    release_version: str,
    current_version: str,
    app_dir: str,
    registration: InstallRegistration,
    progress: Optional[ProgressCallback] = None,
    cancel=None,
    appdata: Optional[str] = None,
    parent_pid: Optional[int] = None,
) -> UpdateJournal:
    """Extract and verify a sibling candidate, then write the journal."""
    if progress is None:
        progress = lambda _phase, _done, _total: None
    parse_strict_version(release_version)
    parse_strict_version(current_version)
    if not is_newer_version(current_version, release_version):
        raise UpdateApplyError("Refusing to apply a version that is not newer.")
    if not native_apply_eligible(registration=registration, app_dir=app_dir):
        raise UpdateApplyError("This installation cannot apply a native update.")
    if registration.display_version != current_version:
        raise UpdateApplyError("The registered version does not match this application.")

    appdata_root = canonical_path(appdata if appdata is not None else local_app_dir())
    transaction_id = uuid.uuid4().hex
    candidate_dir = app_dir.rstrip("\\/") + f".new-{transaction_id[:8]}"
    rollback_dir = app_dir.rstrip("\\/") + f".old-{transaction_id[:8]}"
    if os.path.lexists(candidate_dir) or os.path.lexists(rollback_dir):
        raise UpdateApplyError("A previous update tree is still on disk.")
    tx_dir = transaction_dir(transaction_id, appdata_root)
    os.makedirs(os.path.dirname(tx_dir), exist_ok=True)
    reject_reparse_chain(os.path.dirname(tx_dir), "Update transaction root")
    os.makedirs(tx_dir, exist_ok=False)

    try:
        app_size = tree_size_bytes(app_dir)
        archive_size = os.path.getsize(archive_path)
        if canonical_path(os.path.dirname(archive_path)) != canonical_path(
            updates_root(appdata_root)
        ):
            raise UpdateApplyError("The update archive is outside the update cache.")
        manifest = read_manifest_from_tar_xz(archive_path)
        validate_manifest(
            manifest,
            expected_version=release_version,
            current_updater_version=current_version,
        )
        unpacked_size = int(manifest.get("unpacked_bytes") or 0)
        check_free_space(os.path.dirname(app_dir), app_size + unpacked_size)
        check_free_space(tx_dir, archive_size)
        reject_reparse_chain(app_dir, "Install directory")
        reject_reparse_chain(os.path.dirname(app_dir), "Install parent")
    except Exception:
        shutil.rmtree(tx_dir, ignore_errors=True)
        raise

    try:
        os.makedirs(candidate_dir, exist_ok=False)
        safe_extract_tar_xz(
            archive_path,
            candidate_dir,
            manifest=manifest,
            progress=progress,
            cancel=cancel,
        )
        manifest_path = os.path.join(candidate_dir, MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            raise UpdateApplyError("The update package is missing its manifest.")
        extracted_manifest = load_manifest(manifest_path)
        if extracted_manifest != manifest:
            raise UpdateApplyError("The extracted update manifest changed.")
        verify_tree_against_manifest(candidate_dir, manifest)
        # Hashing and fsyncing the whole tree takes far longer than the
        # extraction that precedes it, so a cancel arriving here has to be
        # honored: otherwise the app hands off to the updater and restarts
        # into a new version the user just declined.
        _check_cancel(cancel)
        preserved, nvidia = preserve_compat_files(
            app_dir, candidate_dir, registration
        )
        compatibility_files = build_compatibility_file_manifest(
            candidate_dir, manifest
        )
        _check_cancel(cancel)
        _fsync_tree(candidate_dir)
        write_completion_sentinel(candidate_dir, release_version)
        helper_src = os.path.join(app_dir, UPDATER_EXE_NAME)
        if not os.path.isfile(helper_src):
            raise UpdateApplyError("The updater helper is missing from this install.")
        helper_copy = os.path.join(tx_dir, UPDATER_EXE_NAME)
        shutil.copy2(helper_src, helper_copy)
        with open(helper_copy, "rb+") as handle:
            os.fsync(handle.fileno())
        old_exe = os.path.join(app_dir, APP_EXE_NAME)
        if not os.path.isfile(old_exe):
            raise UpdateApplyError("The installed executable is missing.")
        new_exe = os.path.join(candidate_dir, APP_EXE_NAME)
        selected_parent_pid = int(
            parent_pid if parent_pid is not None else os.getpid()
        )
        parent_exe, parent_creation_time = process_identity(selected_parent_pid)
        if sys.platform == "win32" and (
            not parent_exe or not parent_creation_time
        ):
            raise UpdateApplyError("Could not identify the running OpenWhisper process.")
        journal = UpdateJournal(
            transaction_id=transaction_id,
            state=TransactionState.PREPARED,
            app_dir=canonical_path(app_dir),
            candidate_dir=canonical_path(candidate_dir),
            rollback_dir=canonical_path(rollback_dir),
            old_version=current_version,
            new_version=release_version,
            old_display_version=registration.display_version,
            old_estimated_size_kb=registration.estimated_size_kb,
            old_install_date=registration.install_date,
            old_exe_sha256=file_sha256(old_exe),
            new_exe_sha256=file_sha256(new_exe),
            parent_pid=selected_parent_pid,
            parent_exe=parent_exe,
            parent_creation_time=parent_creation_time,
            health_token=uuid.uuid4().hex,
            uninstaller_files=preserved,
            compatibility_files=compatibility_files,
            nvidia_preserved=nvidia,
            appdata=appdata_root,
            archive_path=archive_path,
        )
        _check_cancel(cancel)
        save_journal(journal)
        return journal
    except Exception:
        if os.path.isdir(candidate_dir):
            shutil.rmtree(candidate_dir, ignore_errors=True)
        shutil.rmtree(tx_dir, ignore_errors=True)
        raise


def helper_exe_for(journal: UpdateJournal) -> str:
    return os.path.join(
        transaction_dir(journal.transaction_id, journal.appdata or None),
        UPDATER_EXE_NAME,
    )


def helper_argv(journal: UpdateJournal, *, recover: bool = False) -> List[str]:
    args = [TRANSACTION_ARG, journal.transaction_id]
    if recover:
        args.append(RECOVER_ARG)
    return args


def parse_parent_pid(argv: Optional[List[str]] = None) -> Optional[int]:
    args = list(sys.argv[1:] if argv is None else argv)
    if PARENT_PID_ARG not in args:
        return None
    index = args.index(PARENT_PID_ARG)
    if index + 1 >= len(args):
        return None
    try:
        pid = int(args[index + 1])
    except ValueError:
        return None
    return pid if pid > 0 else None


def cleanup_transaction_after_parent(
    transaction_id: str,
    parent_pid: int,
    appdata: Optional[str] = None,
) -> None:
    """Delete a terminal transaction after the updater executable unlocks."""
    journal = load_journal(transaction_id, appdata)
    if journal.state not in {
        TransactionState.HEALTHY,
        TransactionState.ROLLED_BACK,
    }:
        raise UpdateApplyError("Refusing to clean a non-terminal transaction.")
    _wait_for_pid(parent_pid, 120.0)
    shutil.rmtree(
        transaction_dir(journal.transaction_id, journal.appdata or None)
    )


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.OpenMutexW.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel32.ReleaseMutex.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.MoveFileExW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    kernel32.MoveFileExW.restype = wintypes.BOOL
    return kernel32


def _handle_value(handle) -> Optional[int]:
    if not handle:
        return None
    value = getattr(handle, "value", handle)
    return int(value) if value else None


def create_named_mutex(name: str) -> Optional[int]:
    """Create ``name`` and return a handle if this process owns a new mutex."""
    if sys.platform != "win32":
        return 1
    kernel32 = _kernel32()
    handle = kernel32.CreateMutexW(None, True, name)
    if not handle:
        return None
    import ctypes

    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return None
    return _handle_value(handle)


def acquire_named_mutex(name: str, timeout_s: float) -> Optional[int]:
    """Acquire a newly-created named mutex, waiting for its owner to exit."""
    deadline = time.time() + timeout_s
    while True:
        handle = create_named_mutex(name)
        if handle is not None:
            return handle
        if time.time() >= deadline:
            return None
        time.sleep(0.1)


def acquire_named_mutexes(
    names: Tuple[str, ...], timeout_s: float
) -> Optional[List[int]]:
    deadline = time.time() + timeout_s
    handles: List[int] = []
    for name in names:
        remaining = max(0.0, deadline - time.time())
        handle = acquire_named_mutex(name, remaining)
        if handle is None:
            for acquired in reversed(handles):
                release_mutex(acquired)
            return None
        handles.append(handle)
    return handles


def release_mutexes(handles: Optional[List[int]]) -> None:
    for handle in reversed(handles or []):
        release_mutex(handle)


def mutex_exists(name: str) -> bool:
    if sys.platform != "win32":
        return False
    kernel32 = _kernel32()
    SYNCHRONIZE = 0x00100000
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, name)
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def release_mutex(handle: Optional[int]) -> None:
    if not handle or sys.platform != "win32":
        return
    kernel32 = _kernel32()
    kernel32.ReleaseMutex(handle)
    kernel32.CloseHandle(handle)


def acquire_application_mutex_or_exit() -> Optional[List[int]]:
    """Hold the Inno-visible app mutex for this frozen process lifetime."""
    global _APPLICATION_MUTEX_HANDLES
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return None
    if _APPLICATION_MUTEX_HANDLES:
        return _APPLICATION_MUTEX_HANDLES
    health_token = parse_health_token()
    health_launch = bool(
        health_token
        and any(mutex_exists(name) for name in UPDATE_MUTEX_NAMES)
        and is_valid_health_launch_token(health_token)
    )
    setup_gate = create_named_mutex(SETUP_MUTEX_NAME)
    if setup_gate is None and not health_launch:
        raise SystemExit(0)
    try:
        if not health_launch and any(
            mutex_exists(name) for name in UPDATE_MUTEX_NAMES
        ):
            raise SystemExit(0)
        handles = acquire_named_mutexes(APP_MUTEX_NAMES, 0.0)
        if handles is None:
            raise SystemExit(0)
        _APPLICATION_MUTEX_HANDLES = handles
        return handles
    finally:
        release_mutex(setup_gate)


def release_application_mutex_for_setup() -> None:
    """Release the app gate immediately before launching trusted Inno setup."""
    global _APPLICATION_MUTEX_HANDLES
    release_mutexes(_APPLICATION_MUTEX_HANDLES)
    _APPLICATION_MUTEX_HANDLES = []


def _filetime_value(filetime) -> int:
    return (int(filetime.dwHighDateTime) << 32) | int(filetime.dwLowDateTime)


def process_identity(pid: int) -> Tuple[str, int]:
    """Return canonical image path and creation time for ``pid``."""
    if pid <= 0:
        return "", 0
    if sys.platform != "win32":
        return "", 0
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
    )
    if not handle:
        return "", 0
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return "", 0
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return "", 0
        return canonical_path(buffer.value), _filetime_value(created)
    finally:
        kernel32.CloseHandle(handle)


def is_process_elevated() -> bool:
    """Return whether the current Windows process has an elevated token."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    TOKEN_QUERY = 0x0008
    TOKEN_ELEVATION_CLASS = 20

    class TokenElevation(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    token = wintypes.HANDLE()
    kernel32 = _kernel32()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        return True
    try:
        elevation = TokenElevation()
        returned = wintypes.DWORD()
        if not advapi32.GetTokenInformation(
            token,
            TOKEN_ELEVATION_CLASS,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(returned),
        ):
            return True
        return bool(elevation.TokenIsElevated)
    finally:
        kernel32.CloseHandle(token)


def parse_health_token(argv: Optional[List[str]] = None) -> Optional[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    if HEALTH_ARG in args:
        index = args.index(HEALTH_ARG)
        if index + 1 < len(args):
            token = args[index + 1].strip()
            return token or None
    return None


def parse_transaction_id(argv: Optional[List[str]] = None) -> Optional[str]:
    args = list(sys.argv[1:] if argv is None else argv)
    if TRANSACTION_ARG in args:
        index = args.index(TRANSACTION_ARG)
        if index + 1 < len(args):
            token = args[index + 1].strip()
            try:
                return validate_transaction_id(token)
            except ValueError:
                return None
    return None


def is_valid_health_launch_token(
    token: str, appdata: Optional[str] = None
) -> bool:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return False
    tx_root = os.path.join(updates_root(appdata), "tx")
    if not os.path.isdir(tx_root):
        return False
    for transaction_id in os.listdir(tx_root):
        try:
            journal = load_journal(transaction_id, appdata)
        except (UpdateApplyError, OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            journal.health_token == token
            and journal.state == TransactionState.NEW_ACTIVE
            and paths_equal(running_app_dir(), journal.app_dir)
            and os.path.isfile(os.path.join(journal.app_dir, APP_EXE_NAME))
            and file_sha256(os.path.join(journal.app_dir, APP_EXE_NAME))
            == journal.new_exe_sha256
        ):
            return True
    return False


def maybe_exit_if_update_in_progress(argv: Optional[List[str]] = None) -> None:
    """Exit a frozen Windows launch that raced an in-progress commit."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    setup_running = mutex_exists(SETUP_MUTEX_NAME)
    update_running = any(mutex_exists(name) for name in UPDATE_MUTEX_NAMES)
    if not setup_running and not update_running:
        return
    token = parse_health_token(argv)
    if update_running and token and is_valid_health_launch_token(token):
        return
    sys.stderr.write("OpenWhisper is finishing an update. Try again in a moment.\n")
    raise SystemExit(0)


def write_health_acknowledgement(token: str, appdata: Optional[str] = None) -> None:
    root = updates_root(appdata)
    tx_root = os.path.join(root, "tx")
    if not os.path.isdir(tx_root):
        return
    for name in os.listdir(tx_root):
        path = journal_path(name, appdata)
        if not os.path.isfile(path):
            continue
        try:
            journal = load_journal(name, appdata)
        except (UpdateApplyError, OSError, ValueError, json.JSONDecodeError):
            continue
        active_exe = os.path.join(journal.app_dir, APP_EXE_NAME)
        if (
            journal.health_token == token
            and journal.state == TransactionState.NEW_ACTIVE
            and paths_equal(running_app_dir(), journal.app_dir)
            and os.path.isfile(active_exe)
            and file_sha256(active_exe) == journal.new_exe_sha256
        ):
            target = health_path(journal.transaction_id, journal.appdata or appdata)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(token)
                handle.flush()
                os.fsync(handle.fileno())
            return


def _set_runonce(command: str) -> None:
    if sys.platform != "win32":
        return
    winreg = _winreg()
    path = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
    handle = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(handle, RUNONCE_VALUE_NAME, 0, winreg.REG_SZ, command)
        winreg.FlushKey(handle)
    finally:
        winreg.CloseKey(handle)


def _clear_runonce() -> None:
    if sys.platform != "win32":
        return
    winreg = _winreg()
    path = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
    try:
        handle = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE
        )
    except OSError:
        return
    try:
        winreg.DeleteValue(handle, RUNONCE_VALUE_NAME)
        winreg.FlushKey(handle)
    except FileNotFoundError:
        return
    finally:
        winreg.CloseKey(handle)


def _wait_for_pid(
    pid: int,
    timeout_s: float,
    *,
    expected_exe: str = "",
    expected_creation_time: int = 0,
) -> None:
    if pid <= 0:
        return
    if sys.platform != "win32":
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.2)
        raise UpdateApplyError("Timed out waiting for OpenWhisper to exit.")
    kernel32 = _kernel32()
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(
        SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
    )
    if not handle:
        return
    try:
        actual_exe, actual_creation = process_identity(pid)
        if expected_exe and not paths_equal(actual_exe, expected_exe):
            raise UpdateApplyError("The process waiting target changed.")
        if expected_creation_time and actual_creation != expected_creation_time:
            raise UpdateApplyError("The process waiting target changed.")
        waited = kernel32.WaitForSingleObject(handle, int(timeout_s * 1000))
        if waited != 0:
            raise UpdateApplyError("Timed out waiting for OpenWhisper to exit.")
    finally:
        kernel32.CloseHandle(handle)


def _force_close_pid(
    pid: int,
    *,
    expected_exe: str = "",
    expected_creation_time: int = 0,
    timeout_s: float = 15.0,
) -> bool:
    """Terminate a parent that never exited. True when the pid is gone.

    The old app holds its own files open, so a parent that hands off and then
    fails to leave blocks the swap entirely. Closing it is friendlier than
    abandoning an update the user already approved: it has finished staging and
    has nothing left to save that is not already committed atomically.

    Identity is re-checked immediately before terminating, so a pid recycled
    into an unrelated process is never the victim; a mismatch means the parent
    is already gone and is reported as success. An unidentified target is never
    terminated: ``prepare_candidate`` refuses to write a journal without the
    parent's image path and creation time, so their absence here means this is
    not the process that handed off.
    """
    if pid <= 0 or pid == os.getpid():
        return True
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        return False
    if not expected_exe or not expected_creation_time:
        return False
    actual_exe, actual_creation = process_identity(pid)
    if not actual_exe:
        return True
    if not paths_equal(actual_exe, expected_exe):
        return True
    if actual_creation != expected_creation_time:
        return True
    kernel32 = _kernel32()
    PROCESS_TERMINATE = 0x0001
    SYNCHRONIZE = 0x00100000
    handle = kernel32.OpenProcess(
        PROCESS_TERMINATE | SYNCHRONIZE, False, int(pid)
    )
    if not handle:
        return process_identity(pid)[0] == ""
    try:
        if not kernel32.TerminateProcess(handle, 0):
            return False
        return kernel32.WaitForSingleObject(handle, int(timeout_s * 1000)) == 0
    finally:
        kernel32.CloseHandle(handle)


def _launch_exe(path: str, args: List[str]):
    import subprocess

    creation = 0
    if sys.platform == "win32":
        creation = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creation |= subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            [path, *args],
            cwd=os.path.dirname(path),
            close_fds=True,
            creationflags=creation,
        )
    except OSError as exc:
        raise UpdateApplyError(f"Could not start {os.path.basename(path)}.") from exc
    return proc


def _terminate_process(process, timeout_s: float = 30.0) -> None:
    if process is None:
        return
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=timeout_s)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=timeout_s)
        except Exception as exc:
            raise UpdateApplyError(
                "The new version could not be stopped for rollback."
            ) from exc


def _wait_for_health(journal: UpdateJournal, timeout_s: float) -> bool:
    path = health_path(journal.transaction_id, journal.appdata or None)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    token = handle.read().strip()
            except OSError:
                token = ""
            if token == journal.health_token:
                return True
        time.sleep(0.25)
    return False


def _health_file_matches(journal: UpdateJournal) -> bool:
    path = health_path(journal.transaction_id, journal.appdata or None)
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() == journal.health_token
    except OSError:
        return False


def _verify_update_tree(root: str, journal: UpdateJournal) -> None:
    reject_reparse_chain(root, "Update tree")
    sentinel = os.path.join(root, SENTINEL_NAME)
    try:
        with open(sentinel, encoding="utf-8") as handle:
            sentinel_version = handle.read().strip()
    except OSError as exc:
        raise UpdateApplyError("The prepared update is incomplete.") from exc
    if sentinel_version != journal.new_version:
        raise UpdateApplyError("The prepared update has the wrong version.")
    manifest_path = os.path.join(root, MANIFEST_NAME)
    manifest = load_manifest(manifest_path)
    validate_manifest(
        manifest,
        expected_version=journal.new_version,
        current_updater_version=journal.old_version,
    )
    verify_tree_against_manifest(
        root,
        manifest,
        allowed_extra_files=set(journal.compatibility_files),
    )
    if build_compatibility_file_manifest(
        root, manifest
    ) != journal.compatibility_files:
        raise UpdateApplyError("A preserved compatibility file changed before commit.")
    new_exe = os.path.join(root, APP_EXE_NAME)
    if file_sha256(new_exe) != journal.new_exe_sha256:
        raise UpdateApplyError("The prepared executable changed before commit.")


def _verify_candidate_for_commit(journal: UpdateJournal) -> None:
    _verify_update_tree(journal.candidate_dir, journal)


def _verify_old_tree(root: str, journal: UpdateJournal) -> None:
    reject_reparse_chain(root, "Rollback tree")
    exe = os.path.join(root, APP_EXE_NAME)
    if not os.path.isfile(exe) or file_sha256(exe) != journal.old_exe_sha256:
        raise UpdateApplyError("The previous installation tree is not valid.")
    for name in journal.uninstaller_files:
        path = os.path.join(root, name)
        metadata = journal.compatibility_files.get(name)
        if (
            not os.path.isfile(path)
            or has_reparse_point(path)
            or not isinstance(metadata, dict)
            or os.path.getsize(path) != metadata["size"]
            or file_sha256(path) != metadata["sha256"]
        ):
            raise UpdateApplyError("A rollback uninstaller file is invalid.")


def _rename_dir(source: str, destination: str) -> None:
    reject_reparse(os.path.dirname(source), "Rename parent")
    if os.path.lexists(destination):
        raise UpdateApplyError(f"Cannot rename onto an existing path: {destination}")
    _replace_path_write_through(source, destination, replace_existing=False)


def update_arp_after_success(
    journal: UpdateJournal,
    registration: Optional[InstallRegistration] = None,
) -> None:
    if sys.platform != "win32":
        return
    registration = registration or discover_install_registration()
    if registration is None or registration.hive != "HKCU":
        raise UpdateApplyError("The per-user installation registration is missing.")
    if not paths_equal(registration.install_location, journal.app_dir):
        raise UpdateApplyError("The registered install location changed.")
    winreg = _winreg()
    access = winreg.KEY_SET_VALUE | registration.view
    handle = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, registration.key_path, 0, access
    )
    try:
        winreg.SetValueEx(
            handle, "DisplayVersion", 0, winreg.REG_SZ, journal.new_version
        )
        size_kb = max(1, tree_size_bytes(journal.app_dir) // 1024)
        winreg.SetValueEx(handle, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
        winreg.SetValueEx(
            handle,
            "InstallDate",
            0,
            winreg.REG_SZ,
            datetime.now(timezone.utc).strftime("%Y%m%d"),
        )
        winreg.FlushKey(handle)
    finally:
        winreg.CloseKey(handle)


def restore_arp_after_rollback(
    journal: UpdateJournal,
    registration: Optional[InstallRegistration] = None,
) -> None:
    if sys.platform != "win32":
        return
    registration = registration or discover_install_registration()
    if registration is None or registration.hive != "HKCU":
        raise UpdateApplyError("The per-user installation registration is missing.")
    if not paths_equal(registration.install_location, journal.app_dir):
        raise UpdateApplyError("The registered install location changed.")
    winreg = _winreg()
    access = winreg.KEY_SET_VALUE | registration.view
    handle = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, registration.key_path, 0, access
    )
    try:
        winreg.SetValueEx(
            handle,
            "DisplayVersion",
            0,
            winreg.REG_SZ,
            journal.old_display_version,
        )
        if journal.old_estimated_size_kb is None:
            try:
                winreg.DeleteValue(handle, "EstimatedSize")
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(
                handle,
                "EstimatedSize",
                0,
                winreg.REG_DWORD,
                journal.old_estimated_size_kb,
            )
        if journal.old_install_date:
            winreg.SetValueEx(
                handle,
                "InstallDate",
                0,
                winreg.REG_SZ,
                journal.old_install_date,
            )
        else:
            try:
                winreg.DeleteValue(handle, "InstallDate")
            except FileNotFoundError:
                pass
        winreg.FlushKey(handle)
    finally:
        winreg.CloseKey(handle)


def _cleanup_transaction(
    journal: UpdateJournal, *, include_rollback: bool = True
) -> None:
    """Delete a finished transaction's trees, archive, and journal.

    ``include_rollback`` must stay False for a transaction that never renamed
    anything: a rollback tree that exists in that case was not created here and
    is not ours to delete.
    """
    trees = [journal.candidate_dir]
    if include_rollback:
        trees.append(journal.rollback_dir)
    for path in trees:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    if journal.archive_path and os.path.isfile(journal.archive_path):
        try:
            os.unlink(journal.archive_path)
        except OSError:
            pass
    tx = transaction_dir(journal.transaction_id, journal.appdata or None)
    if (
        getattr(sys, "frozen", False)
        and os.path.basename(sys.executable).lower() == UPDATER_EXE_NAME.lower()
    ):
        cleanup_exe = os.path.join(
            updates_root(journal.appdata or None),
            "OpenWhisperUpdaterCleanup.exe",
        )
        reject_reparse_chain(os.path.dirname(cleanup_exe), "Update cleanup root")
        if os.path.lexists(cleanup_exe) and has_reparse_point(cleanup_exe):
            raise UpdateApplyError("The update cleanup helper path is unsafe.")
        shutil.copy2(sys.executable, cleanup_exe)
        with open(cleanup_exe, "rb+") as handle:
            os.fsync(handle.fileno())
        _launch_exe(
            cleanup_exe,
            [
                CLEANUP_ARG,
                TRANSACTION_ARG,
                journal.transaction_id,
                PARENT_PID_ARG,
                str(os.getpid()),
            ],
        )
        return
    shutil.rmtree(tx, ignore_errors=True)


def _restore_rollback(journal: UpdateJournal) -> None:
    failed = journal.app_dir.rstrip("\\/") + ".failed"
    if os.path.isdir(journal.rollback_dir):
        _verify_old_tree(journal.rollback_dir, journal)
    if os.path.isdir(journal.app_dir) and os.path.isdir(journal.rollback_dir):
        if os.path.lexists(failed):
            shutil.rmtree(failed, ignore_errors=True)
        _replace_path_write_through(
            journal.app_dir, failed, replace_existing=False
        )
        try:
            _replace_path_write_through(
                journal.rollback_dir, journal.app_dir, replace_existing=False
            )
        except OSError:
            if os.path.isdir(failed) and not os.path.isdir(journal.app_dir):
                _replace_path_write_through(
                    failed, journal.app_dir, replace_existing=False
                )
            raise
        shutil.rmtree(failed, ignore_errors=True)
        return
    if os.path.isdir(journal.rollback_dir) and not os.path.isdir(journal.app_dir):
        _replace_path_write_through(
            journal.rollback_dir, journal.app_dir, replace_existing=False
        )
        shutil.rmtree(failed, ignore_errors=True)
        return
    if os.path.isdir(journal.app_dir) and not os.path.isdir(journal.rollback_dir):
        _verify_old_tree(journal.app_dir, journal)
        shutil.rmtree(failed, ignore_errors=True)
        return
    raise UpdateApplyError("Neither a usable install nor rollback tree remains.")


def commit_prepared_update(
    journal: UpdateJournal,
    *,
    wait_parent: bool = True,
    launch: bool = True,
    health_timeout_s: float = _HEALTH_WAIT_S,
    parent_timeout_s: float = _PARENT_WAIT_S,
    hooks: Optional[Dict[str, Callable]] = None,
    registration: Optional[InstallRegistration] = None,
) -> str:
    """Apply a prepared journal. Returns the final :class:`TransactionState`."""
    hooks = hooks or {}
    setup_mutex = create_named_mutex(SETUP_MUTEX_NAME)
    if setup_mutex is None:
        raise UpdateApplyError("The setup program is already running.")
    update_mutexes = acquire_named_mutexes(UPDATE_MUTEX_NAMES, 0.0)
    if update_mutexes is None:
        release_mutex(setup_mutex)
        raise UpdateApplyError("Another update is already in progress.")
    app_mutexes: Optional[List[int]] = None
    launched_process = None
    relaunch_old = False
    destructive_started = False
    health_confirmed = False
    runonce_installed = False
    try:
        validate_journal_binding(journal, journal.appdata or None)
        if journal.state != TransactionState.PREPARED:
            raise UpdateApplyError("The update transaction is not prepared.")
        if wait_parent:
            try:
                _wait_for_pid(
                    journal.parent_pid,
                    parent_timeout_s,
                    expected_exe=journal.parent_exe,
                    expected_creation_time=journal.parent_creation_time,
                )
            except UpdateApplyError as exc:
                if not _force_close_pid(
                    journal.parent_pid,
                    expected_exe=journal.parent_exe,
                    expected_creation_time=journal.parent_creation_time,
                ):
                    raise UpdateApplyError(_PARENT_STILL_RUNNING) from exc
        app_mutexes = acquire_named_mutexes(APP_MUTEX_NAMES, parent_timeout_s)
        if app_mutexes is None:
            raise UpdateApplyError(_PARENT_STILL_RUNNING)
        if "after_parent_exit" in hooks:
            hooks["after_parent_exit"](journal)

        if registration is None:
            registration = discover_install_registration()
        if registration is None or registration.hive != "HKCU":
            raise UpdateApplyError("The install registration is no longer valid.")
        if not paths_equal(registration.install_location, journal.app_dir):
            raise UpdateApplyError("The install location changed during the update.")
        if is_process_elevated() or _is_under_program_files(journal.app_dir):
            raise UpdateApplyError("Elevated installations must use the setup program.")
        reject_reparse_chain(journal.app_dir, "Install directory")
        old_exe = os.path.join(journal.app_dir, APP_EXE_NAME)
        if (
            not os.path.isfile(old_exe)
            or file_sha256(old_exe) != journal.old_exe_sha256
        ):
            raise UpdateApplyError("The installed executable changed during the update.")
        if os.path.lexists(journal.rollback_dir):
            raise UpdateApplyError("A previous rollback tree is still on disk.")
        if not os.path.isdir(journal.candidate_dir):
            raise UpdateApplyError("The prepared update is missing.")
        if registered_uninstaller_path(registration, journal.app_dir) is None:
            raise UpdateApplyError("The registered uninstaller is missing.")
        if not set(journal.uninstaller_files).issubset(
            set(uninstaller_basenames(registration))
        ):
            raise UpdateApplyError("The compatibility-file list changed.")
        if uninstaller_basenames(registration)[0] not in journal.uninstaller_files:
            raise UpdateApplyError("The uninstaller was not preserved.")
        if not set(journal.uninstaller_files).issubset(
            set(journal.compatibility_files)
        ):
            raise UpdateApplyError("A preserved uninstaller file is unverified.")
        _verify_candidate_for_commit(journal)

        recover_cmd = (
            f'"{helper_exe_for(journal)}" {TRANSACTION_ARG} '
            f"{journal.transaction_id} {RECOVER_ARG}"
        )
        _set_runonce(recover_cmd)
        runonce_installed = True

        # Persist intent before each rename. Recovery reconciles the journal
        # with all three directory trees, so power loss in either gap is safe.
        journal.state = TransactionState.OLD_MOVED
        save_journal(journal)
        destructive_started = True
        _rename_dir(journal.app_dir, journal.rollback_dir)
        if "after_old_moved" in hooks:
            hooks["after_old_moved"](journal)

        journal.state = TransactionState.NEW_ACTIVE
        save_journal(journal)
        _rename_dir(journal.candidate_dir, journal.app_dir)
        if "after_new_active" in hooks:
            hooks["after_new_active"](journal)

        new_exe = os.path.join(journal.app_dir, APP_EXE_NAME)
        # The health-check app acquires the app mutexes itself. Update/setup
        # mutexes remain held, blocking every ordinary launch and Inno.
        release_mutexes(app_mutexes)
        app_mutexes = None
        if launch:
            launched_process = _launch_exe(
                new_exe, [HEALTH_ARG, journal.health_token]
            )
            if not _wait_for_health(journal, health_timeout_s):
                raise UpdateApplyError(
                    "The new version started but did not become ready."
                )
        else:
            target = health_path(journal.transaction_id, journal.appdata or None)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(journal.health_token)
                handle.flush()
                os.fsync(handle.fileno())

        health_confirmed = True
        if "before_finalize" in hooks:
            hooks["before_finalize"](journal)
        update_arp_after_success(journal, registration)
        journal.state = TransactionState.HEALTHY
        save_journal(journal)
        _clear_runonce()
        runonce_installed = False
        _cleanup_transaction(journal)
        return TransactionState.HEALTHY
    except Exception as exc:
        if health_confirmed:
            write_apply_error(
                f"The new version is running, but update finalization failed: {exc}",
                journal.appdata or None,
            )
            raise
        rollback_error: Optional[Exception] = None
        try:
            _terminate_process(launched_process)
            # Only a restore needs the application lock. Failing before the
            # first rename leaves the install untouched, and demanding the lock
            # there turned "OpenWhisper is still running" into a second,
            # alarming "rollback failed" for a transaction that never moved a
            # single file.
            if app_mutexes is None and destructive_started:
                app_mutexes = acquire_named_mutexes(APP_MUTEX_NAMES, 30.0)
                if app_mutexes is None:
                    raise UpdateApplyError(
                        "OpenWhisper stayed locked during rollback."
                    )
            if destructive_started:
                _restore_rollback(journal)
                restore_arp_after_rollback(journal, registration)
            else:
                _verify_old_tree(journal.app_dir, journal)
            if os.path.isdir(journal.candidate_dir):
                shutil.rmtree(journal.candidate_dir, ignore_errors=True)
            journal.state = TransactionState.ROLLED_BACK
            save_journal(journal)
            write_apply_error(str(exc), journal.appdata or None)
            if runonce_installed:
                _clear_runonce()
                runonce_installed = False
            # The journal is terminal either way: leaving the archive and the
            # candidate tree behind costs the user a gigabyte per attempt.
            _cleanup_transaction(journal, include_rollback=destructive_started)
            relaunch_old = launch
        except Exception as rollback_exc:
            rollback_error = rollback_exc
            write_apply_error(
                f"{exc}\nRollback also failed: {rollback_exc}",
                journal.appdata or None,
            )
        if "after_rollback" in hooks:
            hooks["after_rollback"](journal)
        # Release both gates before relaunching the ordinary old application.
        release_mutexes(app_mutexes)
        app_mutexes = None
        release_mutexes(update_mutexes)
        update_mutexes = None
        release_mutex(setup_mutex)
        setup_mutex = None
        # An update that failed because the app never exited does not need a
        # second copy started behind the one the user is still looking at.
        app_still_running = any(mutex_exists(name) for name in APP_MUTEX_NAMES)
        if relaunch_old and not app_still_running:
            old_exe = os.path.join(journal.app_dir, APP_EXE_NAME)
            if os.path.isfile(old_exe):
                _launch_exe(old_exe, [])
        if rollback_error is not None:
            raise UpdateApplyError(
                f"{exc}; rollback failed: {rollback_error}"
            ) from rollback_error
        raise
    finally:
        release_mutexes(app_mutexes)
        release_mutexes(update_mutexes)
        release_mutex(setup_mutex)


def recover_transaction(
    transaction_id: str,
    appdata: Optional[str] = None,
    registration: Optional[InstallRegistration] = None,
) -> str:
    """Resume or roll back a journal left behind by a crash or power loss."""
    token = validate_transaction_id(transaction_id)
    setup_mutex = create_named_mutex(SETUP_MUTEX_NAME)
    if setup_mutex is None:
        raise UpdateApplyError("The setup program is already running.")
    update_mutexes = acquire_named_mutexes(UPDATE_MUTEX_NAMES, 0.0)
    if update_mutexes is None:
        release_mutex(setup_mutex)
        raise UpdateApplyError("Another update is already in progress.")
    app_mutexes: Optional[List[int]] = None
    relaunch = False
    journal: Optional[UpdateJournal] = None
    try:
        app_mutexes = acquire_named_mutexes(APP_MUTEX_NAMES, 30.0)
        if app_mutexes is None:
            raise UpdateApplyError("OpenWhisper is still running.")
        journal = load_journal(token, appdata)
        registration = registration or discover_install_registration()
        if (
            registration is None
            or registration.hive != "HKCU"
            or not paths_equal(registration.install_location, journal.app_dir)
            or is_process_elevated()
            or _is_under_program_files(journal.app_dir)
        ):
            raise UpdateApplyError("The install registration is no longer valid.")
        expected_uninstaller = uninstaller_basenames(registration)
        if (
            not expected_uninstaller
            or expected_uninstaller[0] not in journal.uninstaller_files
        ):
            raise UpdateApplyError("The rollback uninstaller is not bound.")
        reject_reparse_chain(os.path.dirname(journal.app_dir), "Install parent")
        for path in (
            journal.app_dir,
            journal.candidate_dir,
            journal.rollback_dir,
        ):
            if os.path.lexists(path):
                reject_reparse_chain(path, "Update tree")

        app_exists = os.path.isdir(journal.app_dir)
        candidate_exists = os.path.isdir(journal.candidate_dir)
        rollback_exists = os.path.isdir(journal.rollback_dir)

        rollback_valid = False
        if rollback_exists:
            _verify_old_tree(journal.rollback_dir, journal)
            rollback_valid = True

        active_is_new = False
        active_is_old = False
        if app_exists:
            try:
                _verify_update_tree(journal.app_dir, journal)
                active_is_new = True
            except (OSError, UpdateApplyError, ValueError, json.JSONDecodeError):
                try:
                    _verify_old_tree(journal.app_dir, journal)
                    active_is_old = True
                except (OSError, UpdateApplyError):
                    pass

        health_confirmed = (
            journal.state == TransactionState.HEALTHY
            or _health_file_matches(journal)
        )
        if active_is_new and health_confirmed:
            update_arp_after_success(journal, registration)
            journal.state = TransactionState.HEALTHY
            save_journal(journal)
            _clear_runonce()
            _cleanup_transaction(journal)
            relaunch = True
            return journal.state

        if active_is_old:
            if rollback_valid:
                shutil.rmtree(journal.rollback_dir)
        elif rollback_valid:
            _restore_rollback(journal)
        else:
            raise UpdateApplyError(
                "The interrupted update has no validated rollback tree."
            )

        if candidate_exists and os.path.isdir(journal.candidate_dir):
            shutil.rmtree(journal.candidate_dir, ignore_errors=True)
        _verify_old_tree(journal.app_dir, journal)
        restore_arp_after_rollback(journal, registration)
        journal.state = TransactionState.ROLLED_BACK
        save_journal(journal)
        write_apply_error(
            "The previous update was interrupted and was rolled back.",
            journal.appdata or None,
        )
        _clear_runonce()
        relaunch = True
        return journal.state
    finally:
        release_mutexes(app_mutexes)
        release_mutexes(update_mutexes)
        release_mutex(setup_mutex)
        if relaunch and journal is not None:
            app_exe = os.path.join(journal.app_dir, APP_EXE_NAME)
            if os.path.isfile(app_exe):
                _launch_exe(app_exe, [])


def native_message_box(message: str) -> None:
    """Last-resort error UI when Qt is not available."""
    if sys.platform != "win32":
        sys.stderr.write(message + "\n")
        return
    import ctypes

    ctypes.windll.user32.MessageBoxW(None, message[:_ERROR_LIMIT], APP_NAME, 0x10)


def pack_tar_xz(source_dir: str, destination: str) -> None:
    """Create a tar.xz whose members are the contents of ``source_dir``."""
    files = iter_managed_files(source_dir)
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    try:
        archive = tarfile.open(destination, "w:xz", preset=9)
    except TypeError:
        archive = tarfile.open(destination, "w:xz")
    with archive:
        for relative in files:
            full = os.path.join(source_dir, *relative.split("/"))
            archive.add(full, arcname=relative, recursive=False)


def write_manifest_file(dist_dir: str, version: str) -> str:
    manifest = build_update_manifest(dist_dir, version)
    path = os.path.join(dist_dir, MANIFEST_NAME)
    write_json_atomic(path, manifest)
    return path
