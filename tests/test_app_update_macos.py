"""In-place macOS updates: staging, the swap helper, health and rollback."""
from __future__ import annotations

import builtins
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from services import app_update_macos as mac
from services.update_contract import HEALTH_ARG, PARENT_PID_ARG, TRANSACTION_ARG, apply_error_path

BUNDLE_ID = "tech.fiorilabs.openwhisper"


def make_bundle(path: Path, version: str, bundle_id: str = BUNDLE_ID) -> Path:
    contents = path / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    executable = contents / "MacOS" / "OpenWhisper"
    executable.write_text(f"#!/bin/sh\necho {version}\n")
    executable.chmod(0o755)
    with open(contents / "Info.plist", "wb") as handle:
        plistlib.dump({
            "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": version,
            "CFBundleExecutable": "OpenWhisper",
        }, handle)
    return path


def version_of(bundle: Path) -> str:
    return mac.bundle_info(str(bundle))["CFBundleShortVersionString"]


@pytest.fixture
def signed(monkeypatch):
    """Every bundle verifies, ad-hoc signed (no Team ID)."""
    monkeypatch.setattr(mac, "signature_intact", lambda bundle: True)
    monkeypatch.setattr(mac, "team_identifier", lambda bundle: None)


@pytest.fixture
def image(tmp_path, monkeypatch):
    """A fake disk image whose mount holds OpenWhisper.app 2.7.0."""
    source = make_bundle(tmp_path / "image" / "OpenWhisper.app", "2.7.0")

    def attach(_image, mountpoint):
        shutil.copytree(source, Path(mountpoint) / "OpenWhisper.app", symlinks=True)

    def run(command, timeout=None):
        if command[0] == "/usr/bin/ditto":
            shutil.copytree(command[1], command[2], symlinks=True)
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command {command}")

    monkeypatch.setattr(mac, "_attach", attach)
    monkeypatch.setattr(mac, "_detach", lambda mountpoint: shutil.rmtree(mountpoint))
    monkeypatch.setattr(mac, "_run", run)
    return source


@pytest.fixture
def installed(tmp_path):
    return make_bundle(tmp_path / "Applications" / "OpenWhisper.app", "2.6.13")


def test_result_round_trip_rejects_paths():
    token = "a" * 32
    assert mac.decode_result(mac.encode_result(token)) == token
    for raw in (None, "", "/tmp/update.dmg", "macos:../x", "native:" + token):
        assert mac.decode_result(raw) is None


def test_running_bundle_finds_the_app_around_its_executable(installed, tmp_path):
    executable = installed / "Contents" / "MacOS" / "OpenWhisper"
    assert mac.running_bundle(str(executable)) == str(installed)
    loose = tmp_path / "bin" / "OpenWhisper"
    loose.parent.mkdir()
    loose.write_text("")
    assert mac.running_bundle(str(loose)) is None


def test_blockers_send_unreplaceable_copies_to_a_manual_install(installed, monkeypatch):
    assert mac.in_place_blocker(str(installed)) is None
    assert "not an installed app" in mac.in_place_blocker(None)
    translocated = "/private/var/folders/x/AppTranslocation/ABC/d/OpenWhisper.app"
    assert "App Translocation" in mac.in_place_blocker(translocated)
    real_stat = os.stat
    monkeypatch.setattr(mac.os, "stat", lambda path, *a, **k: SimpleNamespace(st_uid=os.getuid() + 1)
                        if str(path) == str(installed) else real_stat(path, *a, **k))
    assert "another account" in mac.in_place_blocker(str(installed))


