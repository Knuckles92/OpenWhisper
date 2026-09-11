"""Content identities for reproducible benchmark reuse (no network access)."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import os
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def file_identity(path):
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def identity(*, inputs=(), settings=None, model=None):
    # Include dirty source bytes: a commit hash alone misses local edits.
    source = {}
    for directory in ("benchmarks", "meeting", "transcriber", "services"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            source[path.relative_to(ROOT).as_posix()] = file_identity(path)
    source["config.py"] = file_identity(ROOT / "config.py")
    packages = {}
    for name in ("faster-whisper", "ctranslate2", "numpy", "openai"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    try:
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        gpu_identity = gpu.stdout.strip() if gpu.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        gpu_identity = None
    return dict(schema=1, source=source, cpu_count=os.cpu_count(), gpu=gpu_identity,
                inputs={str(Path(p).resolve()): file_identity(p) for p in inputs},
                settings=settings or {}, model=model,
                platform=platform.platform(), processor=platform.processor(),
                python=platform.python_version(), packages=packages)


def model_identity(backend):
    """Identify the installed snapshot and runtime used by an available backend."""
    from services.local_asr.catalog import MODELS, artifacts
    name = backend.model_name
    result = dict(model=name, actual_device=backend.device,
                  compute_type=getattr(backend, "_compute_type", getattr(backend, "compute_type", None)))
    if name in MODELS:
        from services.local_asr import cache
        from services.components import component_dir
        result["artifacts"] = artifacts(name)
        result["installed"] = json.loads((cache.model_dir(name) / "installed.json").read_text())
        component = getattr(backend, "runtime_component", None)
        if component:
            root = Path(component_dir(component))
            result["runtime_files"] = {
                p.relative_to(root).as_posix(): file_identity(p)
                for p in sorted(root.glob("*.json"))
            }
    else:
        from faster_whisper.utils import download_model
        snapshot = Path(name) if Path(name).is_dir() else Path(download_model(name, local_files_only=True))
        result["snapshot"] = str(snapshot.resolve())
        result["files"] = {
            p.relative_to(snapshot).as_posix(): file_identity(p)
            for p in sorted(snapshot.rglob("*")) if p.is_file()
        }
    return result


def reusable(result, expected):
    """Old or incomplete reports cannot acquire new provenance by rescoring."""
    return result.get("provenance") == expected and "error" not in result
