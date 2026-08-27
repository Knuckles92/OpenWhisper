"""Tests for native update extract, manifest, and crash-safe swap."""
import io
import os
import shutil
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from services.app_update_apply import (
    InstallRegistration,
    UpdateApplyError,
    UpdateJournal,
    build_update_manifest,
    commit_prepared_update,
    consume_apply_error,
    file_sha256,
    load_manifest,
    native_apply_eligible,
    pack_tar_xz,
    parse_health_token,
    preserve_compat_files,
    read_manifest_from_tar_xz,
    recover_transaction,
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


class TestHealthAndError:
    def test_parse_health_token(self):
        assert parse_health_token(["--update-health", "abc"]) == "abc"
        assert parse_health_token(["--other"]) is None

    def test_apply_error_is_bounded(self, tmp_path):
        write_apply_error("x" * 8000, str(tmp_path))
        text = consume_apply_error(str(tmp_path))
        assert text is not None
        assert len(text) <= 2000
        assert consume_apply_error(str(tmp_path)) is None
