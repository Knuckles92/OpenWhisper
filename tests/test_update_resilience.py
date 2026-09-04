"""Regressions for interruptions across download, setup, and restart."""
import hashlib
import io
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest

from services import app_update as download
from services import app_update_apply as apply
from services import setup_update, update_data
from services.update_contract import (
    APP_EXE_NAME,
    MANIFEST_NAME,
    TransactionState,
    transaction_dir,
)
from tests.test_app_update_apply import TestCommitAndRecover as _JournalBuilder
from tests.test_app_update_apply import _bundle, _registration


@pytest.fixture(autouse=True)
def isolate_windows_state(monkeypatch):
    prefix = "Local\\OpenWhisper-Resilience-" + uuid.uuid4().hex
    monkeypatch.setattr(apply, "APP_MUTEX_NAMES", (prefix + "-app",))
    monkeypatch.setattr(apply, "UPDATE_MUTEX_NAMES", (prefix + "-update",))
    monkeypatch.setattr(apply, "SETUP_MUTEX_NAME", prefix + "-setup")
    for name in ("_set_runonce", "_clear_runonce", "clear_transaction_runonce",
                 "update_arp_after_success", "restore_arp_after_rollback", "_launch_exe"):
        monkeypatch.setattr(apply, name, MagicMock())
    monkeypatch.setattr(apply, "is_process_elevated", lambda: False)


@pytest.fixture
def journal(tmp_path):
    app = _bundle(tmp_path / "OpenWhisper", "2.5.2", b"old")
    (app / "unins000.exe").write_bytes(b"uninstaller")
    candidate = _bundle(tmp_path / "OpenWhisper.new-aaaaaaaa", "2.5.3", b"new")
    apply.write_completion_sentinel(str(candidate), "2.5.3")
    result = _JournalBuilder()._journal(tmp_path, app, candidate)
    apply.save_journal(result)
    return result


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {}


def fetch(tmp_path, body=b"complete-update"):
    destination = tmp_path / "update.exe"
    download._download_verified(
        "https://github.com/update.exe", hashlib.sha256(body).hexdigest(),
        len(body), str(destination), lambda *args: None, threading.Event(),
    )
    return destination


def test_complete_partial_is_verified_without_range_request(tmp_path, monkeypatch):
    (tmp_path / "update.exe.part").write_bytes(b"complete-update")
    opener = MagicMock()
    monkeypatch.setattr(download, "_open", opener)
    assert fetch(tmp_path).read_bytes() == b"complete-update"
    opener.assert_not_called()


def test_wrong_complete_partial_restarts(tmp_path, monkeypatch):
    (tmp_path / "update.exe.part").write_bytes(b"x" * len(b"complete-update"))
    opener = MagicMock(return_value=Response(b"complete-update"))
    monkeypatch.setattr(download, "_open", opener)
    assert fetch(tmp_path).read_bytes() == b"complete-update"
    assert opener.call_args.args[1] is None


def test_range_416_restarts_once(tmp_path, monkeypatch):
    (tmp_path / "update.exe.part").write_bytes(b"complete")
    opener = MagicMock(side_effect=[
        HTTPError("https://github.com/update.exe", 416, "range", {}, None),
        Response(b"complete-update"),
    ])
    monkeypatch.setattr(download, "_open", opener)
    assert fetch(tmp_path).read_bytes() == b"complete-update"
    assert opener.call_count == 2


def test_short_response_keeps_prefix_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(download, "_open", lambda *args: Response(b"complete"))
    with pytest.raises(download.AppUpdateError, match="did not complete"):
        fetch(tmp_path)
    assert (tmp_path / "update.exe.part").read_bytes() == b"complete"
    opener = MagicMock(return_value=Response(
        b"-update", 206, {"Content-Range": "bytes 8-14/15"},
    ))
    monkeypatch.setattr(download, "_open", opener)
    assert fetch(tmp_path).read_bytes() == b"complete-update"
    assert opener.call_args.args[1] == {"Range": "bytes=8-"}


def test_locked_cleanup_retains_journal_and_retries(journal, monkeypatch):
    os.rename(journal.app_dir, journal.rollback_dir)
    os.rename(journal.candidate_dir, journal.app_dir)
    journal.state = TransactionState.HEALTHY
    journal.cleanup_rollback = True
    apply.save_journal(journal)
    original = shutil.rmtree
    def locked(path, *args, **kwargs):
        if apply.paths_equal(str(path), journal.rollback_dir):
            raise PermissionError("scanner still holds old files")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as m:
        m.setattr(apply.shutil, "rmtree", locked)
        assert not apply._cleanup_transaction(journal)
        assert Path(transaction_dir(journal.transaction_id, journal.appdata)).exists()
    assert apply.prune_abandoned_transactions(journal.appdata) == [journal.transaction_id]
    assert not Path(journal.rollback_dir).exists()
    assert (Path(journal.app_dir) / APP_EXE_NAME).read_bytes() == b"new"


