"""Tests for native update extract, manifest, and crash-safe swap."""
import io
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services import app_update_apply as apply_module
from services.app_update_apply import (
    InstallRegistration,
    UpdateApplyError,
    UpdateCanceled,
    UpdateJournal,
    _force_close_pid,
    acquire_named_mutexes,
    build_update_manifest,
    commit_prepared_update,
    consume_apply_error,
    file_sha256,
    load_manifest,
    native_apply_eligible,
    pack_tar_xz,
    parse_health_token,
    prepare_candidate,
    preserve_compat_files,
    process_identity,
    read_manifest_from_tar_xz,
    recover_transaction,
    release_mutexes,
    safe_extract_tar_xz,
    validate_archive_member_name,
    verify_tree_against_manifest,
    write_apply_error,
    write_completion_sentinel,
    write_json_atomic,
    write_manifest_file,
)
from services.update_contract import (
    APP_EXE_NAME,
    MANIFEST_NAME,
    TransactionState,
    UPDATER_EXE_NAME,
    parse_strict_version,
    transaction_dir,
)

TX_ID = "a" * 32
HEALTH_TOKEN = "b" * 32


@pytest.fixture(autouse=True)
def isolated_mutex_names(monkeypatch):
    """Keep the commit path off the mutexes a real installed OpenWhisper holds.

    The names are process-global, so a run alongside the installed app would
    otherwise wait out the full parent timeout on every commit.
    """
    unique = f"OpenWhisper-Test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(apply_module, "APP_MUTEX_NAMES", (f"Local\\{unique}-app",))
    monkeypatch.setattr(
        apply_module, "UPDATE_MUTEX_NAMES", (f"Local\\{unique}-update",)
    )
    monkeypatch.setattr(apply_module, "SETUP_MUTEX_NAME", f"Local\\{unique}-setup")


def _bundle(root: Path, version: str = "2.4.1", payload: bytes = b"new") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / APP_EXE_NAME).write_bytes(payload)
    (root / UPDATER_EXE_NAME).write_bytes(b"helper")
    internal = root / "_internal"
    internal.mkdir(exist_ok=True)
    (internal / "payload.txt").write_bytes(payload)
    write_manifest_file(str(root), version)
    return root


def _registration(app_dir: Path) -> InstallRegistration:
    return InstallRegistration(
        hive="HKCU",
        key_path=r"Software\Microsoft\Windows\CurrentVersion\Uninstall\test",
        install_location=str(app_dir),
        uninstall_string=str(app_dir / "unins000.exe"),
        display_version="2.4.0",
    )


def _tar_with(members, destination: Path) -> Path:
    with tarfile.open(destination, "w:xz") as archive:
        for name, data, typeflag, linkname in members:
            info = tarfile.TarInfo(name=name)
            info.type = typeflag
            if linkname:
                info.linkname = linkname
            if data is not None:
                raw = data if isinstance(data, bytes) else data.encode("utf-8")
                info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
            else:
                archive.addfile(info)
    return destination


class TestStrictVersion:
    def test_rejects_prerelease(self):
        with pytest.raises(ValueError):
            parse_strict_version("2.4.0-rc.1")

    def test_accepts_plain(self):
        assert parse_strict_version("v2.4.1") == (2, 4, 1)


class TestArchiveMemberNames:
    def test_rejects_parent(self):
        with pytest.raises(UpdateApplyError):
            validate_archive_member_name("../OpenWhisper.exe")

    def test_rejects_absolute(self):
        with pytest.raises(UpdateApplyError):
            validate_archive_member_name("/etc/passwd")

    def test_rejects_drive(self):
        with pytest.raises(UpdateApplyError):
            validate_archive_member_name("C:/Windows/notepad.exe")

    def test_rejects_ads(self):
        with pytest.raises(UpdateApplyError):
            validate_archive_member_name("OpenWhisper.exe:zone.identifier")

    def test_rejects_backslash(self):
        with pytest.raises(UpdateApplyError):
            validate_archive_member_name(r"_internal\payload.dll")

    def test_rejects_reserved(self):
        with pytest.raises(UpdateApplyError):
            validate_archive_member_name("CON")

    def test_accepts_internal_file(self):
        assert validate_archive_member_name("_internal/payload.txt") == (
            "_internal/payload.txt"
        )


