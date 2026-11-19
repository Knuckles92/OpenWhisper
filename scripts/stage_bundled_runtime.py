"""Stage a verified speech runtime inside a frozen OpenWhisper bundle.

Reuses the pinned URLs, SHA-256 digests, archive extraction, and payload
validation from ``services.components`` so the bundled runtime matches an
on-demand install byte-for-byte.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import components
from services.verified_files import sha256_file


def _cached_archive_valid(path: Path, size_bytes: int, sha256: str) -> bool:
    if not path.is_file():
        return False
    if size_bytes and path.stat().st_size != size_bytes:
        return False
    if sha256 and sha256_file(str(path)).lower() != sha256.lower():
        return False
    return True


def _fetch_archives(component_id: str, entry: dict, cache_dir: Path) -> list[Path]:
    archives = entry.get("archives") or []
    if not archives:
        raise RuntimeError(f"Catalog entry for '{component_id}' lists no archives.")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cancel = threading.Event()
    noop_progress = lambda _phase, _done, _total: None
    paths: list[Path] = []
    for archive in archives:
        name = str(archive["name"])
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise RuntimeError(f"Unsafe archive name in catalog: {name!r}")
        url = str(archive["url"])
        size_bytes = int(archive.get("size_bytes", 0))
        sha256 = str(archive.get("sha256", ""))
        dest = cache_dir / name
        if _cached_archive_valid(dest, size_bytes, sha256):
            print(f"    reusing verified archive: {dest.name}")
        else:
            if dest.exists():
                dest.unlink()
            print(f"    downloading {dest.name} ({size_bytes:,} bytes)...")
            components._download_verified(
                url,
                sha256,
                size_bytes,
                str(dest),
                noop_progress,
                cancel,
            )
        paths.append(dest)
    return paths


def _normalize_tree(root: Path) -> None:
    for pycache in list(root.rglob("__pycache__")):
        if pycache.is_dir():
            shutil.rmtree(pycache, ignore_errors=True)
    if os.name == "nt":
        return
    # Clear group/other write bits and strip executable bits from non-executables
    # so Linux packaging permission checks pass cleanly.
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            mode = os.stat(path).st_mode
            if name.endswith(".so") or ".so." in name:
                os.chmod(path, 0o644)
            elif mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                os.chmod(path, 0o755)
            else:
                os.chmod(path, 0o644)


def stage_component(
    component_id: str,
    internal_dir: Path,
    cache_dir: Path,
    *,
    platform_tag: str | None = None,
) -> Path:
    entry = components.catalog_entry_for_platform(component_id, platform_tag=platform_tag)
    if not entry:
        tag = platform_tag or components.current_platform_tag()
        raise RuntimeError(
            f"No catalog entry for component '{component_id}' on platform '{tag}'."
        )
    incompatible = components.check_compatibility(entry)
    if incompatible:
        raise RuntimeError(f"Catalog entry for '{component_id}' is incompatible: {incompatible}")

    archives = entry.get("archives") or []
    archive_paths = _fetch_archives(component_id, entry, cache_dir)

    target_root = internal_dir / components.BUNDLED_COMPONENTS_DIRNAME
    target_root.mkdir(parents=True, exist_ok=True)
    destination = target_root / component_id

    cancel = threading.Event()
    noop_progress = lambda _phase, _done, _total: None

    tmp_parent = Path(tempfile.mkdtemp(prefix=f"stage-{component_id}-", dir=str(target_root)))
    try:
        staging = tmp_parent / component_id
        staging.mkdir(parents=True, exist_ok=True)
        for archive, archive_path in zip(archives, archive_paths):
            components._extract_archive(archive, str(archive_path), str(staging), noop_progress, cancel)
        components._validate_component_payload(component_id, str(staging))
        components._write_install_markers(str(staging), entry)
        _normalize_tree(staging)
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(str(staging), str(destination))
    finally:
        shutil.rmtree(tmp_parent, ignore_errors=True)

    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        required=True,
        help="Path to the frozen bundle's _internal directory.",
    )
    parser.add_argument(
        "--component",
        default=components.ComponentId.ASR_TRANSCRIBE,
        help="Component ID to stage (default: asr-transcribe).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=ROOT / "build" / "component-cache",
        help="Directory for caching verified component archives across builds.",
    )
    args = parser.parse_args(argv)
    staged = stage_component(args.component, args.dest.resolve(), args.cache_dir.resolve())
    print(f"    staged {args.component} -> {staged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
