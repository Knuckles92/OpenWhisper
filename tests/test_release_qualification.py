import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

import pytest

from scripts.check_release_qualification import (
    candidate_identity, main, report_template, validate_draft_assets, validate_report,
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
    report = report_template(metadata, assets)
    for run in report["runs"]:
        run.update(tested_at_utc="2026-10-02T00:00:00Z", tester="Fixture tester", device="Fixture device",
                   os_version="Fixture OS", engine="Fixture engine", model_revision="fixture revision",
                   evidence="Fixture-only test logs; not real release evidence", cold_launches=5,
                   warmed_dictations=30, recording_minutes=60, dropped_frames=0, peak_rss_mb=512,
                   peak_vram_mb=1024, startup_median_s=2, stop_to_result_p95_s=1,
                   baseline=dict(startup_median_s=2, stop_to_result_p95_s=1))
        run["checks"] = dict.fromkeys(run["checks"], True)
    return tmp_path, metadata, assets, report


def check(report, metadata, assets):
    validate_report(report, metadata, assets, now=datetime(2026, 10, 3, tzinfo=timezone.utc))


def test_qualification_is_bound_to_exact_artifacts_and_commit(candidate):
    directory, metadata, assets, report = candidate
    check(report, metadata, assets)
    wrong_commit = copy.deepcopy(report)
    wrong_commit["commit_sha"] = "b" * 40
    with pytest.raises(ValueError, match="different commit"):
        check(wrong_commit, metadata, assets)
    report["artifact_sha256"][next(iter(assets))] = "f" * 64
    with pytest.raises(ValueError, match="candidate artifacts"):
        check(report, metadata, dict(candidate_identity(directory)[1]))


def test_tampered_or_extra_candidate_file_fails(candidate):
    directory, _, assets, _ = candidate
    file = directory / next(iter(assets))
    original = file.read_bytes()
    file.write_bytes(b"different binary")
    with pytest.raises(ValueError, match="checksum mismatch"):
        candidate_identity(directory)
    file.write_bytes(original)
    (directory / "unexpected.exe").write_bytes(b"unverified")
    with pytest.raises(ValueError, match="unverified"):
        candidate_identity(directory)


@pytest.mark.parametrize("change", [
    lambda r: r["runs"].pop(),
    lambda r: r["runs"][0]["checks"].update(disk_full_visible=False),
    lambda r: r["runs"][0].update(recording_minutes=59),
    lambda r: r["runs"][0].update(dropped_frames=1),
    lambda r: r["runs"][0].update(peak_rss_mb=float("nan")),
    lambda r: r["runs"][0].update(warmed_dictations=True),
    lambda r: r["runs"][0].update(tested_at_utc="2026-09-30T00:00:00Z"),
    lambda r: r["runs"][0].update(tested_at_utc="2027-01-01T00:00:00Z"),
    lambda r: r["runs"][-1]["checks"].update(requested_gpu_verified=False),
])
def test_incomplete_or_failed_hardware_evidence_is_blocked(candidate, change):
    _, metadata, assets, report = candidate
    change(report)
    with pytest.raises(ValueError):
        check(report, metadata, assets)


def test_regression_requires_explicit_investigation(candidate):
    _, metadata, assets, report = candidate
    report["runs"][0]["startup_median_s"] = 3
    with pytest.raises(ValueError, match="investigation"):
        check(report, metadata, assets)
    report["runs"][0]["regression_review"] = "Fixture investigation and acceptance recorded in test evidence."
    check(report, metadata, assets)


def test_template_never_claims_hardware_was_tested(candidate):
    _, metadata, assets, _ = candidate
    report = report_template(metadata, assets)
    with pytest.raises(ValueError):
        check(report, metadata, assets)
    assert all(not any(run["checks"].values()) for run in report["runs"])


def test_cli_rejects_old_run_or_moved_tag(candidate, tmp_path_factory):
    directory, _, _, report = candidate
    path = tmp_path_factory.mktemp("evidence") / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    args = ["--candidate", str(directory), "--report", str(path)]
    assert main([*args, "--expected-run", "124"]) == 1
    assert main([*args, "--expected-commit", "b" * 40]) == 1


def test_draft_rejects_unrelated_and_duplicate_assets(candidate):
    directory, _, _, _ = candidate
    validate_draft_assets(directory, {"assets": []})
    with pytest.raises(ValueError, match="unrelated"):
        validate_draft_assets(directory, {"assets": [{"name": "older-installer.exe"}]})
    with pytest.raises(ValueError, match="duplicate"):
        validate_draft_assets(directory, {"assets": [{"name": "SHA256SUMS.txt"}] * 2})


def test_uploaded_draft_requires_exact_inventory_and_all_digests(candidate):
    directory, _, _, _ = candidate
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
    directory, metadata, _, _ = candidate
    metadata["windows_setup_only"] = True
    (directory / "BUILD-METADATA.json").write_text(json.dumps(metadata), encoding="utf-8")
    archive = f"OpenWhisper-{metadata['version']}-win64.tar.xz"
    (directory / archive).unlink()
    validate_draft_assets(directory, {"assets": [{"name": archive}]})
    with pytest.raises(ValueError, match="unrelated"):
        validate_draft_assets(directory, {"assets": [{"name": "OpenWhisper-2.6.14-win64.tar.xz"}]})


def test_release_workflows_require_ci_and_hardware_before_upload():
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
    qualification = read("qualify-release.yml")["jobs"]["qualify"]["steps"]
    commands = [s.get("run", "") for s in qualification]
    validate = next(i for i, s in enumerate(commands) if "scripts/check_release_qualification.py" in s)
    upload = next(i for i, s in enumerate(commands) if "gh release upload" in s)
    assert validate < upload
    assert "--expected-commit" in commands[validate] and "--expected-run" in commands[validate]
    assert commands[upload].index("draft-assets.json") < commands[upload].index("gh release upload")
    assert "uploaded=True" in commands[upload]
    assert not any(s.get("continue-on-error") for s in qualification)
