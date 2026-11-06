#!/usr/bin/env python3
"""Copy upstream license material into a native desktop package."""
from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib


_NOTICE = re.compile(r"(?:^|[._-])(?:licen[cs]e|copying|notice|copyright)(?:$|[._-])", re.I)
_METADATA = {"direct_url.json", "metadata", "record", "wheel", "installer", "requested", "entry_points.txt", "top_level.txt"}
_SOURCE_SUFFIXES = {".py", ".pyc", ".rs", ".c", ".h", ".cpp", ".js", ".sh", ".ps1"}


def _name(value):
    return re.sub(r"[^a-zA-Z0-9_.+-]", "_", str(value))


def _notice(path):
    return path.name.lower() not in _METADATA and path.suffix.lower() not in _SOURCE_SUFFIXES and bool(_NOTICE.search(path.name))


def _copy(source, relative, destination, allowed_root):
    source = source.resolve()
    relative = Path(relative)
    if (relative.is_absolute() or ".." in relative.parts or not source.is_file()
            or not source.is_relative_to(allowed_root.resolve())):
        return None
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return relative.as_posix()


def python_licenses(destination):
    result = []
    for distribution in sorted(metadata.distributions(), key=lambda item: (item.metadata.get("Name", "").lower(), item.version)):
        name = distribution.metadata.get("Name", "")
        if not name or name.lower().startswith(("pyqt", "pyside", "shiboken")):
            continue
        folder = Path("python") / f"{_name(name)}-{_name(distribution.version)}"
        root = Path(distribution.locate_file("")).resolve()
        copied = []
        for item in sorted(distribution.files or (), key=str):
            relative = Path(item)
            parts = relative.parts
            in_license_metadata = any(
                part.endswith(".dist-info") and position + 1 < len(parts) and parts[position + 1] == "licenses"
                for position, part in enumerate(parts)
            )
            if relative.name.lower() in _METADATA or not (_notice(relative) or in_license_metadata):
                continue
            path = _copy(Path(distribution.locate_file(item)), folder / relative, destination, root)
            if path:
                copied.append(path)
        result.append({"name": name, "version": distribution.version, "files": sorted(set(copied))})
    return result


def _crate_notices(root):
    for folder, directories, names in os.walk(root):
        directories[:] = [name for name in directories if name not in (".git", "target", "node_modules", "__pycache__")]
        for name in names:
            path = Path(folder) / name
            if _notice(path):
                yield path


def _git_workspace(crate):
    # Registry crates must never walk into Cargo's cache or an unrelated project.
    # Git checkouts can inherit their workspace's license, as Rune does.
    checkout = next((path for path in (crate, *crate.parents)
                     if path.parent.parent.name == "checkouts" and path.parent.parent.parent.name == "git"), None)
    if checkout is None:
        return crate
    for ancestor in crate.parents:
        if not ancestor.is_relative_to(checkout):
            break
        manifest = ancestor / "Cargo.toml"
        if manifest.is_file() and "workspace" in tomllib.loads(manifest.read_text(encoding="utf-8")):
            return ancestor
        if ancestor == checkout:
            return ancestor
    return crate


def rust_licenses(destination, cargo_metadata):
    result = []
    workspace = set(cargo_metadata["workspace_members"])
    included = {node["id"] for node in cargo_metadata["resolve"]["nodes"]}
    for package in sorted(cargo_metadata["packages"], key=lambda item: (item["name"], item["version"])):
        if package["id"] in workspace or package["id"] not in included:
            continue
        crate = Path(package["manifest_path"]).resolve().parent
        source = package.get("source") or ""
        owner = _git_workspace(crate) if source.startswith("git+") else crate
        folder = Path("rust") / f"{_name(package['name'])}-{_name(package['version'])}"
        candidates = set(_crate_notices(crate))
        if owner != crate:
            candidates.update(path for path in owner.iterdir() if path.is_file() and _notice(path))
        if package.get("license_file"):
            declared = Path(package["license_file"])
            candidates.add(declared if declared.is_absolute() else crate / declared)
        copied = []
        for path in sorted(candidates):
            resolved = path.resolve()
            if resolved.is_relative_to(crate):
                relative = resolved.relative_to(crate)
            elif resolved.is_relative_to(owner):
                relative = Path("workspace") / resolved.relative_to(owner)
            else:
                continue
            copied_path = _copy(path, folder / relative, destination, owner)
            if copied_path:
                copied.append(copied_path)
        entry = {"name": package["name"], "version": package["version"], "license": package.get("license"), "files": sorted(set(copied))}
        revision = source.rpartition("#")[2]
        if source.startswith("git+") and re.fullmatch(r"[a-fA-F0-9]{40,64}", revision):
            entry["revision"] = revision
        result.append(entry)
    return result


def collect(destination, manifest):
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    host = next(line.removeprefix("host: ") for line in subprocess.run(
        ["rustc", "-vV"], check=True, capture_output=True, encoding="utf-8"
    ).stdout.splitlines() if line.startswith("host: "))
    cargo = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--locked", "--filter-platform", host, "--manifest-path", str(manifest)],
        check=True, capture_output=True, encoding="utf-8",
    )
    index = {"format": 1, "target": host, "python": python_licenses(destination), "rust": rust_licenses(destination, json.loads(cargo.stdout))}
    (destination / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--manifest-path", type=Path, default=Path(__file__).resolve().parent.parent / "native/Cargo.toml")
    args = parser.parse_args()
    collect(args.destination, args.manifest_path)


if __name__ == "__main__":
    main()
