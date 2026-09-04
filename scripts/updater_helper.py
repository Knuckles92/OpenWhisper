"""Windowless helper that commits a prepared OpenWhisper update.

Frozen as ``OpenWhisperUpdater.exe`` and copied next to the running app.
The current install's helper is copied into the transaction directory before
the app quits so the commit process is not inside ``{app}``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.app_update_apply import (  # noqa: E402
    UpdateApplyError,
    _wait_for_pid,
    cleanup_transaction_after_parent,
    commit_prepared_update,
    leave_launch_directory,
    load_journal,
    native_message_box,
    parse_parent_pid,
    parse_transaction_id,
    recover_transaction,
    setup_updater_logging,
)
from services.update_contract import CLEANUP_ARG, RECOVER_ARG  # noqa: E402

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    setup_updater_logging()
    if "--setup-action" in args:
        return _run_setup(args)
    transaction_id = parse_transaction_id(args)
    if not transaction_id:
        native_message_box("OpenWhisper updater was started without a transaction.")
        return 2
    if leave_launch_directory(args):
        return 0
    try:
        if CLEANUP_ARG in args:
            return _run_cleanup(transaction_id, args)
        if RECOVER_ARG in args:
            parent_pid = parse_parent_pid(args)
            if parent_pid:
                _wait_for_pid(parent_pid, 120.0)
            recover_transaction(transaction_id)
            return 0
        journal = load_journal(transaction_id)
        commit_prepared_update(journal)
        return 0
    except UpdateApplyError as exc:
        native_message_box(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 - last-resort helper failure
        native_message_box(f"The update failed: {exc}")
        return 1


def _run_setup(args: list[str]) -> int:
    import argparse

    from services.setup_update import finish_setup, prepare_setup, rollback_setup

    parser = argparse.ArgumentParser()
    parser.add_argument("--setup-action", choices=("prepare", "finish", "rollback"), required=True)
    parser.add_argument("--app-dir", required=True)
    parser.add_argument("--error-file", required=True)
    options = parser.parse_args(args)
    try:
        actions = {"prepare": prepare_setup, "finish": finish_setup, "rollback": rollback_setup}
        actions[options.setup_action](options.app_dir)
        return 0
    except Exception as exc:
        logger.exception("Setup %s failed", options.setup_action)
        Path(options.error_file).write_text(str(exc), encoding="utf-8")
        return 1


def _run_cleanup(transaction_id: str, args: list[str]) -> int:
    """Delete a finished transaction's leftovers.

    Cleanup runs after the update has already been decided and reported, so it
    stays silent: a dialog here reads as a second, contradictory verdict on the
    update. Whatever it cannot delete is collected the next time the app starts.
    """
    parent_pid = parse_parent_pid(args)
    if parent_pid is None:
        logger.error("Cleanup of %s has no parent process", transaction_id)
        return 2
    try:
        cleanup_transaction_after_parent(transaction_id, parent_pid)
    except Exception:  # noqa: BLE001 - leftovers are not worth a dialog
        logger.exception("Could not delete transaction %s", transaction_id)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