def test_recovery_never_overwrites_independent_setup_repair(journal):
    os.rename(journal.app_dir, journal.rollback_dir)
    os.rename(journal.candidate_dir, journal.app_dir)
    (Path(journal.app_dir) / APP_EXE_NAME).write_bytes(b"newer-setup-repair")
    journal.state = TransactionState.NEW_ACTIVE
    apply.save_journal(journal)
    assert apply.recover_transaction(
        journal.transaction_id, journal.appdata, _registration(Path(journal.app_dir)),
    ) == TransactionState.SUPERSEDED
    assert (Path(journal.app_dir) / APP_EXE_NAME).read_bytes() == b"newer-setup-repair"


def test_setup_removes_stale_runtime_and_preserves_data_and_gpu(journal, tmp_path):
    app = Path(journal.app_dir)
    (app / "_internal" / "obsolete.dll").write_bytes(b"stale")
    gpu = app / "_internal" / "nvidia"
    gpu.mkdir()
    (gpu / "legacy.dll").write_bytes(b"gpu")
    data = Path(journal.appdata) / "openwhisper_settings.json"
    data.write_text('{"preference": true}', encoding="utf-8")
    source = _bundle(tmp_path / "release", "2.5.3", b"new")
    setup_update.prepare_setup(str(app), journal.appdata)
    assert not (app / APP_EXE_NAME).exists()
    assert (app / "unins000.exe").exists()
    shutil.copytree(source, app, dirs_exist_ok=True)
    setup_update.finish_setup(str(app))
    assert not (app / "_internal" / "obsolete.dll").exists()
    assert (gpu / "legacy.dll").read_bytes() == b"gpu"
    assert data.read_text(encoding="utf-8") == '{"preference": true}'
    assert not Path(str(app) + ".setup-backup").exists()


def test_setup_failure_restores_complete_previous_runtime(journal):
    app = Path(journal.app_dir)
    setup_update.prepare_setup(str(app), journal.appdata)
    (app / APP_EXE_NAME).write_bytes(b"partial-new")
    with pytest.raises((OSError, apply.UpdateApplyError)):
        setup_update.finish_setup(str(app))
    setup_update.rollback_setup(str(app))
    assert (app / APP_EXE_NAME).read_bytes() == b"old"
    assert (app / "_internal" / "payload.txt").read_bytes() == b"old"
    assert (app / "unins000.exe").read_bytes() == b"uninstaller"


def test_setup_retry_recovers_interrupted_previous_attempt(journal):
    app = Path(journal.app_dir)
    setup_update.prepare_setup(str(app), journal.appdata)
    (app / APP_EXE_NAME).write_bytes(b"partial")
    setup_update.prepare_setup(str(app), journal.appdata)
    setup_update.rollback_setup(str(app))
    assert (app / APP_EXE_NAME).read_bytes() == b"old"


def test_retired_transaction_cannot_run_after_setup(journal, monkeypatch):
    monkeypatch.setattr(apply, "_cleanup_transaction", lambda *args: False)
    setup_update.retire_native_transactions(journal.app_dir, journal.appdata)
    assert apply.load_journal(journal.transaction_id, journal.appdata).state == TransactionState.SUPERSEDED
    assert apply.recover_transaction(journal.transaction_id, journal.appdata) == TransactionState.SUPERSEDED
    assert (Path(journal.app_dir) / APP_EXE_NAME).read_bytes() == b"old"


def test_data_snapshot_includes_committed_wal_and_restores_migration(journal):
    root = Path(journal.appdata)
    settings = root / "openwhisper_settings.json"
    settings.write_text('{"old": true}', encoding="utf-8")
    database = root / "openwhisper.db"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE transcripts (text TEXT)")
        connection.execute("INSERT INTO transcripts VALUES ('keep this')")
        connection.commit()
        update_data.snapshot_data(journal)
    with closing(sqlite3.connect(database)) as connection:
        connection.execute("DROP TABLE transcripts")
        connection.commit()
    settings.write_text('{"new": true}', encoding="utf-8")
    update_data.restore_data(journal)
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT text FROM transcripts").fetchone() == ("keep this",)
    assert settings.read_text(encoding="utf-8") == '{"old": true}'


def test_absent_data_is_restored_as_absent(journal):
    update_data.snapshot_data(journal)
    settings = Path(journal.appdata) / "openwhisper_settings.json"
    settings.write_text("{}")
    update_data.restore_data(journal)
    assert not settings.exists()