def test_candidate_must_be_this_app_at_the_release_version(tmp_path, monkeypatch, signed):
    good = make_bundle(tmp_path / "good.app", "2.7.0")
    mac.verify_candidate(str(good), version="v2.7.0", bundle_id=BUNDLE_ID, team_id=None)
    with pytest.raises(mac.MacUpdateError, match="different app"):
        mac.verify_candidate(str(make_bundle(tmp_path / "other.app", "2.7.0", "com.example")),
                             version="2.7.0", bundle_id=BUNDLE_ID, team_id=None)
    with pytest.raises(mac.MacUpdateError, match="version"):
        mac.verify_candidate(str(good), version="2.7.1", bundle_id=BUNDLE_ID, team_id=None)
    monkeypatch.setattr(mac, "team_identifier", lambda bundle: "OTHERTEAM1")
    with pytest.raises(mac.MacUpdateError, match="same developer"):
        mac.verify_candidate(str(good), version="2.7.0", bundle_id=BUNDLE_ID, team_id="ABCDE12345")
    monkeypatch.setattr(mac, "signature_intact", lambda bundle: False)
    with pytest.raises(mac.MacUpdateError, match="signature"):
        mac.verify_candidate(str(good), version="2.7.0", bundle_id=BUNDLE_ID, team_id=None)


def test_prepare_stages_a_verified_copy_beside_the_app(tmp_path, installed, image, signed):
    appdata = str(tmp_path / "data")
    handoff = mac.prepare_in_place_update(
        "/tmp/OpenWhisper-2.7.0-macos-arm64.dmg", "2.7.0", "2.6.13",
        appdata=appdata, bundle=str(installed),
    )
    transaction = mac.decode_result(handoff)
    journal = mac.load_journal(transaction, appdata)
    assert journal.state == mac.State.PREPARED
    assert Path(journal.staged_path).parent == installed.parent
    assert Path(journal.staged_path).name.startswith(".OpenWhisper-update-")
    assert version_of(Path(journal.staged_path)) == "2.7.0"
    assert version_of(installed) == "2.6.13"
    assert len(journal.health_token) == 32
    assert not (Path(mac.transaction_dir(transaction, appdata)) / "mount").exists()
    mac.discard(transaction, appdata)
    assert not Path(journal.staged_path).exists()
    assert not Path(mac.transaction_dir(transaction, appdata)).exists()


def test_prepare_returns_the_image_when_the_app_cannot_replace_itself(tmp_path, image):
    assert mac.prepare_in_place_update(
        "/tmp/update.dmg", "2.7.0", "2.6.13", appdata=str(tmp_path), bundle=None,
    ) == "/tmp/update.dmg"


def test_prepare_leaves_nothing_behind_when_verification_fails(tmp_path, installed, image, monkeypatch):
    monkeypatch.setattr(mac, "signature_intact", lambda bundle: False)
    monkeypatch.setattr(mac, "team_identifier", lambda bundle: None)
    with pytest.raises(mac.MacUpdateError, match="signature"):
        mac.prepare_in_place_update("/tmp/u.dmg", "2.7.0", "2.6.13",
                                    appdata=str(tmp_path / "data"), bundle=str(installed))
    assert sorted(p.name for p in installed.parent.iterdir()) == ["OpenWhisper.app"]
    assert not any(Path(mac.transactions_root(str(tmp_path / "data"))).iterdir())


def test_prepare_stops_when_canceled(tmp_path, installed, image, signed):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(mac.MacUpdateError, match="canceled"):
        mac.prepare_in_place_update("/tmp/u.dmg", "2.7.0", "2.6.13", cancel=cancel,
                                    appdata=str(tmp_path / "data"), bundle=str(installed))
    assert sorted(p.name for p in installed.parent.iterdir()) == ["OpenWhisper.app"]


class FakeProcess:
    def __init__(self, exit_code=None):
        self.exit_code = exit_code
        self.terminated = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = -15

    def wait(self, timeout=None):
        return self.exit_code

    def kill(self):
        self.exit_code = -9


def three_rename_swap(first, second):
    parking = first + ".swap"
    os.rename(first, parking)
    os.rename(second, first)
    os.rename(parking, second)


def staged(tmp_path, installed, image, *, parent_pid):
    appdata = str(tmp_path / "data")
    transaction = mac.decode_result(mac.prepare_in_place_update(
        "/tmp/u.dmg", "2.7.0", "2.6.13", appdata=appdata, bundle=str(installed),
    ))
    journal = mac.load_journal(transaction, appdata)
    journal.parent_pid = parent_pid
    mac.save_journal(journal, appdata)
    return appdata, journal


