"""Verify candidate checksums, source qualification, and exact build provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys


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
    """Reject mixed drafts and verify every asset after a verified upload.

    The directory must already have passed candidate validation.
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
        _require(set(actual) == set(expected), "Uploaded draft asset inventory differs from verified candidate")
        for name, path in expected.items():
            _require(actual[name] == "sha256:" + _sha256(path), f"Uploaded asset checksum mismatch: {name}")
    else:
        metadata = json.loads((directory / "BUILD-METADATA.json").read_text(encoding="utf-8"))
        removable = ({f"OpenWhisper-{metadata['version']}-win64.tar.xz"}
                     if metadata["windows_setup_only"] else set())
        _require(set(actual) <= set(expected) | removable,
                 "Draft contains unrelated assets; clean up the draft before uploading this candidate")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-run", type=int)
    args = parser.parse_args(argv)
    try:
        metadata, _ = candidate_identity(args.candidate)
        if args.expected_commit:
            _require(metadata["commit_sha"] == args.expected_commit, "Release tag moved or candidate commit differs")
        if args.expected_run:
            _require(metadata.get("workflow_run_id") == args.expected_run, "Candidate is from a different build run")
        print("Candidate checksums, source qualification, and build provenance passed.")
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Release qualification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
