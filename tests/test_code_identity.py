"""Telling that the code on disk changed under the running app.

A copy updated underneath a running app mixes modules already loaded with
newer ones read on first use; the restart offer depends on noticing it.
"""

import os
import shutil
import subprocess
import sys

import pytest

from services import code_identity

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(code_identity, "is_frozen", lambda: False)
    monkeypatch.setattr(code_identity, "bundle_root", lambda: str(tmp_path))
    monkeypatch.setattr(code_identity, "_at_launch", None)
    git_dir = tmp_path / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    return git_dir


def test_a_new_commit_on_the_branch_is_a_change(checkout):
    branch = checkout / "refs" / "heads" / "main"
    branch.write_text(COMMIT_A + "\n")
    code_identity.remember_launch()
    assert not code_identity.changed_since_launch()

    branch.write_text(COMMIT_B + "\n")

    assert code_identity.changed_since_launch()


def test_packed_and_detached_heads_are_read(checkout):
    (checkout / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{COMMIT_B} refs/heads/feature\n"
        f"{COMMIT_A} refs/heads/main\n"
        f"^{COMMIT_B}\n"
    )
    assert code_identity.code_identity() == COMMIT_A

    (checkout / "HEAD").write_text(COMMIT_B + "\n")
    assert code_identity.code_identity() == COMMIT_B


def test_nothing_is_reported_without_a_launch_version_or_a_readable_head(checkout):
    (checkout / "refs" / "heads" / "main").write_text(COMMIT_A)
    assert not code_identity.changed_since_launch()  # Nothing remembered.

    code_identity.remember_launch()
    (checkout / "HEAD").unlink()  # Mid-write, or the checkout went away.
    assert not code_identity.changed_since_launch()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_a_real_commit_counts_and_an_uncommitted_edit_does_not(tmp_path, monkeypatch):
    monkeypatch.setattr(code_identity, "is_frozen", lambda: False)
    monkeypatch.setattr(code_identity, "bundle_root", lambda: str(tmp_path))
    monkeypatch.setattr(code_identity, "_at_launch", None)

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
             *args],
            cwd=tmp_path, check=True, capture_output=True,
        )

    git("init", "-q", "-b", "main")
    module = tmp_path / "module.py"
    module.write_text("A = 1\n")
    git("add", ".")
    git("commit", "-qm", "first")
    git("pack-refs", "--all")
    code_identity.remember_launch()

    module.write_text("A = 2\n")
    assert not code_identity.changed_since_launch()

    git("commit", "-qam", "second")
    assert code_identity.changed_since_launch()


def test_an_installed_build_notices_its_program_file_replaced(tmp_path, monkeypatch):
    program = tmp_path / "OpenWhisper"
    program.write_bytes(b"version one")
    monkeypatch.setattr(code_identity, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "executable", str(program))
    monkeypatch.setattr(code_identity, "_at_launch", None)
    code_identity.remember_launch()
    assert not code_identity.changed_since_launch()

    # Package managers write the new file beside the old and rename it over.
    staged = tmp_path / "OpenWhisper.new"
    staged.write_bytes(b"version two, longer")
    os.replace(staged, program)

    assert code_identity.changed_since_launch()
