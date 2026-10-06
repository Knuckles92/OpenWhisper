import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.check_release_qualification import (
    candidate_identity, main, validate_draft_assets,
)


@pytest.fixture
def candidate(tmp_path):
    metadata = dict(schema_version=2, source_qualification="passed", version="2.6.15",
                    commit_sha="a" * 40, workflow_run_id=123, windows_setup_only=False,
                    built_at_utc="2026-10-01T00:00:00Z")
    names = ["OpenWhisper-Setup-2.6.15.exe", "OpenWhisper-2.6.15-linux-amd64.deb",
             "OpenWhisper-2.6.15-linux-x86_64.pkg.tar.zst", "OpenWhisper-2.6.15-macos-arm64.dmg",
             "OpenWhisper-2.6.15-win64.tar.xz"]
    for name in names:
        (tmp_path / name).write_bytes(b"candidate fixture")
    (tmp_path / "BUILD-METADATA.json").write_text(json.dumps(metadata), encoding="utf-8")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()}
    (tmp_path / "SHA256SUMS.txt").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in hashes.items()), encoding="utf-8")
    _, assets = candidate_identity(tmp_path)
    return tmp_path, metadata, assets


def test_tampered_or_extra_candidate_file_fails(candidate):
    directory, _, assets = candidate
    file = directory / next(iter(assets))
    original = file.read_bytes()
    file.write_bytes(b"different binary")
    with pytest.raises(ValueError, match="checksum mismatch"):
        candidate_identity(directory)
    file.write_bytes(original)
    (directory / "unexpected.exe").write_bytes(b"unverified")
    with pytest.raises(ValueError, match="unverified"):
        candidate_identity(directory)


def test_cli_verifies_candidate_without_manual_report(candidate):
    directory, metadata, _ = candidate
    args = ["--candidate", str(directory)]
    assert main([*args, "--expected-run", "123", "--expected-commit", metadata["commit_sha"]]) == 0
    assert main([*args, "--expected-run", "124"]) == 1
    assert main([*args, "--expected-commit", "b" * 40]) == 1


def test_draft_rejects_unrelated_and_duplicate_assets(candidate):
    directory, _, _ = candidate
    validate_draft_assets(directory, {"assets": []})
    with pytest.raises(ValueError, match="unrelated"):
        validate_draft_assets(directory, {"assets": [{"name": "older-installer.exe"}]})
    with pytest.raises(ValueError, match="duplicate"):
        validate_draft_assets(directory, {"assets": [{"name": "SHA256SUMS.txt"}] * 2})


def test_uploaded_draft_requires_exact_inventory_and_all_digests(candidate):
    directory, _, _ = candidate
    release = {"assets": [{"name": p.name, "digest": "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()}
                          for p in directory.iterdir()]}
    validate_draft_assets(directory, release, uploaded=True)
    wrong_digest = copy.deepcopy(release)
    wrong_digest["assets"][0]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="checksum mismatch"):
        validate_draft_assets(directory, wrong_digest, uploaded=True)
    release["assets"].pop()
    with pytest.raises(ValueError, match="inventory differs"):
        validate_draft_assets(directory, release, uploaded=True)


def test_setup_only_draft_allows_removing_only_its_current_native_archive(candidate):
    directory, metadata, _ = candidate
    metadata["windows_setup_only"] = True
    (directory / "BUILD-METADATA.json").write_text(json.dumps(metadata), encoding="utf-8")
    archive = f"OpenWhisper-{metadata['version']}-win64.tar.xz"
    (directory / archive).unlink()
    validate_draft_assets(directory, {"assets": [{"name": archive}]})
    with pytest.raises(ValueError, match="unrelated"):
        validate_draft_assets(directory, {"assets": [{"name": "OpenWhisper-2.6.14-win64.tar.xz"}]})


def test_release_workflows_require_ci_and_checksums_without_manual_report():
    import yaml

    root = Path(__file__).resolve().parents[1]
    read = lambda name: yaml.load((root / ".github/workflows" / name).read_text(encoding="utf-8"),
                                 Loader=yaml.BaseLoader)
    build = read("build-installers.yml")["jobs"]
    assert build["qualification"]["uses"] == "./.github/workflows/ci.yml"
    for job in ("windows", "linux", "macos"):
        assert "qualification" in build[job]["needs"]
        checkout = next(s for s in build[job]["steps"] if s.get("uses", "").startswith("actions/checkout@"))
        assert checkout["with"]["ref"] == "${{ needs.source.outputs.commit }}"
    assert "upload-draft" not in build
    workflow = read("qualify-release.yml")
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {"candidate_run_id", "release_tag"}
    qualification = workflow["jobs"]["qualify"]["steps"]
    commands = [s.get("run", "") for s in qualification]
    validate = next(i for i, s in enumerate(commands) if "scripts/check_release_qualification.py" in s)
    upload = next(i for i, s in enumerate(commands) if "gh release upload" in s)
    assert validate < upload
    assert "--expected-commit" in commands[validate] and "--expected-run" in commands[validate]
    assert commands[upload].index("draft-assets.json") < commands[upload].index("gh release upload")
    assert "uploaded=True" in commands[upload]
    assert not any(s.get("continue-on-error") for s in qualification)