def run_guarded(argv, appdata, launch, swap=three_rename_swap, **options):
    """Run the helper, recording every import made after the first swap."""
    real_import = builtins.__import__
    late = []

    def guard(name, *args, **kwargs):
        late.append(name)
        return real_import(name, *args, **kwargs)

    def swapping(first, second):
        swap(first, second)
        builtins.__import__ = guard

    try:
        code = mac.run_helper(argv, appdata, swap=swapping, launch=launch,
                              environment={}, parent_wait_s=5, health_wait_s=2, **options)
    finally:
        builtins.__import__ = real_import
    return code, late


def helper_args(journal):
    return [TRANSACTION_ARG, journal.transaction_id, PARENT_PID_ARG, str(journal.parent_pid)]


def test_helper_swaps_in_the_new_version_and_drops_the_old_one_once_healthy(
        tmp_path, installed, image, signed):
    appdata, journal = staged(tmp_path, installed, image, parent_pid=999_999_999)
    launches = []

    def launch(executable, arguments, environment):
        launches.append((executable, arguments))
        assert version_of(installed) == "2.7.0"
        assert mac.write_health_acknowledgement(arguments[1], appdata, bundle=str(installed))
        return FakeProcess()

    code, late_imports = run_guarded(helper_args(journal), appdata, launch)
    assert code == 0
    assert late_imports == []
    assert launches == [(str(installed / "Contents/MacOS/OpenWhisper"), [HEALTH_ARG, journal.health_token])]
    assert version_of(installed) == "2.7.0"
    assert not Path(journal.staged_path).exists()
    assert not Path(mac.transaction_dir(journal.transaction_id, appdata)).exists()


def test_helper_rolls_back_when_the_new_version_does_not_start(tmp_path, installed, image, signed):
    appdata, journal = staged(tmp_path, installed, image, parent_pid=999_999_999)
    launches = []

    def launch(executable, arguments, environment):
        launches.append(arguments)
        # The new version crashes; the restored one starts normally.
        return FakeProcess(exit_code=1 if arguments else None)

    code, late_imports = run_guarded(helper_args(journal), appdata, launch)
    assert code == 1
    assert late_imports == []
    assert launches == [[HEALTH_ARG, journal.health_token], []]
    assert version_of(installed) == "2.6.13"
    assert not Path(journal.staged_path).exists()
    message = Path(apply_error_path(appdata)).read_text()
    assert "2.7.0 did not start correctly" in message and "2.6.13 was restored" in message


def test_helper_stops_a_new_version_that_never_reports_healthy(tmp_path, installed, image, signed):
    appdata, journal = staged(tmp_path, installed, image, parent_pid=999_999_999)
    hung = FakeProcess()
    code, _late = run_guarded(helper_args(journal), appdata,
                              lambda executable, arguments, environment: hung if arguments else FakeProcess())
    assert code == 1 and hung.terminated
    assert version_of(installed) == "2.6.13"


def test_helper_installs_nothing_while_the_app_is_still_running(tmp_path, installed, image, signed):
    appdata, journal = staged(tmp_path, installed, image, parent_pid=os.getpid())
    launch = MagicMock()
    started = time.monotonic()
    code = mac.run_helper(helper_args(journal), appdata, swap=MagicMock(), launch=launch,
                          environment={}, parent_wait_s=0.3)
    assert code == 1 and time.monotonic() - started < 5
    launch.assert_not_called()
    assert version_of(installed) == "2.6.13"
    assert not Path(journal.staged_path).exists()
    assert mac.load_journal(journal.transaction_id, appdata).state == mac.State.ABANDONED


def test_helper_refuses_a_transaction_for_another_parent(tmp_path, installed, image, signed):
    appdata, journal = staged(tmp_path, installed, image, parent_pid=999_999_999)
    swap = MagicMock()
    args = [TRANSACTION_ARG, journal.transaction_id, PARENT_PID_ARG, "123"]
    assert mac.run_helper(args, appdata, swap=swap, launch=MagicMock(), environment={}) == 2
    swap.assert_not_called()


