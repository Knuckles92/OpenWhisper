"""Opt-in subprocess checks of the exact helper shipped in the installer."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_app_update_apply import _bundle

HELPER = os.environ.get("OPENWHISPER_TEST_FROZEN_HELPER")
pytestmark = pytest.mark.skipif(not HELPER, reason="requires a built Windows helper")


def test_frozen_setup_helper_replaces_and_rolls_back(tmp_path):
    helper = str(Path(HELPER).resolve())
    app = _bundle(tmp_path / "installed", "2.5.2", b"old")
    (app / "unins000.exe").write_bytes(b"uninstaller")
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path / "userdata"))
    error = tmp_path / "error.txt"
    def run(action):
        result = subprocess.run(
            [helper, "--setup-action", action, "--app-dir", str(app),
             "--error-file", str(error)],
            env=env, cwd=str(tmp_path), timeout=90, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert result.returncode == 0, error.read_text() if error.exists() else result.returncode
    run("prepare")
    (app / "OpenWhisper.exe").write_bytes(b"incomplete")
    run("rollback")
    assert (app / "OpenWhisper.exe").read_bytes() == b"old"
    (app / "_internal" / "retired.dll").write_bytes(b"old dependency")
    run("prepare")
    source = _bundle(tmp_path / "new", "2.5.3", b"new")
    shutil.copytree(source, app, dirs_exist_ok=True)
    run("finish")
    assert (app / "OpenWhisper.exe").read_bytes() == b"new"
    assert not (app / "_internal" / "retired.dll").exists()
    assert (app / "unins000.exe").read_bytes() == b"uninstaller"
    assert not Path(str(app) + ".setup-backup").exists()
