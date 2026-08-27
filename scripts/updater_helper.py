"""Windowless helper that commits a prepared OpenWhisper update.

Frozen as ``OpenWhisperUpdater.exe`` and copied next to the running app.
The current install's helper is copied into the transaction directory before
the app quits so the commit process is not inside ``{app}``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.app_update_apply import (  # noqa: E402
    UpdateApplyError,
    cleanup_transaction_after_parent,
    commit_prepared_update,
    load_journal,
    native_message_box,
    parse_parent_pid,
    parse_transaction_id,
    recover_transaction,
)
from services.update_contract import CLEANUP_ARG, RECOVER_ARG  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    transaction_id = parse_transaction_id(args)
    if not transaction_id:
        native_message_box("OpenWhisper updater was started without a transaction.")
        return 2
    try:
        if CLEANUP_ARG in args:
            parent_pid = parse_parent_pid(args)
            if parent_pid is None:
                raise UpdateApplyError("The cleanup parent process is missing.")
            cleanup_transaction_after_parent(transaction_id, parent_pid)
            return 0
        if RECOVER_ARG in args:
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


if __name__ == "__main__":
    sys.exit(main())
