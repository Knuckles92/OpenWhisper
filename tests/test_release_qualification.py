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
    metadata = dict(schema_version=3, source_qualification="passed", version="2.6.15",
                    commit_sha="a" * 40, workflow_run_id=123, windows_setup_only=False,
                    windows_signed=False, built_at_utc="2026-10-01T00:00:00Z")
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
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {
        "candidate_run_id", "release_tag", "require_windows_signed"}
    qualification = workflow["jobs"]["qualify"]["steps"]
    commands = [s.get("run", "") for s in qualification]
    validate = next(i for i, s in enumerate(commands) if "scripts/check_release_qualification.py" in s)
    upload = next(i for i, s in enumerate(commands) if "gh release upload" in s)
    assert validate < upload
    assert "--expected-commit" in commands[validate] and "--expected-run" in commands[validate]
    # The check's output is piped to tee; the default shell has no pipefail,
    # so without this a failed qualification would pass the step.
    assert commands[validate].index("set -o pipefail") < commands[validate].index("| tee")
    assert commands[upload].index("draft-assets.json") < commands[upload].index("gh release upload")
    assert "uploaded=True" in commands[upload]
    assert not any(s.get("continue-on-error") for s in qualification)


def _rewrite_metadata(directory, metadata):
    (directory / "BUILD-METADATA.json").write_text(json.dumps(metadata), encoding="utf-8")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
              for p in directory.iterdir() if p.name != "SHA256SUMS.txt"}
    (directory / "SHA256SUMS.txt").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in hashes.items()), encoding="utf-8")


def test_candidate_must_record_windows_signing(candidate):
    directory, metadata, _ = candidate
    for broken in ({"windows_signed": None}, {"windows_signed": "no"}, {"schema_version": 2},
                   {"windows_signed": True}, {"windows_signed": True, "windows_signing": {"method": "x"}}):
        _rewrite_metadata(directory, {**metadata, **broken})
        with pytest.raises(ValueError):
            candidate_identity(directory)


def test_cli_reports_and_can_require_windows_signing(candidate, capsys):
    directory, metadata, _ = candidate
    args = ["--candidate", str(directory)]
    assert main(args) == 0
    assert "unsigned" in capsys.readouterr().out
    assert main([*args, "--require-windows-signed"]) == 1
    signing = {"method": "trusted-signing", "signer_subject": "CN=Fiori Labs",
               "signer_thumbprint": "A" * 40}
    _rewrite_metadata(directory, {**metadata, "windows_signed": True, "windows_signing": signing})
    assert main([*args, "--require-windows-signed"]) == 0
    assert "signed via trusted-signing by CN=Fiori Labs" in capsys.readouterr().out


def test_windows_signing_is_optional_verified_and_recorded():
    import yaml

    root = Path(__file__).resolve().parents[1]
    build = yaml.load((root / ".github/workflows/build-installers.yml").read_text(encoding="utf-8"),
                      Loader=yaml.BaseLoader)["jobs"]
    windows = build["windows"]
    names = [step.get("name", "") for step in windows["steps"]]
    configure = windows["steps"][names.index("Configure Windows code signing")]
    for secret in ("WINDOWS_CERTIFICATE_PFX", "WINDOWS_CERTIFICATE_PASSWORD",
                   "AZURE_TRUSTED_SIGNING_TENANT_ID", "AZURE_TRUSTED_SIGNING_CLIENT_ID",
                   "AZURE_TRUSTED_SIGNING_CLIENT_SECRET", "AZURE_TRUSTED_SIGNING_ENDPOINT",
                   "AZURE_TRUSTED_SIGNING_ACCOUNT", "AZURE_TRUSTED_SIGNING_PROFILE"):
        assert configure["env"][secret] == "${{ secrets.%s }}" % secret
    # Only the steps that sign may see a secret: dependency installs and the
    # test run in the same job must not.
    assert "secrets." not in str(windows.get("env", {}))
    with_secrets = {step.get("name", "") for step in windows["steps"]
                    if "secrets." in str(step.get("env", {}))}
    assert with_secrets == {"Configure Windows code signing", "Build and verify"}
    assert "unsigned" in configure["run"] and "::notice" in configure["run"]
    assert names.index("Configure Windows code signing") < names.index("Build and verify")
    report = next(s for s in windows["steps"] if s.get("with", {}).get("name") == "windows-signing")
    assert report["with"]["if-no-files-found"] == "error"
    bundle = "\n".join(step.get("run", "") for step in build["bundle"]["steps"])
    assert "scripts/windows_signing.py metadata" in bundle
    assert '"schema_version": 3' in bundle
    script = (root / "scripts/build_installer.ps1").read_text(encoding="utf-8")
    assert script.index("windows_signing.py', 'sign'") < script.index("pack_update_archive.py")
    assert "windows_signing.py inno-args" in script
    assert script.index("$InnoSignArgs\n") < script.index("windows_signing.py', 'verify'")
    iss = (root / "installer/OpenWhisper.iss").read_text(encoding="utf-8")
    assert "#ifdef SignToolName\nSignTool={#SignToolName}\nSignedUninstaller=yes\n#endif" in iss