def test_modified_snapshot_is_rejected_before_data_changes(journal):
    settings = Path(journal.appdata) / "openwhisper_settings.json"
    settings.write_text('{"old": true}')
    update_data.snapshot_data(journal)
    settings.write_text('{"current": true}')
    backup = Path(transaction_dir(journal.transaction_id, journal.appdata)) / "data" / settings.name
    backup.write_text("tampered")
    with pytest.raises(apply.UpdateApplyError, match="verification"):
        update_data.restore_data(journal)
    assert settings.read_text() == '{"current": true}'


def test_preparing_journal_is_collected_after_parent_dies(journal, monkeypatch):
    journal.state = TransactionState.PREPARING
    apply.save_journal(journal)
    monkeypatch.setattr(apply, "running_app_dir", lambda: journal.app_dir)
    monkeypatch.setattr(apply, "parse_health_token", lambda: None)
    monkeypatch.setattr(apply, "process_identity", lambda pid: ("", 0))
    assert not apply.recover_before_start(journal.appdata)
    assert not Path(journal.candidate_dir).exists()
    assert (Path(journal.app_dir) / APP_EXE_NAME).exists()


def test_startup_dispatches_recovery_before_acquiring_app_lock(journal, monkeypatch):
    journal.state = TransactionState.NEW_ACTIVE
    apply.save_journal(journal)
    monkeypatch.setattr(apply, "running_app_dir", lambda: journal.app_dir)
    monkeypatch.setattr(apply, "parse_health_token", lambda: None)
    assert apply.recover_before_start(journal.appdata)
    assert "--recover" in apply._launch_exe.call_args.args[1]


def test_health_process_exit_fails_without_waiting_for_timeout(journal):
    process = SimpleNamespace(poll=lambda: 1)
    assert not apply._wait_for_health(journal, 30, process=process)


def test_old_updater_floor_requires_setup(journal):
    manifest = apply.load_manifest(str(Path(journal.candidate_dir) / MANIFEST_NAME))
    for version in ("2.4.0", "2.4.8", "2.5.0", "2.5.1"):
        with pytest.raises(apply.UpdateRequiresSetup):
            apply.validate_manifest(manifest, expected_version="2.5.3", current_updater_version=version)


def test_queued_success_after_cancel_cannot_launch():
    from services.application_controller import ApplicationController
    attempt = object()
    controller = SimpleNamespace(
        _update_attempt=attempt, _update_cancel=threading.Event(),
        discard_update_handoff=MagicMock(), ui_controller=MagicMock(),
    )
    controller._update_cancel.set()
    ApplicationController._on_update_download_finished(controller, attempt, "native:" + "a" * 32, "")
    controller.discard_update_handoff.assert_called_once()
    assert controller.ui_controller.on_update_download_finished.call_args.args[0] is None


def test_stale_attempt_cannot_change_current_dialog():
    from services.application_controller import ApplicationController
    controller = SimpleNamespace(
        _update_attempt=object(), _update_cancel=threading.Event(),
        discard_update_handoff=MagicMock(), ui_controller=MagicMock(),
    )
    ApplicationController._on_update_download_finished(controller, object(), None, "old error")
    controller.ui_controller.on_update_download_finished.assert_not_called()


def test_ui_cancel_after_success_is_queued_keeps_app_open():
    from ui_qt.ui_controller import UIController
    controller = SimpleNamespace(
        _update_canceled=True, _update_dialog=MagicMock(), on_update_abandon=MagicMock(),
        exit_for_update=MagicMock(),
    )
    UIController.on_update_download_finished(controller, "native:" + "a" * 32, "")
    controller.on_update_abandon.assert_called_once()
    controller.exit_for_update.assert_not_called()


def test_incompatible_native_payload_automatically_uses_verified_setup(monkeypatch):
    from tests.test_app_update import _release
    release = _release(native=True)
    result = SimpleNamespace(release=release, apply_mode=download.ApplyMode.NATIVE, current_version="2.5.2")
    monkeypatch.setattr(download, "download_release_asset", lambda *args, **kwargs: "archive")
    monkeypatch.setattr(download, "discover_install_registration", lambda: object())
    monkeypatch.setattr(download, "prepare_candidate", MagicMock(side_effect=apply.UpdateRequiresSetup("new topology")))
    installer = MagicMock(return_value="verified-setup.exe")
    monkeypatch.setattr(download, "download_installer", installer)
    assert download.apply_update(result) == "verified-setup.exe"
    installer.assert_called_once()


def test_integrity_failure_does_not_automatically_install_something_else(monkeypatch):
    from tests.test_app_update import _release
    result = SimpleNamespace(release=_release(native=True), apply_mode=download.ApplyMode.NATIVE, current_version="2.5.2")
    monkeypatch.setattr(download, "download_release_asset", lambda *args, **kwargs: "archive")
    monkeypatch.setattr(download, "discover_install_registration", lambda: object())
    monkeypatch.setattr(download, "prepare_candidate", MagicMock(side_effect=apply.UpdateApplyError("hash mismatch")))
    installer = MagicMock()
    monkeypatch.setattr(download, "download_installer", installer)
    with pytest.raises(download.AppUpdateError, match="hash mismatch"):
        download.apply_update(result)
    installer.assert_not_called()


