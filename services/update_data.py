"""Consistent pre-startup snapshots for native-update rollback."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from contextlib import closing

from services import app_update_apply as apply
from services.update_contract import transaction_dir

_NAMES = ("openwhisper_settings.json", "openwhisper.db")
_MARKER = "data-snapshot.json"


def snapshot_data(journal) -> None:
    root = transaction_dir(journal.transaction_id, journal.appdata)
    destination = os.path.join(root, "data")
    apply.reject_reparse_chain(destination, "Update data snapshot")
    os.makedirs(destination, exist_ok=False)
    records = {}
    for name in _NAMES:
        source = os.path.join(journal.appdata, name)
        apply.reject_reparse_chain(source, "Application data")
        if not os.path.exists(source):
            records[name] = None
            continue
        target = os.path.join(destination, name)
        if name.endswith(".db"):
            # SQLite's backup API includes committed WAL data; copying the
            # database file alone can silently lose recent transcripts.
            with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
        else:
            shutil.copy2(source, target)
        with open(target, "rb+") as handle:
            os.fsync(handle.fileno())
        records[name] = apply.file_sha256(target)
    apply.write_json_atomic(os.path.join(root, _MARKER), records)


def restore_data(journal) -> None:
    root = transaction_dir(journal.transaction_id, journal.appdata)
    marker = os.path.join(root, _MARKER)
    if not os.path.isfile(marker):
        return
    apply.reject_reparse_chain(root, "Update data snapshot")
    with open(marker, encoding="utf-8") as handle:
        records = json.load(handle)
    if not isinstance(records, dict) or set(records) != set(_NAMES):
        raise apply.UpdateApplyError("The update data snapshot is invalid.")
    for name, digest in records.items():
        source = os.path.join(root, "data", name)
        if digest is not None:
            apply.reject_reparse_chain(source, "Update data snapshot")
            if apply.file_sha256(source) != digest:
                raise apply.UpdateApplyError("The update data snapshot failed verification.")
    for name, digest in records.items():
        target = os.path.join(journal.appdata, name)
        apply.reject_reparse_chain(target, "Application data")
        if name.endswith(".db"):
            for suffix in ("-wal", "-shm"):
                sidecar = target + suffix
                apply.reject_reparse_chain(sidecar, "Application database sidecar")
                if os.path.isfile(sidecar):
                    os.unlink(sidecar)
        if digest is None:
            if os.path.isfile(target):
                os.unlink(target)
            continue
        temporary = target + ".update-restore"
        apply.reject_reparse_chain(temporary, "Restored application data")
        shutil.copy2(os.path.join(root, "data", name), temporary)
        with open(temporary, "rb+") as handle:
            os.fsync(handle.fileno())
        apply._replace_path_write_through(temporary, target, replace_existing=True)
