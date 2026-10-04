"""Bind real-hardware qualification to the exact, checksum-verified candidate.

--template creates an explicitly incomplete report to fill after testing.
Without --template, missing evidence or failed checks return a nonzero exit.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys

CHECKS = (
    "capture_and_playback", "cancel_decode_no_late_output", "cancel_download",
    "quit_while_busy", "microphone_disconnect_visible", "disk_full_visible",
    "forced_exit_recovery", "backup_restore",
)
PLATFORMS = ("windows", "linux", "macos")


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def candidate_identity(directory: Path):
    hashes = {}
    for line in (directory / "SHA256SUMS.txt").read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([a-f0-9]{64})  ([A-Za-z0-9_.-]+)", line)
        _require(match is not None, "Candidate has an invalid checksum entry")
        digest, name = match.groups()
        _require(name not in hashes, "Candidate has duplicate checksum entries")
        path = directory / name
        _require(path.is_file() and not path.is_symlink(), f"Candidate file is missing or unsafe: {name}")
        _require(_sha256(path) == digest, f"Candidate checksum mismatch: {name}")
        hashes[name] = digest
    _require("BUILD-METADATA.json" in hashes, "Candidate build metadata is not checksummed")
    metadata = json.loads((directory / "BUILD-METADATA.json").read_text(encoding="utf-8"))
    _require(isinstance(metadata, dict), "Invalid build metadata")
    _require(metadata.get("schema_version") == 2 and metadata.get("source_qualification") == "passed",
             "Candidate predates the required full source qualification")
    _require(re.fullmatch(r"[a-f0-9]{40}", str(metadata.get("commit_sha", ""))), "Invalid candidate commit")
    version = metadata.get("version")
    _require(isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+", version), "Invalid candidate version")
    _require(type(metadata.get("windows_setup_only")) is bool, "Missing Windows artifact mode")
    expected = {
        f"OpenWhisper-Setup-{version}.exe", f"OpenWhisper-{version}-linux-amd64.deb",
        f"OpenWhisper-{version}-linux-x86_64.pkg.tar.zst", f"OpenWhisper-{version}-macos-arm64.dmg",
    }
    if not metadata["windows_setup_only"]:
        expected.add(f"OpenWhisper-{version}-win64.tar.xz")
    _require(set(hashes) == expected | {"BUILD-METADATA.json"}, "Candidate artifact inventory is incomplete or unexpected")
    _require({p.name for p in directory.iterdir()} == set(hashes) | {"SHA256SUMS.txt"},
             "Candidate directory contains unverified files")
    return metadata, {name: hashes[name] for name in sorted(expected)}


def validate_draft_assets(directory: Path, release, *, uploaded=False):
    """Reject mixed drafts and verify every asset after a qualified upload.

    The directory must already have passed candidate and hardware validation.
    Before upload, a setup-only candidate may remove its same-version native
    Windows archive; unrelated assets require an explicit draft cleanup.
    """
    expected = {path.name: path for path in directory.iterdir()}
    assets = release.get("assets") if isinstance(release, dict) else None
    _require(isinstance(assets, list), "Could not read draft asset inventory")
    actual = {}
    for asset in assets:
        _require(isinstance(asset, dict) and isinstance(asset.get("name"), str),
                 "Invalid draft asset")
        name = asset["name"]
        _require(name not in actual, "Draft contains duplicate asset names")
        actual[name] = asset.get("digest")
    if uploaded:
        _require(set(actual) == set(expected), "Uploaded draft asset inventory differs from qualified candidate")
        for name, path in expected.items():
            _require(actual[name] == "sha256:" + _sha256(path), f"Uploaded asset checksum mismatch: {name}")
    else:
        metadata = json.loads((directory / "BUILD-METADATA.json").read_text(encoding="utf-8"))
        removable = ({f"OpenWhisper-{metadata['version']}-win64.tar.xz"}
                     if metadata["windows_setup_only"] else set())
        _require(set(actual) <= set(expected) | removable,
                 "Draft contains unrelated assets; clean up the draft before uploading this candidate")


def _timestamp(value):
    _require(isinstance(value, str), "Missing qualification timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(parsed.tzinfo is not None, "Qualification timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _number(value, minimum, name):
    _require(type(value) in (int, float) and math.isfinite(value) and value >= minimum,
             f"Missing or invalid measurement: {name}")


def validate_report(report, metadata, artifacts, *, now=None):
    _require(isinstance(report, dict) and report.get("schema_version") == 1, "Invalid hardware report")
    _require(report.get("commit_sha") == metadata["commit_sha"] and report.get("version") == metadata["version"],
             "Hardware report is for a different commit or version")
    _require(report.get("artifact_sha256") == artifacts, "Hardware report does not match these candidate artifacts")
    runs = report.get("runs")
    _require(isinstance(runs, list) and 4 <= len(runs) <= 100, "Require CPU runs on all three platforms and a GPU run")
    now = now or datetime.now(timezone.utc)
    built = _timestamp(metadata.get("built_at_utc"))
    covered = set()
    for run in runs:
        _require(isinstance(run, dict), "Invalid hardware run")
        platform, compute = run.get("platform"), run.get("compute")
        _require(platform in PLATFORMS and compute in ("cpu", "gpu"), "Unknown qualification platform/device")
        _require((platform, compute) not in covered, "Duplicate platform/device qualification")
        tested = _timestamp(run.get("tested_at_utc"))
        _require(built <= tested <= now, "Hardware test must be after the build and cannot be in the future")
        for field in ("tester", "device", "os_version", "engine", "model_revision", "evidence"):
            value = run.get(field)
            _require(isinstance(value, str) and bool(value.strip()) and len(value) <= 2000,
                     f"Missing hardware evidence: {field}")
        checks = run.get("checks")
        _require(isinstance(checks, dict) and all(checks.get(name) is True for name in CHECKS),
                 f"Missing or failed hardware check on {platform}/{compute}")
        for name, minimum in (("cold_launches", 5), ("warmed_dictations", 30),
                              ("recording_minutes", 60), ("peak_rss_mb", 1),
                              ("startup_median_s", 0.001), ("stop_to_result_p95_s", 0.001)):
            _number(run.get(name), minimum, name)
        _require(type(run.get("dropped_frames")) is int and run["dropped_frames"] == 0,
                 "Long recording must have zero dropped frames")
        baseline = run.get("baseline")
        _require(isinstance(baseline, dict), "Record the previous accepted release baseline")
        for metric in ("startup_median_s", "stop_to_result_p95_s"):
            _number(baseline.get(metric), 0.001, "baseline " + metric)
            if run[metric] > baseline[metric] * 1.2:
                review = run.get("regression_review")
                _require(isinstance(review, str) and len(review.strip()) >= 20,
                         "A timing regression above 20% needs a recorded investigation and acceptance")
        if compute == "gpu":
            _number(run.get("peak_vram_mb"), 1, "peak_vram_mb")
            _require(checks.get("requested_gpu_verified") is True, "Verify actual inference used the requested GPU")
        covered.add((platform, compute))
    _require(all((platform, "cpu") in covered for platform in PLATFORMS), "CPU qualification is missing a platform")
    _require(any(compute == "gpu" for _, compute in covered), "GPU qualification is missing")


def report_template(metadata, artifacts):
    return {
        "schema_version": 1, "commit_sha": metadata["commit_sha"], "version": metadata["version"],
        "artifact_sha256": artifacts,
        "runs": [dict(platform=platform, compute=compute, tester="", tested_at_utc="",
                      device="", os_version="", engine="", model_revision="", evidence="",
                      cold_launches=0, warmed_dictations=0, recording_minutes=0, dropped_frames=None,
                      peak_rss_mb=0, peak_vram_mb=0, startup_median_s=0, stop_to_result_p95_s=0,
                      baseline=dict(startup_median_s=0, stop_to_result_p95_s=0), regression_review="",
                      checks=dict.fromkeys((*CHECKS, "requested_gpu_verified"), False))
                 for platform, compute in [(p, "cpu") for p in PLATFORMS] + [("windows", "gpu")]],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--report", type=Path)
    choice.add_argument("--template", type=Path)
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-run", type=int)
    args = parser.parse_args(argv)
    try:
        metadata, artifacts = candidate_identity(args.candidate)
        if args.expected_commit:
            _require(metadata["commit_sha"] == args.expected_commit, "Release tag moved or candidate commit differs")
        if args.expected_run:
            _require(metadata.get("workflow_run_id") == args.expected_run, "Candidate is from a different build run")
        if args.template:
            with args.template.open("x", encoding="utf-8") as handle:
                json.dump(report_template(metadata, artifacts), handle, indent=2)
                handle.write("\n")
            print("Created incomplete hardware qualification template; fill it after testing the candidate.")
        else:
            report = json.loads(args.report.read_text(encoding="utf-8"))
            validate_report(report, metadata, artifacts)
            print("Candidate checksums, source qualification, and hardware evidence passed.")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Release qualification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