def test_health_acknowledgement_requires_the_matching_swapped_install(tmp_path, installed, image, signed):
    appdata, journal = staged(tmp_path, installed, image, parent_pid=999_999_999)
    token = journal.health_token
    assert not mac.write_health_acknowledgement(token, appdata, bundle=str(installed))  # Not swapped yet.
    journal.state = mac.State.SWAPPED
    mac.save_journal(journal, appdata)
    assert not mac.write_health_acknowledgement("0" * 32, appdata, bundle=str(installed))
    assert not mac.write_health_acknowledgement(token, appdata, bundle=str(tmp_path / "Other.app"))
    assert mac.write_health_acknowledgement(token, appdata, bundle=str(installed))


def test_startup_prune_finishes_interrupted_and_finished_transactions(tmp_path, installed, image, signed):
    appdata, fresh = staged(tmp_path, installed, image, parent_pid=0)
    _, swapped = staged(tmp_path, installed, image, parent_pid=0)
    swapped.state = mac.State.SWAPPED
    mac.save_journal(swapped, appdata)
    _, old = staged(tmp_path, installed, image, parent_pid=0)
    stale = time.time() - 7200
    os.utime(mac.transaction_dir(old.transaction_id, appdata), (stale, stale))
    removed = mac.prune_transactions(appdata, bundle=str(installed))
    assert sorted(removed) == sorted([swapped.transaction_id, old.transaction_id])
    assert Path(fresh.staged_path).exists()
    assert not Path(swapped.staged_path).exists() and not Path(old.staged_path).exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="renamex_np is macOS-only")
def test_real_swap_exchanges_two_bundles_atomically(tmp_path):
    first = make_bundle(tmp_path / "A.app", "1.0.0")
    second = make_bundle(tmp_path / "B.app", "2.0.0")
    mac._swapper()(str(first), str(second))
    assert version_of(first) == "2.0.0" and version_of(second) == "1.0.0"


def test_ui_starts_the_helper_and_quits_once_it_is_ready():
    from ui_qt.ui_controller import UIController

    controller = SimpleNamespace(
        _update_dialog=MagicMock(), main_window=MagicMock(), exit_for_update=MagicMock(),
        on_update_abandon=MagicMock(),
    )
    timers = []

    class Timer:
        def __init__(self, parent):
            self.callback = None
            timers.append(self)

        def setInterval(self, _ms):
            pass

        @property
        def timeout(self):
            return SimpleNamespace(connect=lambda callback: setattr(self, "callback", callback))

        def start(self):
            pass

        def stop(self):
            pass

    ready = []
    with patch("ui_qt.ui_controller.QTimer", Timer), \
            patch("services.app_update_macos.start_helper", return_value=FakeProcess()), \
            patch("services.app_update_macos.helper_ready", side_effect=lambda tx: bool(ready)):
        UIController._start_macos_update(controller, "a" * 32, "macos:" + "a" * 32)
        timers[0].callback()
        controller.exit_for_update.assert_not_called()
        ready.append(True)
        timers[0].callback()
    controller.exit_for_update.assert_called_once()
    controller.on_update_abandon.assert_not_called()


def test_ui_abandons_the_update_when_the_helper_dies():
    from ui_qt.ui_controller import UIController

    controller = SimpleNamespace(
        _update_dialog=MagicMock(), main_window=MagicMock(), exit_for_update=MagicMock(),
        on_update_abandon=MagicMock(),
    )
    callbacks = []
    timer = MagicMock()
    timer.timeout.connect.side_effect = callbacks.append
    with patch("ui_qt.ui_controller.QTimer", return_value=timer), \
            patch("services.app_update_macos.start_helper", return_value=FakeProcess(exit_code=2)), \
            patch("services.app_update_macos.helper_ready", return_value=False):
        UIController._start_macos_update(controller, "a" * 32, "macos:" + "a" * 32)
        callbacks[0]()
    controller.exit_for_update.assert_not_called()
    controller.on_update_abandon.assert_called_once_with("macos:" + "a" * 32)
    controller._update_dialog.set_error.assert_called_once_with("The updater could not be started.")
