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
    # The real ones attach a handler to the user's updater log and change the
    # test process's working directory.
    module.setup_updater_logging = lambda *args, **kwargs: None
    module.leave_launch_directory = lambda *args, **kwargs: None
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


def test_cleanup_failure_is_not_reported_as_an_update_failure():
    module = _helper_module()
    with patch.object(module, "parse_transaction_id", return_value="abc"), patch.object(
        module, "parse_parent_pid", return_value=42
    ), patch.object(
        module,
        "cleanup_transaction_after_parent",
        side_effect=OSError(13, "Access is denied"),
    ), patch.object(module, "native_message_box") as box:
        assert module.main(
            [
                "--transaction-id",
                "abc",
                "--cleanup-transaction",
                "--parent-pid",
                "42",
            ]
        ) == 1
    box.assert_not_called()


def test_helper_reports_apply_error():
    module = _helper_module()
    with patch.object(module, "parse_transaction_id", return_value="abc"), patch.object(
        module, "load_journal", side_effect=UpdateApplyError("missing journal")
    ), patch.object(module, "native_message_box") as box:
        assert module.main(["--transaction-id", "abc"]) == 1
    box.assert_called_once()


def test_helper_leaves_its_launch_directory_before_committing():
    module = _helper_module()
    calls = []
    module.leave_launch_directory = lambda *args, **kwargs: calls.append("left")
    with patch.object(module, "parse_transaction_id", return_value="abc"), patch.object(
        module, "load_journal", return_value=object()
    ), patch.object(
        module,
        "commit_prepared_update",
        side_effect=lambda journal: calls.append("committed"),
    ):
        assert module.main(["--transaction-id", "abc"]) == 0
    assert calls == ["left", "committed"]


def test_start_detached_tuple_is_unpacked():
    class _Proc:
        @staticmethod
        def startDetached(program, arguments, working_directory=""):
            return (False, 0)

    with patch("PyQt6.QtCore.QProcess", _Proc):
        assert _start_detached("x.exe", []) is False

    class _Ok:
        @staticmethod
        def startDetached(program, arguments, working_directory=""):
            return (True, 42)

    with patch("PyQt6.QtCore.QProcess", _Ok):
        assert _start_detached("x.exe", []) is True


def test_start_detached_runs_the_program_from_its_own_directory():
    """The helper must not inherit the app's working directory.

    Shortcuts start the app inside the install directory, and a child that
    keeps that directory as its working directory blocks renaming it.
    """
    seen = {}

    class _Proc:
        @staticmethod
        def startDetached(program, arguments, working_directory=""):
            seen["cwd"] = working_directory
            return (True, 1)

    helper = str(Path("C:/appdata/updates/tx/abc/OpenWhisperUpdater.exe"))
    with patch("PyQt6.QtCore.QProcess", _Proc):
        assert _start_detached(helper, ["--transaction-id", "abc"]) is True
    assert seen["cwd"] == str(Path("C:/appdata/updates/tx/abc"))
