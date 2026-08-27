"""Tests for the windowless updater helper entry."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

from services.app_update_apply import UpdateApplyError
from services.update_contract import TransactionState
from ui_qt.ui_controller import _start_detached

HELPER = Path(__file__).resolve().parents[1] / "scripts" / "updater_helper.py"


def _helper_module():
    spec = importlib.util.spec_from_file_location("openwhisper_updater_helper", HELPER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_helper_requires_transaction_id():
    module = _helper_module()
    with patch.object(module, "native_message_box") as box:
        assert module.main([]) == 2
    box.assert_called_once()


def test_helper_commits_prepared_journal():
    module = _helper_module()
    journal = object()
    with patch.object(module, "parse_transaction_id", return_value="abc"), patch.object(
        module, "load_journal", return_value=journal
    ), patch.object(
        module, "commit_prepared_update", return_value=TransactionState.HEALTHY
    ) as commit:
        assert module.main(["--transaction-id", "abc"]) == 0
    commit.assert_called_once_with(journal)


def test_helper_recover_path():
    module = _helper_module()
    with patch.object(module, "parse_transaction_id", return_value="abc"), patch.object(
        module, "recover_transaction"
    ) as recover:
        assert module.main(["--transaction-id", "abc", "--recover"]) == 0
    recover.assert_called_once_with("abc")


def test_helper_cleanup_path():
    module = _helper_module()
    with patch.object(module, "parse_transaction_id", return_value="abc"), patch.object(
        module, "parse_parent_pid", return_value=42
    ), patch.object(module, "cleanup_transaction_after_parent") as cleanup:
        assert module.main(
            [
                "--transaction-id",
                "abc",
                "--cleanup-transaction",
                "--parent-pid",
                "42",
            ]
        ) == 0
    cleanup.assert_called_once_with("abc", 42)


def test_helper_reports_apply_error():
    module = _helper_module()
    with patch.object(module, "parse_transaction_id", return_value="abc"), patch.object(
        module, "load_journal", side_effect=UpdateApplyError("missing journal")
    ), patch.object(module, "native_message_box") as box:
        assert module.main(["--transaction-id", "abc"]) == 1
    box.assert_called_once()


def test_start_detached_tuple_is_unpacked():
    class _Proc:
        @staticmethod
        def startDetached(program, arguments):
            return (False, 0)

    with patch("PyQt6.QtCore.QProcess", _Proc):
        assert _start_detached("x.exe", []) is False

    class _Ok:
        @staticmethod
        def startDetached(program, arguments):
            return (True, 42)

    with patch("PyQt6.QtCore.QProcess", _Ok):
        assert _start_detached("x.exe", []) is True