def test_failed_commit_restores_settings_before_old_app_returns(journal):
    settings = Path(journal.appdata) / "openwhisper_settings.json"
    settings.write_text('{"original": true}')
    def broken_startup(_journal):
        settings.write_text('{"incompatible": true}')
        raise apply.UpdateApplyError("failed migration")
    with pytest.raises(apply.UpdateApplyError, match="failed migration"):
        apply.commit_prepared_update(
            journal, wait_parent=False, launch=False,
            registration=_registration(Path(journal.app_dir)),
            hooks={"after_new_active": broken_startup},
        )
    assert settings.read_text() == '{"original": true}'
    assert (Path(journal.app_dir) / APP_EXE_NAME).read_bytes() == b"old"


def test_completed_rollback_never_rewinds_later_user_work(journal):
    settings = Path(journal.appdata) / "openwhisper_settings.json"
    settings.write_text('{"before_update": true}')
    update_data.snapshot_data(journal)
    journal.state = TransactionState.ROLLED_BACK
    apply.save_journal(journal)
    settings.write_text('{"later_user_work": true}')
    assert apply.recover_transaction(journal.transaction_id, journal.appdata) == TransactionState.ROLLED_BACK
    assert settings.read_text() == '{"later_user_work": true}'


def test_new_work_at_download_completion_can_decline_restart():
    from ui_qt.ui_controller import UIController
    controller = SimpleNamespace(
        _update_canceled=False, _update_dialog=MagicMock(),
        on_update_abandon=MagicMock(), exit_for_update=MagicMock(),
        _confirm_update_while_busy=lambda: False,
    )
    UIController.on_update_download_finished(controller, "native:" + "a" * 32, "")
    controller.on_update_abandon.assert_called_once()
    controller.exit_for_update.assert_not_called()


def test_bridge_policy_is_enforced_at_the_minimum_version():
    from services.update_contract import (
        MINIMUM_UPDATER_VERSION,
        is_setup_bridge_release,
        parse_strict_version,
    )
    assert is_setup_bridge_release(MINIMUM_UPDATER_VERSION)
    major, minor, patch = parse_strict_version(MINIMUM_UPDATER_VERSION)
    assert not is_setup_bridge_release(f"{major}.{minor}.{patch + 1}")


def test_frozen_child_environment_drops_parent_runtime(monkeypatch, tmp_path):
    runtime = tmp_path / "old-runtime"
    user_bin = tmp_path / "user-bin"
    monkeypatch.setattr(apply.sys, "_MEIPASS", str(runtime), raising=False)
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", str(runtime))
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("PATH", os.pathsep.join((str(runtime / "Qt"), str(user_bin))))
    environment = apply._environment_without_bootstrap()
    assert "_PYI_APPLICATION_HOME_DIR" not in environment
    assert "_PYI_PARENT_PROCESS_LEVEL" not in environment
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"
    assert environment["PATH"] == str(user_bin)


def test_recovery_collects_failed_tree_from_legacy_journal(journal):
    os.rename(journal.app_dir, journal.rollback_dir)
    os.rename(journal.candidate_dir, journal.app_dir)
    journal.state = TransactionState.NEW_ACTIVE
    journal.cleanup_rollback = False
    apply.save_journal(journal)
    assert apply.recover_transaction(
        journal.transaction_id, journal.appdata, _registration(Path(journal.app_dir)),
    ) == TransactionState.ROLLED_BACK
    assert (Path(journal.app_dir) / APP_EXE_NAME).read_bytes() == b"old"
    assert not Path(journal.app_dir + ".failed-aaaaaaaa").exists()


def test_valid_old_install_is_relaunched_even_when_cleanup_is_locked(journal, monkeypatch):
    shutil.copytree(journal.app_dir, journal.rollback_dir)
    journal.state = TransactionState.OLD_MOVED
    apply.save_journal(journal)
    original = shutil.rmtree
    def locked(path, *args, **kwargs):
        if apply.paths_equal(str(path), journal.rollback_dir):
            raise PermissionError("old duplicate is locked")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(apply.shutil, "rmtree", locked)
    assert apply.recover_transaction(
        journal.transaction_id, journal.appdata, _registration(Path(journal.app_dir)),
    ) == TransactionState.ROLLED_BACK
    assert Path(transaction_dir(journal.transaction_id, journal.appdata)).exists()
    apply._launch_exe.assert_called_once()
