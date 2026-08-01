"""Build downloadable component archives and the catalog that describes them.

Usage::

    python scripts/build_component.py gpu-accel

Produces ``build/components/<id>/*.zip`` plus a ``catalog-fragment.json``
holding each archive's SHA-256 and size. Upload the archives as GitHub Release
assets, paste the fragment into the published
``components/v1/index.json``, and fill in the resulting download URLs.

The dependency closure is resolved with ``pip install --target`` against a
pinned requirements file rather than by copying from the development venv.
A dev venv carries torch, pytest, and ~170 other top-level entries, and a
copy-based build silently drifts from what the app was tested against.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from _version import __version__  # noqa: E402
from services.components import COMPONENT_API  # noqa: E402

BUILD_ROOT = REPO_ROOT / "build" / "components"

# Must match the interpreter the application is frozen with.
TARGET_PYTHON = "3.12"
TARGET_ABI = "cp312"
TARGET_PLATFORM = "win_amd64"

# gpu-accel is split into three archives. A single ~1 GB download that fails
# at 95% with no per-part retry is a support problem; parts also give honest
# progress and let a corrupt piece be re-fetched on its own.
GPU_ARCHIVE_GROUPS: Dict[str, List[str]] = {
    "cublas": ["cublas"],
    "cudnn": ["cudnn"],
    "nvrtc": ["cuda_nvrtc", "cuda_runtime"],
}


def _run(command: List[str]) -> None:
    print("   $", " ".join(command))
    subprocess.run(command, check=True)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _human(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def build_gpu_accel() -> dict:
    """Download the NVIDIA CUDA wheels and repack them as flat DLL archives.

    Returns:
        A catalog entry for the component, with URLs left blank.
    """
    work = BUILD_ROOT / "gpu-accel"
    raw = work / "raw"
    if work.exists():
        shutil.rmtree(work)
    raw.mkdir(parents=True)

    print("=> Resolving CUDA wheels")
    _run([
        sys.executable, "-m", "pip", "install",
        "--target", str(raw),
        "--only-binary=:all:",
        "--platform", TARGET_PLATFORM,
        "--python-version", TARGET_PYTHON,
        "--implementation", "cp",
        "--abi", TARGET_ABI,
        "--no-compile",
        "-r", str(REPO_ROOT / "requirements-gpu.txt"),
    ])

    nvidia_root = raw / "nvidia"
    if not nvidia_root.is_dir():
        raise SystemExit("No nvidia/ tree was produced; check requirements-gpu.txt")

    print("=> Packing archives")
    archives = []
    install_bytes = 0

    for archive_name, subdirs in GPU_ARCHIVE_GROUPS.items():
        dll_paths = []
        for subdir in subdirs:
            bin_dir = nvidia_root / subdir / "bin"
            if bin_dir.is_dir():
                dll_paths.extend(sorted(bin_dir.glob("*.dll")))

        if not dll_paths:
            print(f"   (skipping '{archive_name}': no DLLs found)")
            continue

        archive_path = work / f"gpu-accel-{archive_name}.zip"
        with zipfile.ZipFile(
            archive_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            for dll in dll_paths:
                # Flatten into a single bin/ directory. cuDNN 9's dispatcher
                # LoadLibrary()s its sub-libraries by bare name, so one flat
                # directory on the search path is more reliable than the
                # wheel's nvidia/<lib>/bin split.
                archive.write(dll, f"bin/{dll.name}")
                install_bytes += dll.stat().st_size

        size = archive_path.stat().st_size
        archives.append({
            "name": archive_path.name,
            "url": f"<fill in: GitHub Release asset URL for {archive_path.name}>",
            "sha256": _sha256(archive_path),
            "size_bytes": size,
        })
        print(f"   {archive_path.name}: {_human(size)} ({len(dll_paths)} DLLs)")

    return {
        "version": f"{__version__}+cuda12",
        "component_api": COMPONENT_API,
        "platform": TARGET_PLATFORM,
        "app_min_version": __version__,
        "install_bytes": install_bytes,
        "archives": archives,
    }


BUILDERS = {"gpu-accel": build_gpu_accel}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=sorted(BUILDERS), help="Component to build")
    args = parser.parse_args()

    entry = BUILDERS[args.component]()

    fragment_path = BUILD_ROOT / args.component / "catalog-fragment.json"
    fragment = {"schema": 1, "components": {args.component: entry}}
    fragment_path.write_text(json.dumps(fragment, indent=2), encoding="utf-8")

    download_total = sum(a["size_bytes"] for a in entry["archives"])
    print()
    print(f"Download size : {_human(download_total)}")
    print(f"Installed size: {_human(entry['install_bytes'])}")
    print(f"Catalog       : {fragment_path}")
    print()
    print("Next: upload the .zip files as GitHub Release assets, put their URLs")
    print("into the fragment, and publish it at components/v1/index.json.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