class TestSafeExtract:
    def test_round_trip_bundle(self, tmp_path):
        src = _bundle(tmp_path / "src")
        (src / "_internal" / "empty.pxd").write_bytes(b"")
        write_manifest_file(str(src), "2.4.1")
        archive = tmp_path / "OpenWhisper-2.4.1-win64.tar.xz"
        pack_tar_xz(str(src), str(archive))
        dest = tmp_path / "out"
        dest.mkdir()
        packed_manifest = read_manifest_from_tar_xz(str(archive))
        safe_extract_tar_xz(str(archive), str(dest), manifest=packed_manifest)
        manifest = load_manifest(str(dest / MANIFEST_NAME))
        verify_tree_against_manifest(str(dest), manifest)

    def test_rejects_symlink(self, tmp_path):
        archive = _tar_with(
            [("evil", None, tarfile.SYMTYPE, "/tmp/x")],
            tmp_path / "bad.tar.xz",
        )
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(UpdateApplyError, match="unsupported"):
            safe_extract_tar_xz(str(archive), str(dest))

    def test_rejects_hardlink(self, tmp_path):
        archive = _tar_with(
            [("evil", None, tarfile.LNKTYPE, "OpenWhisper.exe")],
            tmp_path / "bad.tar.xz",
        )
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(UpdateApplyError, match="unsupported"):
            safe_extract_tar_xz(str(archive), str(dest))

    def test_rejects_duplicate_case(self, tmp_path):
        archive = _tar_with(
            [
                ("File.txt", b"a", tarfile.REGTYPE, ""),
                ("file.txt", b"b", tarfile.REGTYPE, ""),
            ],
            tmp_path / "bad.tar.xz",
        )
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(UpdateApplyError, match="duplicate"):
            safe_extract_tar_xz(str(archive), str(dest))

    def test_rejects_parent_member(self, tmp_path):
        archive = _tar_with(
            [("../escape.exe", b"x", tarfile.REGTYPE, "")],
            tmp_path / "bad.tar.xz",
        )
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(UpdateApplyError, match="unsafe"):
            safe_extract_tar_xz(str(archive), str(dest))


class TestManifest:
    def test_missing_exe_fails(self, tmp_path):
        root = tmp_path / "bad"
        root.mkdir()
        (root / UPDATER_EXE_NAME).write_bytes(b"h")
        (root / "_internal").mkdir()
        (root / "_internal" / "x").write_text("x")
        with pytest.raises(UpdateApplyError, match="OpenWhisper.exe"):
            build_update_manifest(str(root), "2.4.1")

    def test_mismatch_detected(self, tmp_path):
        src = _bundle(tmp_path / "src")
        manifest = load_manifest(str(src / MANIFEST_NAME))
        (src / "_internal" / "payload.txt").write_bytes(b"changed")
        with pytest.raises(UpdateApplyError, match="verification"):
            verify_tree_against_manifest(str(src), manifest)

    def test_unmanaged_top_level_file_forces_setup(self, tmp_path):
        root = _bundle(tmp_path / "src")
        (root / "new-top-level.dll").write_bytes(b"x")
        with pytest.raises(UpdateApplyError, match="unmanaged"):
            build_update_manifest(str(root), "2.4.1")

    def test_transaction_id_cannot_escape_root(self, tmp_path):
        with pytest.raises(ValueError):
            transaction_dir("../../outside", str(tmp_path))


class TestPreserveAndEligibility:
    def test_preserves_uninstaller_and_nvidia(self, tmp_path):
        app = tmp_path / "app"
        cand = tmp_path / "cand"
        _bundle(app, "2.4.0", b"old")
        _bundle(cand, "2.4.1", b"new")
        (app / "unins000.exe").write_bytes(b"unins")
        (app / "unins000.dat").write_bytes(b"data")
        nvidia = app / "_internal" / "nvidia" / "cublas" / "bin"
        nvidia.mkdir(parents=True)
        (nvidia / "cublas64_12.dll").write_bytes(b"gpu")
        preserved, copied = preserve_compat_files(
            str(app), str(cand), _registration(app)
        )
        assert "unins000.exe" in preserved
        assert copied
        assert (cand / "unins000.exe").read_bytes() == b"unins"
        assert (cand / "_internal" / "nvidia" / "cublas" / "bin" / "cublas64_12.dll").exists()

    def test_hklm_is_not_native_eligible(self, tmp_path):
        app = tmp_path / "app"
        app.mkdir()
        (app / UPDATER_EXE_NAME).write_bytes(b"h")
        reg = InstallRegistration(
            hive="HKLM",
            key_path="x",
            install_location=str(app),
            uninstall_string="unins000.exe",
            display_version="2.4.0",
        )
        assert not native_apply_eligible(
            registration=reg, app_dir=str(app), helper_present=True
        )

    def test_missing_registered_uninstaller_is_not_native_eligible(self, tmp_path):
        app = _bundle(tmp_path / "app")
        with patch("services.app_update_apply.is_process_elevated", return_value=False):
            assert not native_apply_eligible(
                registration=_registration(app),
                app_dir=str(app),
                helper_present=True,
            )

    def test_elevated_process_is_not_native_eligible(self, tmp_path):
        app = _bundle(tmp_path / "app")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        with patch("services.app_update_apply.is_process_elevated", return_value=True):
            assert not native_apply_eligible(
                registration=_registration(app),
                app_dir=str(app),
                helper_present=True,
            )


