"""Download, verify, and stage a pinned local speech model for an all-in-one installer.

Run during Windows x64 and Linux x86_64 all-in-one packaging so OpenWhisper
ships with a compact speech model ready to use offline on first launch.

Usage:
    python scripts/stage_bundled_model.py --dest build/all-in-one-models
    python scripts/stage_bundled_model.py --dest "$STAGE_ROOT/usr/lib/openwhisper/_internal/bundled_models"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def _file_matches(path: Path, expected_sha256: str, expected_size: int) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != expected_size:
            return False
        return _sha256_file(path) == expected_sha256.lower()
    except OSError:
        return False


def _dir_matches(directory: Path, spec: dict) -> bool:
    try:
        if json.loads((directory / "installed.json").read_text(encoding="utf-8")) != spec:
            return False
        return all(
            _file_matches(directory / entry["name"], entry["sha256"], entry["size_bytes"])
            for entry in spec["files"]
        )
    except (OSError, ValueError, KeyError):
        return False


def _normalize_permissions(root: Path) -> None:
    if os.name == "nt":
        return
    os.chmod(root, 0o755)
    for dirpath, dirnames, filenames in os.walk(root):
        for dirname in dirnames:
            os.chmod(Path(dirpath) / dirname, 0o755)
        for filename in filenames:
            os.chmod(Path(dirpath) / filename, 0o644)


def stage_model(dest: Path, *, model_key: str, cache_dir: Path) -> Path:
    """Download, verify, and stage ``model_key`` under ``<dest>/<model_key>``."""
    from services.components import _download_verified
    from services.local_asr.catalog import MODELS, artifacts

    if model_key not in MODELS:
        raise ValueError(f"Unknown speech model key: {model_key}")
    spec = artifacts(model_key)
    target = dest / model_key

    if _dir_matches(target, spec):
        print(f"Bundled model already staged and verified: {target}")
        return target

    model_cache = cache_dir / model_key
    model_cache.mkdir(parents=True, exist_ok=True)
    cancel = threading.Event()

    cached_files: list[tuple[dict, Path]] = []
    for entry in spec["files"]:
        cached = model_cache / entry["name"]
        if _file_matches(cached, entry["sha256"], entry["size_bytes"]):
            print(f"Using cached verified model file: {entry['name']}")
        else:
            print(f"Downloading {entry['name']} ({entry['size_bytes'] / 1e6:.1f} MB)...")
            _download_verified(
                entry["url"],
                entry["sha256"],
                entry["size_bytes"],
                str(cached),
                lambda *_args: None,
                cancel,
            )
        cached_files.append((entry, cached))

    dest.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".stage-{model_key}-", dir=dest))
    try:
        for entry, cached in cached_files:
            shutil.copy2(cached, staging / entry["name"])
        (staging / "installed.json").write_text(json.dumps(spec), encoding="utf-8")
        if not _dir_matches(staging, spec):
            raise RuntimeError(f"Staged model failed verification: {staging}")
        _normalize_permissions(staging)
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging, target)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    print(f"Staged bundled speech model '{model_key}' at {target}")
    return target


def main(argv: list[str] | None = None) -> int:
    from services.local_asr.catalog import ALL_IN_ONE_MODEL

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        required=True,
        type=Path,
        help="Parent bundled_models directory; the model is written to <dest>/<model-key>.",
    )
    parser.add_argument(
        "--model",
        default=ALL_IN_ONE_MODEL,
        help=f"Catalog model key to bundle (default: {ALL_IN_ONE_MODEL}).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=REPO_ROOT / "build" / "model-cache",
        help="Directory for caching verified downloads across builds.",
    )
    args = parser.parse_args(argv)
    stage_model(args.dest.resolve(), model_key=args.model, cache_dir=args.cache_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
