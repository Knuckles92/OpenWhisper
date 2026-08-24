"""Regenerate the pinned component catalog in ``services/components.py``.

Usage::

    python scripts/build_component.py gpu-accel
    python scripts/build_component.py meeting-agent

``gpu-accel`` resolves the NVIDIA wheels that ``requirements-gpu.txt`` selects
for the target platform, records the SHA-256 that pip reports for each,
measures the payload, and prints a ready-to-paste ``_BUILTIN_GPU_ARCHIVES``
block plus the matching ``install_bytes``. Entries point straight at PyPI,
whose published wheels are immutable.

``meeting-agent`` pins the official Node.js win-x64 zip from nodejs.org
(SHA-256 from that release's ``SHASUMS256.txt``), builds ``sidecar/dist/bundle.cjs``,
zips the bundle into ``dist/components/``, and prints a ready-to-paste
``_BUILTIN_MEETING_AGENT_ARCHIVES`` block. The emitted GitHub URL uses this
checkout's ``_version.py``. Set ``MEETING_AGENT_RELEASE_TAG`` and paste only
when attaching a new zip.

An earlier version of this script repacked the GPU DLLs into zips for hosting
as GitHub Release assets, described by a JSON catalog on the project website.
That indirection bought the ability to re-point a payload without an app
release, but the website served its SPA shell for the catalog path so the
fetch never once succeeded, and a CUDA bump changes ``requirements-gpu.txt``
anyway — which is an app release by definition.

The GPU dependency closure is resolved with ``pip install --dry-run`` against
the pinned requirements file rather than by reading the development venv. A
dev venv carries torch, pytest, and ~170 other top-level entries, and a
copy-based build silently drifts from what the app was tested against.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Dict, List
from urllib.request import urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from _version import __version__  # noqa: E402
from services.app_update import GITHUB_REPO  # noqa: E402
from services.components import (  # noqa: E402
    COMPONENT_API,
    GPU_COMPONENT_VERSION,
    MEETING_AGENT_COMPONENT_VERSION,
    MEETING_AGENT_RELEASE_TAG,
)

# Official Node 22 LTS (Jod). Bump together with MEETING_AGENT_COMPONENT_VERSION
# when the sidecar needs a newer runtime.
NODE_VERSION = "22.23.2"
NODE_DIST_FILENAME = f"node-v{NODE_VERSION}-win-x64.zip"
NODE_DIST_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/{NODE_DIST_FILENAME}"
NODE_SHASUMS_URL = f"https://nodejs.org/dist/v{NODE_VERSION}/SHASUMS256.txt"

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


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with urlopen(url) as response, destination.open("wb") as out:
        while chunk := response.read(1 << 20):
            out.write(chunk)


def _node_sha256_from_shasums() -> str:
    """Return the official digest for the pinned win-x64 zip."""
    print(f"=> Fetching {NODE_SHASUMS_URL}")
    with urlopen(NODE_SHASUMS_URL) as response:
        text = response.read().decode("utf-8")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        filename = parts[-1].lstrip("*")
        if filename == NODE_DIST_FILENAME:
            digest = parts[0].lower()
            if len(digest) != 64:
                raise SystemExit(f"Unexpected digest for {NODE_DIST_FILENAME}: {digest}")
            return digest
    raise SystemExit(
        f"{NODE_DIST_FILENAME} was not listed in SHASUMS256.txt for v{NODE_VERSION}"
    )


def _pin_node() -> dict:
    """Download, verify, and measure the official Node win-x64 zip."""
    expected = _node_sha256_from_shasums()
    with tempfile.TemporaryDirectory() as work:
        archive_path = Path(work) / NODE_DIST_FILENAME
        print(f"=> Downloading {NODE_DIST_FILENAME}")
        _download(NODE_DIST_URL, archive_path)
        digest = _sha256_file(archive_path)
        if digest != expected:
            raise SystemExit(
                f"Node zip digest mismatch.\n  expected: {expected}\n  got     : {digest}"
            )
        size_bytes = archive_path.stat().st_size
        node_exe_bytes = 0
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                name = member.filename.replace("\\", "/").lower()
                if name.endswith("/node.exe") or name == "node.exe":
                    node_exe_bytes = member.file_size
                    break
        if node_exe_bytes <= 0:
            raise SystemExit(f"{NODE_DIST_FILENAME} did not contain node.exe")
    return {
        "name": NODE_DIST_FILENAME,
        "url": NODE_DIST_URL,
        "sha256": expected,
        "size_bytes": size_bytes,
        "install_bytes": node_exe_bytes,
        "extract": "node-exe",
    }


def _npm() -> str:
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm is required to build the sidecar bundle")
    return npm


def _run_npm(args: List[str], cwd: Path) -> None:
    command = [_npm(), *args]
    print("   $", " ".join(command))
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            f"npm {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"
        )


def _build_sidecar_bundle_zip() -> dict:
    """Build bundle.cjs and zip it for the GitHub Release asset."""
    sidecar = REPO_ROOT / "sidecar"
    print("=> Building sidecar bundle")
    _run_npm(["ci"], sidecar)
    _run_npm(["run", "build"], sidecar)
    bundle = sidecar / "dist" / "bundle.cjs"
    if not bundle.is_file():
        raise SystemExit("sidecar build did not produce dist/bundle.cjs")

    out_dir = REPO_ROOT / "dist" / "components"
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_name = f"meeting-agent-win_amd64-{MEETING_AGENT_COMPONENT_VERSION}.zip"
    zip_path = out_dir / zip_name
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(bundle, "bundle.cjs")

    return {
        "name": zip_name,
        "url": (
            f"https://github.com/{GITHUB_REPO}/releases/download/"
            f"v{__version__}/{zip_name}"
        ),
        "sha256": _sha256_file(zip_path),
        "size_bytes": zip_path.stat().st_size,
        "install_bytes": bundle.stat().st_size,
        "extract": "zip",
        "path": str(zip_path),
    }


def _split_url(url: str) -> tuple[str, str]:
    head, tail = url.rsplit("/", 1)
    return f"{head}/", tail


def _emit_meeting_agent(archives: List[dict]) -> None:
    """Print the block to paste into services/components.py."""
    print()
    print("=" * 78)
    print("Paste into services/components.py, replacing _BUILTIN_MEETING_AGENT_ARCHIVES:")
    print("=" * 78)
    print()
    print("_BUILTIN_MEETING_AGENT_ARCHIVES: Final[Tuple[dict, ...]] = (")
    for archive in archives:
        head, tail = _split_url(archive["url"])
        print("    {")
        print(f'        "name": "{archive["name"]}",')
        print('        "url": (')
        print(f'            "{head}"')
        print(f'            "{tail}"')
        print("        ),")
        print(f'        "sha256": "{archive["sha256"]}",')
        print(f'        "size_bytes": {archive["size_bytes"]:_},')
        print(f'        "extract": "{archive["extract"]}",')
        print("    },")
    print(")")
    print()
    install_bytes = sum(int(a["install_bytes"]) for a in archives)
    download_total = sum(int(a["size_bytes"]) for a in archives)
    print(f'    # In _BUILTIN_CATALOG: "install_bytes": {install_bytes:_},')
    print(f"    # Download total: {download_total / 1e6:.0f} MB")
    print(f"    # Installed     : {install_bytes / 1e6:.0f} MB")
    print(
        f'    # Version pin   : MEETING_AGENT_COMPONENT_VERSION = "{MEETING_AGENT_COMPONENT_VERSION}"'
    )
    print(f"    #                 (component_api {COMPONENT_API})")
    print(f"    # Catalog pin   : {MEETING_AGENT_RELEASE_TAG}  (MEETING_AGENT_RELEASE_TAG today)")
    print(f"    # This URL tag  : v{__version__}  (from _version.py)")
    print()
    print("Payload unchanged: do not paste this block; leave the catalog as-is.")
    print(f"New payload: attach the zip to the v{__version__} GitHub Release,")
    print(f'set MEETING_AGENT_RELEASE_TAG = "v{__version__}", and paste this block.')
    for archive in archives:
        if archive.get("path"):
            print(f"  {archive['path']}")
    print()
    print("Bump MEETING_AGENT_COMPONENT_VERSION when Node or the sidecar bundle")
    print("changes, so existing installs are offered the new payload.")


def build_meeting_agent() -> None:
    """Pin Node, build the sidecar zip, and emit the catalog entry."""
    node = _pin_node()
    bundle = _build_sidecar_bundle_zip()
    _emit_meeting_agent([node, bundle])


BUILDERS = {
    "gpu-accel": build_gpu_accel,
    "meeting-agent": build_meeting_agent,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=sorted(BUILDERS), help="Component to build")
    args = parser.parse_args()
    BUILDERS[args.component]()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