class TestCommitAndRecover:
    @pytest.fixture(autouse=True)
    def _mock_arp_writes(self):
        with patch(
            "services.app_update_apply.update_arp_after_success"
        ), patch(
            "services.app_update_apply.restore_arp_after_rollback"
        ), patch(
            "services.app_update_apply._set_runonce"
        ), patch("services.app_update_apply._clear_runonce"):
            yield

    def _journal(self, tmp_path: Path, app: Path, candidate: Path) -> UpdateJournal:
        appdata = tmp_path / "appdata"
        tx = appdata / "updates" / "tx" / TX_ID
        tx.mkdir(parents=True)
        (tx / UPDATER_EXE_NAME).write_bytes(b"helper")
        uninstaller = candidate / "unins000.exe"
        uninstaller.write_bytes((app / "unins000.exe").read_bytes())
        return UpdateJournal(
            transaction_id=TX_ID,
            state=TransactionState.PREPARED,
            app_dir=str(app),
            candidate_dir=str(candidate),
            rollback_dir=str(app) + ".old-aaaaaaaa",
            old_version="2.4.0",
            new_version="2.4.1",
            old_display_version="2.4.0",
            old_estimated_size_kb=None,
            old_install_date="20260825",
            old_exe_sha256=file_sha256(str(app / APP_EXE_NAME)),
            new_exe_sha256=file_sha256(str(candidate / APP_EXE_NAME)),
            parent_pid=0,
            parent_exe="",
            parent_creation_time=0,
            health_token=HEALTH_TOKEN,
            uninstaller_files=["unins000.exe"],
            compatibility_files={
                "unins000.exe": {
                    "size": uninstaller.stat().st_size,
                    "sha256": file_sha256(str(uninstaller)),
                }
            },
            appdata=str(appdata),
        )

    def test_swap_and_cleanup(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new")
        write_completion_sentinel(str(candidate), "2.4.1")
        journal = self._journal(tmp_path, app, candidate)
        write_json_atomic(
            str(Path(journal.appdata) / "updates" / "tx" / TX_ID / "journal.json"),
            journal.to_dict(),
        )
        state = commit_prepared_update(
            journal,
            wait_parent=False,
            launch=False,
            registration=_registration(app),
        )
        assert state == TransactionState.HEALTHY
        assert (tmp_path / "OpenWhisper" / APP_EXE_NAME).read_bytes() == b"new"
        assert not (tmp_path / "OpenWhisper.new-aaaaaaaa").exists()
        assert not Path(str(app) + ".old-aaaaaaaa").exists()

    def test_injected_failure_after_old_moved_rolls_back(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new")
        write_completion_sentinel(str(candidate), "2.4.1")
        journal = self._journal(tmp_path, app, candidate)
        write_json_atomic(
            str(Path(journal.appdata) / "updates" / "tx" / TX_ID / "journal.json"),
            journal.to_dict(),
        )

        def boom(_journal):
            raise UpdateApplyError("injected crash")

        with pytest.raises(UpdateApplyError, match="injected"):
            commit_prepared_update(
                journal,
                wait_parent=False,
                launch=False,
                registration=_registration(app),
                hooks={"after_old_moved": boom},
            )
        assert (tmp_path / "OpenWhisper" / APP_EXE_NAME).read_bytes() == b"old"
        error = consume_apply_error(str(Path(journal.appdata)))
        assert error and "injected" in error

    def test_recover_prepared_abandons_candidate(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new")
        journal = self._journal(tmp_path, app, candidate)
        write_json_atomic(
            str(Path(journal.appdata) / "updates" / "tx" / TX_ID / "journal.json"),
            journal.to_dict(),
        )
        with patch("services.app_update_apply._launch_exe"):
            recover_transaction(
                TX_ID, journal.appdata, registration=_registration(app)
            )
        assert (tmp_path / "OpenWhisper" / APP_EXE_NAME).read_bytes() == b"old"
        assert not candidate.exists()

    def test_recover_prepared_after_old_rename_restores_rollback(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(
            tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new"
        )
        journal = self._journal(tmp_path, app, candidate)
        write_json_atomic(
            str(
                Path(journal.appdata)
                / "updates"
                / "tx"
                / TX_ID
                / "journal.json"
            ),
            journal.to_dict(),
        )
        os.replace(journal.app_dir, journal.rollback_dir)
        with patch("services.app_update_apply._launch_exe"):
            state = recover_transaction(
                TX_ID, journal.appdata, registration=_registration(app)
            )
        assert state == TransactionState.ROLLED_BACK
        assert (Path(journal.app_dir) / APP_EXE_NAME).read_bytes() == b"old"

    def test_recover_old_moved_after_candidate_rename_rolls_back(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(
            tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new"
        )
        journal = self._journal(tmp_path, app, candidate)
        journal.state = TransactionState.OLD_MOVED
        write_json_atomic(
            str(
                Path(journal.appdata)
                / "updates"
                / "tx"
                / TX_ID
                / "journal.json"
            ),
            journal.to_dict(),
        )
        os.replace(journal.app_dir, journal.rollback_dir)
        os.replace(journal.candidate_dir, journal.app_dir)
        with patch("services.app_update_apply._launch_exe"):
            state = recover_transaction(
                TX_ID, journal.appdata, registration=_registration(app)
            )
        assert state == TransactionState.ROLLED_BACK
        assert (Path(journal.app_dir) / APP_EXE_NAME).read_bytes() == b"old"

    def test_empty_health_file_is_not_success(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(
            tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new"
        )
        journal = self._journal(tmp_path, app, candidate)
        journal.state = TransactionState.NEW_ACTIVE
        write_json_atomic(
            str(
                Path(journal.appdata)
                / "updates"
                / "tx"
                / TX_ID
                / "journal.json"
            ),
            journal.to_dict(),
        )
        os.replace(journal.app_dir, journal.rollback_dir)
        os.replace(journal.candidate_dir, journal.app_dir)
        health = (
            Path(journal.appdata) / "updates" / "tx" / TX_ID / "healthy"
        )
        health.write_text("", encoding="utf-8")
        with patch("services.app_update_apply._launch_exe"):
            state = recover_transaction(
                TX_ID, journal.appdata, registration=_registration(app)
            )
        assert state == TransactionState.ROLLED_BACK
        assert (Path(journal.app_dir) / APP_EXE_NAME).read_bytes() == b"old"

    def test_tampered_candidate_is_rejected_before_swap(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(
            tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new"
        )
        write_completion_sentinel(str(candidate), "2.4.1")
        journal = self._journal(tmp_path, app, candidate)
        write_json_atomic(
            str(
                Path(journal.appdata)
                / "updates"
                / "tx"
                / TX_ID
                / "journal.json"
            ),
            journal.to_dict(),
        )
        (candidate / APP_EXE_NAME).write_bytes(b"tampered")
        with pytest.raises(UpdateApplyError, match="verification|changed"):
            commit_prepared_update(
                journal,
                wait_parent=False,
                launch=False,
                registration=_registration(app),
            )
        assert (app / APP_EXE_NAME).read_bytes() == b"old"

    def test_existing_rollback_tree_is_never_activated(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(
            tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new"
        )
        write_completion_sentinel(str(candidate), "2.4.1")
        journal = self._journal(tmp_path, app, candidate)
        rollback = Path(journal.rollback_dir)
        rollback.mkdir()
        (rollback / APP_EXE_NAME).write_bytes(b"untrusted")
        with pytest.raises(UpdateApplyError, match="rollback tree"):
            commit_prepared_update(
                journal,
                wait_parent=False,
                launch=False,
                registration=_registration(app),
            )
        assert (app / APP_EXE_NAME).read_bytes() == b"old"
        assert (rollback / APP_EXE_NAME).read_bytes() == b"untrusted"

    def test_damaged_healthy_tree_is_rolled_back(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(
            tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new"
        )
        write_completion_sentinel(str(candidate), "2.4.1")
        journal = self._journal(tmp_path, app, candidate)
        journal.state = TransactionState.NEW_ACTIVE
        write_json_atomic(
            str(
                Path(journal.appdata)
                / "updates"
                / "tx"
                / TX_ID
                / "journal.json"
            ),
            journal.to_dict(),
        )
        os.replace(journal.app_dir, journal.rollback_dir)
        os.replace(journal.candidate_dir, journal.app_dir)
        (
            Path(journal.app_dir) / "_internal" / "payload.txt"
        ).write_bytes(b"damaged")
        (
            Path(journal.appdata) / "updates" / "tx" / TX_ID / "healthy"
        ).write_text(HEALTH_TOKEN, encoding="utf-8")
        with patch("services.app_update_apply._launch_exe"):
            state = recover_transaction(
                TX_ID, journal.appdata, registration=_registration(app)
            )
        assert state == TransactionState.ROLLED_BACK
        assert (app / APP_EXE_NAME).read_bytes() == b"old"

    def test_unconfirmed_new_tree_without_rollback_keeps_recovery(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(
            tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new"
        )
        write_completion_sentinel(str(candidate), "2.4.1")
        journal = self._journal(tmp_path, app, candidate)
        journal.state = TransactionState.NEW_ACTIVE
        write_json_atomic(
            str(
                Path(journal.appdata)
                / "updates"
                / "tx"
                / TX_ID
                / "journal.json"
            ),
            journal.to_dict(),
        )
        shutil.rmtree(journal.app_dir)
        os.replace(journal.candidate_dir, journal.app_dir)
        with patch("services.app_update_apply._clear_runonce") as clear:
            with pytest.raises(UpdateApplyError, match="validated rollback"):
                recover_transaction(
                    TX_ID, journal.appdata, registration=_registration(app)
                )
        clear.assert_not_called()


class TestLiveParent:
    """The app is still alive when the helper wants to swap the install."""

    @pytest.fixture(autouse=True)
    def _mock_arp_writes(self):
        with patch(
            "services.app_update_apply.update_arp_after_success"
        ), patch(
            "services.app_update_apply.restore_arp_after_rollback"
        ), patch(
            "services.app_update_apply._set_runonce"
        ), patch("services.app_update_apply._clear_runonce"):
            yield

    def test_running_parent_is_not_reported_as_a_failed_rollback(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new")
        write_completion_sentinel(str(candidate), "2.4.1")
        journal = TestCommitAndRecover()._journal(tmp_path, app, candidate)
        journal.parent_pid = os.getpid()
        write_json_atomic(
            str(Path(journal.appdata) / "updates" / "tx" / TX_ID / "journal.json"),
            journal.to_dict(),
        )

        app_locks = acquire_named_mutexes(apply_module.APP_MUTEX_NAMES, 0.0)
        try:
            with pytest.raises(UpdateApplyError) as caught:
                commit_prepared_update(
                    journal,
                    launch=False,
                    parent_timeout_s=0.2,
                    registration=_registration(app),
                )
        finally:
            release_mutexes(app_locks)

        assert "did not close" in str(caught.value)
        assert "rollback failed" not in str(caught.value)
        assert (app / APP_EXE_NAME).read_bytes() == b"old"
        assert not candidate.exists()
        assert not Path(transaction_dir(TX_ID, journal.appdata)).exists()
        error = consume_apply_error(journal.appdata)
        assert error and "did not close" in error

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only commit path")
    def test_parent_that_never_exits_is_closed_and_the_update_lands(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        candidate = _bundle(tmp_path / "OpenWhisper.new-aaaaaaaa", "2.4.1", b"new")
        write_completion_sentinel(str(candidate), "2.4.1")
        journal = TestCommitAndRecover()._journal(tmp_path, app, candidate)
        parent = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"]
        )
        try:
            parent_exe, parent_creation = process_identity(parent.pid)
            assert parent_exe and parent_creation
            journal.parent_pid = parent.pid
            journal.parent_exe = parent_exe
            journal.parent_creation_time = parent_creation
            write_json_atomic(
                str(Path(journal.appdata) / "updates" / "tx" / TX_ID / "journal.json"),
                journal.to_dict(),
            )
            state = commit_prepared_update(
                journal,
                launch=False,
                parent_timeout_s=0.3,
                registration=_registration(app),
            )
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=30)

        assert state == TransactionState.HEALTHY
        assert (app / APP_EXE_NAME).read_bytes() == b"new"

    def test_this_process_is_never_its_own_victim(self):
        assert _force_close_pid(
            os.getpid(),
            expected_exe=sys.executable,
            expected_creation_time=process_identity(os.getpid())[1] or 1,
        )

    @pytest.mark.skipif(sys.platform != "win32", reason="needs process identity")
    def test_an_unidentified_parent_is_never_terminated(self):
        parent = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"]
        )
        try:
            # Without a recorded image path and creation time there is no proof
            # this pid is the process that handed off, so it survives and the
            # commit reports the failure instead.
            assert not _force_close_pid(
                parent.pid, expected_exe="", expected_creation_time=0
            )
            assert parent.poll() is None
        finally:
            parent.kill()
            parent.wait(timeout=30)

    @pytest.mark.skipif(sys.platform != "win32", reason="needs process identity")
    def test_a_recycled_pid_is_not_the_victim(self):
        parent = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"]
        )
        try:
            _, creation = process_identity(parent.pid)
            assert _force_close_pid(
                parent.pid,
                expected_exe=str(Path(sys.executable).parent / "not-openwhisper.exe"),
                expected_creation_time=creation,
            )
            assert parent.poll() is None
        finally:
            parent.kill()
            parent.wait(timeout=30)


class TestPrepareCancel:
    def test_cancel_after_extraction_still_cancels(self, tmp_path):
        app = _bundle(tmp_path / "OpenWhisper", "2.4.0", b"old")
        (app / "unins000.exe").write_bytes(b"uninstaller")
        appdata = tmp_path / "appdata"
        updates = appdata / "updates"
        updates.mkdir(parents=True)
        source = _bundle(tmp_path / "src", "2.4.1", b"new")
        archive = updates / "OpenWhisper-2.4.1-win64.tar.xz"
        pack_tar_xz(str(source), str(archive))

        cancel = threading.Event()
        total_members = read_manifest_from_tar_xz(str(archive))["member_count"]

        def progress(phase, done, total):
            # Fires on the last archive member, after the extract loop has run
            # its final cancellation check.
            if phase == "extracting" and done >= total_members:
                cancel.set()

        with patch(
            "services.app_update_apply.native_apply_eligible", return_value=True
        ):
            with pytest.raises(UpdateCanceled):
                prepare_candidate(
                    str(archive),
                    release_version="2.4.1",
                    current_version="2.4.0",
                    app_dir=str(app),
                    registration=_registration(app),
                    progress=progress,
                    cancel=cancel,
                    appdata=str(appdata),
                    parent_pid=os.getpid(),
                )

        assert not list(tmp_path.glob("OpenWhisper.new-*"))
        assert not list((updates / "tx").glob("*"))


class TestLockedFiles:
    """Windows refuses a file another process still holds, briefly and often."""

    @staticmethod
    def _sharing_violation() -> OSError:
        exc = OSError(13, "The process cannot access the file")
        exc.winerror = 32
        return exc

    def test_a_move_that_is_blocked_for_a_moment_still_happens(self, tmp_path):
        source = tmp_path / "source"
        source.mkdir()
        destination = tmp_path / "destination"
        attempts = []
        real_move = apply_module._move_path_write_through

        def flaky(src, dst, *, replace_existing):
            attempts.append(src)
            if len(attempts) < 3:
                raise self._sharing_violation()
            real_move(src, dst, replace_existing=replace_existing)

        with patch.object(apply_module, "_LOCK_POLL_S", 0.01), patch.object(
            apply_module, "_move_path_write_through", flaky
        ):
            apply_module._rename_dir(str(source), str(destination))

        assert len(attempts) == 3
        assert destination.is_dir()

    def test_a_lock_that_never_clears_still_fails(self, tmp_path):
        with patch.object(apply_module, "_LOCK_POLL_S", 0.01):
            with pytest.raises(OSError) as caught:
                apply_module._retry_while_locked(
                    "target",
                    lambda: (_ for _ in ()).throw(self._sharing_violation()),
                    timeout_s=0.05,
                )
        assert caught.value.winerror == 32

    def test_an_ordinary_error_is_not_retried(self):
        attempts = []

        def missing():
            attempts.append(1)
            raise FileNotFoundError(2, "nope")

        with pytest.raises(FileNotFoundError):
            apply_module._retry_while_locked("target", missing)
        assert len(attempts) == 1

    def test_hashing_the_app_it_just_closed_waits_for_the_handle(self, tmp_path):
        target = tmp_path / "OpenWhisper.exe"
        target.write_bytes(b"payload")
        attempts = []
        real_open = open

        def flaky(*args, **kwargs):
            attempts.append(args)
            if len(attempts) < 2:
                raise self._sharing_violation()
            return real_open(*args, **kwargs)

        with patch.object(apply_module, "_LOCK_POLL_S", 0.01), patch(
            "builtins.open", flaky
        ):
            digest = file_sha256(str(target))

        assert len(attempts) == 2
        assert digest == file_sha256(str(target))

    def test_cleanup_waits_out_the_updater_that_asked_for_it(self, tmp_path):
        appdata = tmp_path / "appdata"
        tx = Path(transaction_dir(TX_ID, str(appdata)))
        tx.mkdir(parents=True)
        (tx / UPDATER_EXE_NAME).write_bytes(b"helper")
        write_json_atomic(
            str(tx / "journal.json"),
            {"transaction_id": TX_ID, "state": TransactionState.ROLLED_BACK},
        )
        attempts = []
        real_rmtree = shutil.rmtree

        def flaky(path, *args, **kwargs):
            attempts.append(path)
            if len(attempts) < 3:
                raise self._sharing_violation()
            real_rmtree(path, *args, **kwargs)

        journal = SimpleNamespace(
            transaction_id=TX_ID,
            state=TransactionState.ROLLED_BACK,
            appdata=str(appdata),
        )
        with patch.object(apply_module, "_LOCK_POLL_S", 0.01), patch.object(
            apply_module, "load_journal", return_value=journal
        ), patch.object(apply_module, "_wait_for_pid"), patch.object(
            apply_module.shutil, "rmtree", flaky
        ):
            apply_module.cleanup_transaction_after_parent(TX_ID, 4321, str(appdata))

        assert len(attempts) == 3
        assert not tx.exists()


class TestAbandonedTransactions:
    def test_a_transaction_without_a_journal_is_collected(self, tmp_path):
        appdata = tmp_path / "appdata"
        stale = Path(transaction_dir("c" * 32, str(appdata)))
        stale.mkdir(parents=True)
        (stale / UPDATER_EXE_NAME).write_bytes(b"helper")
        live = Path(transaction_dir(TX_ID, str(appdata)))
        live.mkdir(parents=True)
        write_json_atomic(str(live / "journal.json"), {"transaction_id": TX_ID})

        removed = apply_module.prune_abandoned_transactions(str(appdata))

        assert removed == ["c" * 32]
        assert not stale.exists()
        assert live.is_dir()

    def test_a_missing_transactions_directory_is_not_an_error(self, tmp_path):
        assert apply_module.prune_abandoned_transactions(str(tmp_path)) == []

    @pytest.mark.parametrize(
        "state", [TransactionState.HEALTHY, TransactionState.ROLLED_BACK]
    )
    def test_a_finished_transaction_is_collected(self, tmp_path, state):
        """Cleanup gives up on an updater that sits in its error dialog."""
        appdata = tmp_path / "appdata"
        finished = Path(transaction_dir("d" * 32, str(appdata)))
        finished.mkdir(parents=True)
        (finished / UPDATER_EXE_NAME).write_bytes(b"helper")
        write_json_atomic(
            str(finished / "journal.json"),
            {"transaction_id": "d" * 32, "state": state},
        )

        removed = apply_module.prune_abandoned_transactions(str(appdata))

        assert removed == ["d" * 32]
        assert not finished.exists()

    @pytest.mark.parametrize(
        "state",
        [
            TransactionState.PREPARED,
            TransactionState.OLD_MOVED,
            TransactionState.NEW_ACTIVE,
        ],
    )
    def test_a_transaction_recovery_may_still_need_is_kept(self, tmp_path, state):
        appdata = tmp_path / "appdata"
        live = Path(transaction_dir("e" * 32, str(appdata)))
        live.mkdir(parents=True)
        write_json_atomic(
            str(live / "journal.json"), {"transaction_id": "e" * 32, "state": state}
        )

        assert apply_module.prune_abandoned_transactions(str(appdata)) == []
        assert live.is_dir()

    def test_an_unreadable_journal_is_left_for_recovery(self, tmp_path):
        appdata = tmp_path / "appdata"
        odd = Path(transaction_dir("f" * 32, str(appdata)))
        odd.mkdir(parents=True)
        (odd / "journal.json").write_text("{not json", encoding="utf-8")

        assert apply_module.prune_abandoned_transactions(str(appdata)) == []
        assert odd.is_dir()


class TestLaunchDirectory:
    def test_a_source_run_changes_directory(self, tmp_path, monkeypatch):
        install = tmp_path / "programs" / "openwhisper"
        install.mkdir(parents=True)
        appdata = tmp_path / "appdata"
        monkeypatch.chdir(install)
        monkeypatch.setattr(apply_module.sys, "frozen", False, raising=False)

        assert apply_module.leave_launch_directory([], str(appdata)) is False

        expected = Path(apply_module.updates_root(str(appdata)))
        assert Path(os.getcwd()).resolve() == expected.resolve()

    def test_a_frozen_helper_restarts_itself_from_the_updates_root(
        self, tmp_path, monkeypatch
    ):
        """A onefile bootloader parent keeps the launch directory; only a new
        process pair started elsewhere releases it."""
        install = tmp_path / "programs" / "openwhisper"
        install.mkdir(parents=True)
        appdata = tmp_path / "appdata"
        monkeypatch.chdir(install)
        monkeypatch.setattr(apply_module.sys, "frozen", True, raising=False)
        monkeypatch.setattr(apply_module.sys, "executable", str(install / "OpenWhisperUpdater.exe"))
        monkeypatch.delenv(apply_module._RELAUNCHED_ENV, raising=False)
        monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", str(tmp_path / "_MEI1"))
        monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
        monkeypatch.setenv("_MEIPASS2", str(tmp_path / "_MEI1"))
        monkeypatch.setenv("LOCALAPPDATA_KEEP", "kept")
        launches = []
        monkeypatch.setattr(
            apply_module,
            "_launch_exe",
            lambda path, args, **kwargs: launches.append((path, args, kwargs)),
        )

        argv = ["--transaction-id", TX_ID]
        assert apply_module.leave_launch_directory(argv, str(appdata)) is True

        (path, args, kwargs), = launches
        assert path == str(install / "OpenWhisperUpdater.exe")
        assert args == argv
        assert Path(kwargs["cwd"]).resolve() == Path(
            apply_module.updates_root(str(appdata))
        ).resolve()
        assert kwargs["env"][apply_module._RELAUNCHED_ENV] == "1"
        # Inherited bootstrap variables would make the copy run inside this
        # process's extraction, and this process's parent would outlive it.
        assert not any(key.startswith("_PYI_") for key in kwargs["env"])
        assert "_MEIPASS2" not in kwargs["env"]
        assert kwargs["env"]["LOCALAPPDATA_KEEP"] == "kept"
        assert Path(os.getcwd()).resolve() == install.resolve()

    def test_the_restarted_copy_does_not_restart_again(self, tmp_path, monkeypatch):
        install = tmp_path / "programs" / "openwhisper"
        install.mkdir(parents=True)
        appdata = tmp_path / "appdata"
        monkeypatch.chdir(install)
        monkeypatch.setattr(apply_module.sys, "frozen", True, raising=False)
        monkeypatch.setenv(apply_module._RELAUNCHED_ENV, "1")
        monkeypatch.setattr(
            apply_module, "_launch_exe", lambda *a, **k: pytest.fail("relaunched twice")
        )

        assert apply_module.leave_launch_directory([], str(appdata)) is False
        assert Path(os.getcwd()).resolve() == Path(
            apply_module.updates_root(str(appdata))
        ).resolve()

    def test_a_helper_already_under_the_updates_root_is_left_alone(
        self, tmp_path, monkeypatch
    ):
        appdata = tmp_path / "appdata"
        tx = Path(transaction_dir(TX_ID, str(appdata)))
        tx.mkdir(parents=True)
        monkeypatch.chdir(tx)
        monkeypatch.setattr(apply_module.sys, "frozen", True, raising=False)
        monkeypatch.setattr(
            apply_module, "_launch_exe", lambda *a, **k: pytest.fail("relaunched")
        )

        assert apply_module.leave_launch_directory([], str(appdata)) is False
        assert Path(os.getcwd()).resolve() == tx.resolve()
