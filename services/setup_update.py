"""Setup's durable backup and native-transaction retirement protocol.

Inno invokes the bundled helper from its temporary directory while holding
the setup mutex. User data and the registered uninstaller stay outside this
replacement; only the explicitly managed runtime is staged.
"""

from __future__ import annotations

import json
import logging
import os
import shutil

from services import app_update_apply as apply
from services.update_contract import (
    APP_EXE_NAME,
    APP_ID,
    INTERNAL_DIRNAME,
    MANAGED_TOP_LEVEL_FILES,
    MANIFEST_NAME,
    NVIDIA_RELATIVE,
    SENTINEL_NAME,
    TransactionState,
    updates_root,
)

logger = logging.getLogger(__name__)
_STAGE_MARKER = "setup.json"
_MANAGED = (*MANAGED_TOP_LEVEL_FILES, INTERNAL_DIRNAME, SENTINEL_NAME)


def _paths(app_dir: str) -> tuple[str, str]:
    target = apply.canonical_path(app_dir)
    if not os.path.isabs(app_dir) or target == os.path.dirname(target):
        raise apply.UpdateApplyError("Setup needs an absolute application directory.")
    apply.reject_reparse_chain(target, "Setup directory")
    backup = target + ".setup-backup"
    apply.reject_reparse_chain(backup, "Setup backup")
    return target, backup


def _load_stage(app_dir: str) -> tuple[str, str, dict]:
    target, backup = _paths(app_dir)
    with open(os.path.join(backup, _STAGE_MARKER), encoding="utf-8") as handle:
        state = json.load(handle)
    if (
        state.get("app_id") != APP_ID
        or state.get("app_dir") != target
        or state.get("state") not in {"copying", "prepared", "installed"}
        or not isinstance(state.get("files"), list)
        or any(name not in _MANAGED for name in state["files"])
    ):
        raise apply.UpdateApplyError("The setup backup does not belong to this installation.")
    return target, backup, state


def _remove(path: str) -> None:
    apply.reject_reparse_chain(path, "Setup managed path")
    if os.path.isdir(path):
        apply._retry_while_locked(path, lambda: shutil.rmtree(path))
    elif os.path.lexists(path):
        apply._retry_while_locked(path, lambda: os.unlink(path))


def _copy(source: str, destination: str) -> None:
    apply.reject_reparse_chain(source, "Setup backup source")
    apply.reject_reparse_chain(destination, "Setup backup destination")
    if os.path.isdir(source):
        apply.iter_managed_files(source)
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def retire_native_transactions(app_dir: str, appdata: str | None = None) -> None:
    target, _backup = _paths(app_dir)
    tx_root = os.path.join(updates_root(appdata), "tx")
    if not os.path.isdir(tx_root):
        return
    apply.reject_reparse_chain(tx_root, "Update transaction root")
    for token in sorted(os.listdir(tx_root)):
        try:
            journal = apply.load_journal(token, appdata)
        except (OSError, ValueError, apply.UpdateApplyError):
            continue
        if not apply.paths_equal(journal.app_dir, target):
            continue
        if journal.state not in {TransactionState.HEALTHY, TransactionState.ROLLED_BACK}:
            journal.cleanup_rollback = journal.state in {
                TransactionState.OLD_MOVED, TransactionState.NEW_ACTIVE,
                TransactionState.SUPERSEDED,
            }
        active = os.path.join(target, APP_EXE_NAME)
        if (
            journal.state in {TransactionState.OLD_MOVED, TransactionState.NEW_ACTIVE}
            and not apply._health_file_matches(journal)
            and os.path.isdir(journal.rollback_dir)
            and (not os.path.isfile(active) or apply.file_sha256(active) in {
                journal.old_exe_sha256, journal.new_exe_sha256,
            })
        ):
            apply._restore_rollback(journal)
            from services.update_data import restore_data
            restore_data(journal)
        # Older helpers reject this state before they can move an install.
        journal.state = TransactionState.SUPERSEDED
        apply.save_journal(journal)
        apply.clear_transaction_runonce(journal)
        apply._cleanup_transaction(journal)


def prepare_setup(app_dir: str, appdata: str | None = None) -> None:
    target, backup = _paths(app_dir)
    if os.path.exists(backup):
        _target, _backup, previous = _load_stage(target)
        if previous["state"] == "prepared":
            rollback_setup(target)
        else:
            _remove(backup)
    retire_native_transactions(target, appdata)
    os.makedirs(target, exist_ok=True)
    present = [name for name in _MANAGED if os.path.exists(os.path.join(target, name))]
    apply.check_free_space(os.path.dirname(target), apply.tree_size_bytes(target))
    os.mkdir(backup)
    state = {"app_id": APP_ID, "app_dir": target, "state": "copying", "files": present}
    marker = os.path.join(backup, _STAGE_MARKER)
    apply.write_json_atomic(marker, state)
    try:
        for name in present:
            _copy(os.path.join(target, name), os.path.join(backup, name))
        apply._fsync_tree(backup)
        state["state"] = "prepared"
        apply.write_json_atomic(marker, state)
        for name in present:
            _remove(os.path.join(target, name))
    except Exception:
        if state["state"] == "prepared":
            rollback_setup(target)
        else:
            _remove(backup)
        raise


def finish_setup(app_dir: str) -> None:
    target, backup, state = _load_stage(app_dir)
    if state["state"] == "installed":
        cleanup_setup_backup(target)
        return
    if state["state"] != "prepared":
        raise apply.UpdateApplyError("Setup did not finish preparing its backup.")
    legacy_gpu = os.path.join(backup, NVIDIA_RELATIVE)
    installed_gpu = os.path.join(target, NVIDIA_RELATIVE)
    if os.path.isdir(legacy_gpu) and not os.path.exists(installed_gpu):
        _copy(legacy_gpu, installed_gpu)
    manifest = apply.load_manifest(os.path.join(target, MANIFEST_NAME))
    extras = {
        name for name in apply.iter_managed_files(target)
        if "/" not in name and name not in manifest["files"]
    }
    apply.verify_tree_against_manifest(
        target, manifest, allowed_extra_files=extras,
        allowed_extra_prefixes=(NVIDIA_RELATIVE.replace(os.sep, "/"),),
    )
    state["state"] = "installed"
    apply.write_json_atomic(os.path.join(backup, _STAGE_MARKER), state)
    cleanup_setup_backup(target)


def rollback_setup(app_dir: str) -> None:
    target, backup, state = _load_stage(app_dir)
    if state["state"] == "installed":
        return
    if state["state"] == "prepared":
        # Copy back instead of consuming the backup so recovery can be retried
        # after interruption at any file, including the executable.
        for name in _MANAGED:
            destination = os.path.join(target, name)
            _remove(destination)
            if name in state["files"]:
                _copy(os.path.join(backup, name), destination)
        apply._fsync_tree(target)
    _remove(backup)


def cleanup_setup_backup(app_dir: str) -> None:
    _target, backup = _paths(app_dir)
    if not os.path.isdir(backup):
        return
    try:
        _target, backup, state = _load_stage(app_dir)
        if state["state"] == "installed":
            _remove(backup)
    except (OSError, apply.UpdateApplyError, ValueError):
        logger.info("Setup backup cleanup will retry on next start", exc_info=True)
