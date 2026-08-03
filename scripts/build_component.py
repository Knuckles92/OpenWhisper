"""Regenerate the pinned component catalog in ``services/components.py``.

Usage::

    python scripts/build_component.py gpu-accel

Resolves the NVIDIA wheels that ``requirements-gpu.txt`` selects for the target
platform, records the SHA-256 that pip reports for each, measures the payload, and
prints a ready-to-paste ``_BUILTIN_GPU_ARCHIVES`` block plus the matching
``install_bytes``. Nothing is uploaded and nothing needs hosting: the catalog
entries point straight at PyPI, whose published wheels are immutable, and the
application verifies the digest before extracting anything.

An earlier version of this script repacked the DLLs into zips for hosting as
GitHub Release assets, described by a JSON catalog on the project website. That
indirection bought the ability to re-point a payload without an app release, but
the website served its SPA shell for the catalog path so the fetch never once
succeeded, and a CUDA bump changes ``requirements-gpu.txt`` anyway — which is an
app release by definition.

The dependency closure is resolved with ``pip install --dry-run`` against the
pinned requirements file rather than by reading the development venv. A dev venv
carries torch, pytest, and ~170 other top-level entries, and a copy-based build
silently drifts from what the app was tested against.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.components import COMPONENT_API, GPU_COMPONENT_VERSION  # noqa: E402

# Must match the interpreter the application is frozen with.
TARGET_PYTHON = "3.12"
TARGET_ABI = "cp312"
TARGET_PLATFORM = "win_amd64"

# Wheels whose DLLs the component ships, in install order. cuDNN is deliberately
# absent: CTranslate2 4.8 has no import-table entry and no LoadLibrary name
# string for it, and a GPU transcription with cuDNN removed loads zero cuDNN
# modules — so shipping the ~740 MB wheel would more than double the download for
# libraries never mapped into the process.
EXPECTED_PACKAGES: List[str] = [
    "nvidia-cublas-cu12",
    "nvidia-cuda-nvrtc-cu12",
    "nvidia-cuda-runtime-cu12",
]


def _resolve_wheels() -> List[dict]:
    """Ask pip which wheels satisfy requirements-gpu.txt for the target platform.

    Returns:
        One dict per wheel with ``name``, ``url``, ``sha256`` and ``size_bytes``.

    Raises:
        SystemExit: pip failed, or resolved a set that does not match
            ``EXPECTED_PACKAGES``.
    """
    with tempfile.TemporaryDirectory() as work:
        report = Path(work) / "report.json"
        command = [
            sys.executable, "-m", "pip", "install",
            "--dry-run", "--quiet",
            "--report", str(report),
            "--target", str(Path(work) / "target"),
            "--only-binary=:all:",
            "--platform", TARGET_PLATFORM,
            "--python-version", TARGET_PYTHON,
            "--implementation", "cp",
            "--abi", TARGET_ABI,
            "-r", str(REPO_ROOT / "requirements-gpu.txt"),
        ]
        print("=> Resolving CUDA wheels for", TARGET_PLATFORM)
        print("   $", " ".join(command))
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(
                "pip could not resolve requirements-gpu.txt:\n" + result.stderr
            )
        resolved = json.loads(report.read_text(encoding="utf-8"))

    archives = []
    for item in resolved.get("install", []):
        download = item.get("download_info") or {}
        url = download.get("url", "")
        digest = (download.get("archive_info") or {}).get("hashes", {}).get("sha256")
        if not url or not digest:
            raise SystemExit(
                f"pip reported no URL/sha256 for {item.get('metadata', {}).get('name')}"
            )
        archives.append({
            "name": url.rsplit("/", 1)[-1],
            "url": url,
            "sha256": digest,
            "package": item["metadata"]["name"].lower(),
            "version": item["metadata"]["version"],
        })

    found = sorted(a["package"] for a in archives)
    if found != sorted(EXPECTED_PACKAGES):
        raise SystemExit(
            f"Resolved an unexpected wheel set.\n  expected: {sorted(EXPECTED_PACKAGES)}"
            f"\n  got     : {found}\nUpdate EXPECTED_PACKAGES if this is intended."
        )
    # Preserve EXPECTED_PACKAGES order so the emitted block is stable across runs.
    order = {name: index for index, name in enumerate(EXPECTED_PACKAGES)}
    archives.sort(key=lambda a: order[a["package"]])
    return archives


def _measure(archives: List[dict]) -> Dict[str, int]:
    """Download each wheel and total the DLL bytes the component will install.

    The digests come from pip's resolution report rather than being recomputed
    here; this pass exists to measure the extracted payload.


    The size the installer needs is the extracted payload, not the compressed
    wheel, and it drives the pre-install free-space check — so it is measured
    rather than estimated.

    Returns:
        Mapping of wheel name to its compressed size, plus ``install_bytes``.
    """
    import zipfile
    from urllib.request import urlopen

    sizes: Dict[str, int] = {}
    install_bytes = 0
    with tempfile.TemporaryDirectory() as work:
        for archive in archives:
            target = Path(work) / archive["name"]
            print(f"=> Downloading {archive['name']}")
            with urlopen(archive["url"]) as response, target.open("wb") as out:
                while chunk := response.read(1 << 20):
                    out.write(chunk)

            sizes[archive["name"]] = target.stat().st_size
            with zipfile.ZipFile(target) as wheel:
                for member in wheel.infolist():
                    parts = member.filename.replace("\\", "/").split("/")
                    # Mirror components._safe_extract_nvidia_wheel's selection.
                    if (
                        len(parts) >= 4
                        and parts[0].lower() == "nvidia"
                        and parts[-2].lower() == "bin"
                        and parts[-1].lower().endswith(".dll")
                    ):
                        install_bytes += member.file_size
    sizes["install_bytes"] = install_bytes
    return sizes


def _emit(archives: List[dict], sizes: Dict[str, int]) -> None:
    """Print the block to paste into services/components.py."""
    print()
    print("=" * 78)
    print("Paste into services/components.py, replacing _BUILTIN_GPU_ARCHIVES:")
    print("=" * 78)
    print()
    print("_BUILTIN_GPU_ARCHIVES: Final[Tuple[dict, ...]] = (")
    for archive in archives:
        print("    {")
        print(f'        "name": "{archive["name"]}",')
        print('        "url": (')
        head, tail = archive["url"].rsplit("/", 1)
        print(f'            "{head}/"')
        print(f'            "{tail}"')
        print("        ),")
        print(f'        "sha256": "{archive["sha256"]}",')
        print(f'        "size_bytes": {sizes[archive["name"]]:_},')
        print('        "extract": "nvidia-wheel",')
        print("    },")
    print(")")
    print()
    download_total = sum(sizes[a["name"]] for a in archives)
    print(f'    # In _BUILTIN_CATALOG: "install_bytes": {sizes["install_bytes"]:_},')
    print(f"    # Download total: {download_total / 1e6:.0f} MB")
    print(f"    # Installed     : {sizes['install_bytes'] / 1e6:.0f} MB")
    print(f'    # Version pin   : GPU_COMPONENT_VERSION = "{GPU_COMPONENT_VERSION}"')
    print(f"    #                 (component_api {COMPONENT_API})")
    print()
    print("Bump GPU_COMPONENT_VERSION when the CUDA versions above change, so")
    print("existing installs are offered the new payload.")
    for archive in archives:
        print(f"  resolved {archive['package']}=={archive['version']}")


def build_gpu_accel() -> None:
    """Resolve, measure, and emit the gpu-accel catalog entry."""
    archives = _resolve_wheels()
    sizes = _measure(archives)
    _emit(archives, sizes)


BUILDERS = {"gpu-accel": build_gpu_accel}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=sorted(BUILDERS), help="Component to build")
    args = parser.parse_args()
    BUILDERS[args.component]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
