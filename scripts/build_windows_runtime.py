"""Pin a Windows speech runtime's wheels without installing them.

Usage::

    python scripts/build_windows_runtime.py asr-qwen

pip resolves the runtime's dependency closure for embedded Python 3.12 on
win_amd64 (``--dry-run``; nothing is installed). Every package the current
manifest already ships keeps its version unless the runtime lists it in
``upgrade``, so a re-pin moves only what it has to. Each wheel is downloaded
into ``--cache`` (kept for the next run and for local install tests), checked
against the digest pip reported, measured, and written to the runtime's
``services/local_asr/*_runtime.json``.

The installer unpacks the embedded Python and every wheel straight into the
component folder (``"extract": "zip"``), next to ``python.exe``. A wheel whose
modules live under ``*.data/purelib`` would not be importable from there, so
the script rejects one.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packaging.markers import default_environment  # noqa: E402
from packaging.requirements import Requirement  # noqa: E402
from packaging.utils import canonicalize_name, parse_wheel_filename  # noqa: E402

RUNTIMES = {
    # torch 2.7 is the last release whose CUDA 12.8 wheel still has Pascal
    # (sm_61) kernels; 2.7 added Blackwell (sm_120). One build covers a
    # GTX 1050 Ti through an RTX 50-series card.
    "asr-qwen": dict(
        requirements=["qwen-asr==0.0.6", "torch==2.7.1+cu128"],
        extra_index="https://download.pytorch.org/whl/cu128",
        # torch 2.7 needs sympy>=1.13.3; 2.6 pinned it to exactly 1.13.1.
        upgrade={"torch", "sympy"},
        output="qwen_runtime.json",
    ),
}

CHUNK = 1 << 20
# PyTorch's R2 mirror refuses urllib's default User-Agent.
USER_AGENT = "OpenWhisper-runtime-builder"


def canonical_url(url: str) -> str:
    """pip reports the R2 mirror; the manifest keeps PyTorch's main host."""
    return url.replace("://download-r2.pytorch.org/", "://download.pytorch.org/", 1)


def _wheel_name(archive_name: str) -> str | None:
    if not archive_name.endswith(".whl"):
        return None
    return canonicalize_name(parse_wheel_filename(archive_name)[0])


def current_pins(manifest: dict, upgrade: set[str]) -> list[str]:
    """``name==version`` for every wheel the manifest ships, minus ``upgrade``."""
    pins = []
    for archive in manifest["archives"]:
        name = _wheel_name(archive["name"])
        if name is None or name in upgrade:
            continue
        version = parse_wheel_filename(archive["name"])[1]
        pins.append(f"{name}=={version}")
    return pins


def resolve(requirements: list[str], extra_index: str, constraints: list[str]) -> dict:
    with tempfile.TemporaryDirectory() as work:
        report = Path(work) / "report.json"
        constraint_file = Path(work) / "constraints.txt"
        constraint_file.write_text("\n".join(constraints) + "\n", encoding="utf-8")
        command = [
            sys.executable, "-m", "pip", "install", "--dry-run", "--quiet",
            "--ignore-installed", "--only-binary=:all:",
            "--platform", "win_amd64", "--python-version", "3.12",
            "--implementation", "cp", "--abi", "cp312",
            "--index-url", "https://pypi.org/simple",
            "--extra-index-url", extra_index,
            "--constraint", str(constraint_file),
            "--report", str(report), *requirements,
        ]
        print("   $", " ".join(command), flush=True)
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise SystemExit("pip could not resolve the runtime:\n" + result.stderr)
        return json.loads(report.read_text(encoding="utf-8"))


def download(url: str, sha256: str, cache: Path) -> Path:
    """Stream ``url`` into ``cache`` once; a cached file with the right digest is reused."""
    target = cache / unquote(Path(urlparse(url).path).name)
    if target.exists() and _digest(target) == sha256:
        return target
    partial = target.with_name(target.name + ".part")
    for attempt in range(3):
        try:
            digest = hashlib.sha256()
            request = Request(url, headers={"User-Agent": USER_AGENT})
            with urlopen(request, timeout=120) as response, open(partial, "wb") as output:
                while block := response.read(CHUNK):
                    digest.update(block)
                    output.write(block)
            break
        except OSError:
            if attempt == 2:
                raise
    if digest.hexdigest() != sha256:
        partial.unlink(missing_ok=True)
        raise SystemExit(f"Checksum mismatch for {url}")
    partial.replace(target)
    return target


def _digest(path: Path) -> str:
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def pypi_file(name: str, version: str, filename: str) -> tuple[str, str] | None:
    """PyPI's URL and digest for ``filename``, when PyPI hosts that exact file."""
    request = Request(
        f"https://pypi.org/pypi/{name}/{version}/json", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urlopen(request, timeout=60) as response:
            release = json.load(response)
    except OSError:
        return None
    for file in release.get("urls", []):
        if file.get("filename") == filename:
            return file["url"], file["digests"]["sha256"]
    return None


def pin_wheel(item: dict, cache: Path):
    info = item["download_info"]
    url = canonical_url(info["url"])
    sha256 = info.get("archive_info", {}).get("hashes", {}).get("sha256")
    if "pytorch.org" in url:
        # PyTorch's index mirrors some PyPI packages, a few without a digest.
        # Only torch itself has to come from there.
        mirrored = pypi_file(
            item["metadata"]["name"], item["metadata"]["version"],
            unquote(Path(urlparse(url).path).name),
        )
        if mirrored:
            url, sha256 = mirrored
    if not sha256:
        raise SystemExit(f"No SHA-256 published for {url}")
    path = download(url, sha256, cache)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        misplaced = [
            name for name in names
            if ".data/purelib/" in name or ".data/platlib/" in name
        ]
        if misplaced:
            raise SystemExit(
                f"{path.name} installs modules from {misplaced[0]}, which a "
                "zip extract would leave unimportable"
            )
        metadata = BytesParser().parsebytes(
            archive.read(next(name for name in names if name.endswith(".dist-info/METADATA")))
        )
        installed = sum(member.file_size for member in archive.infolist())
    return (
        dict(
            name=path.name,
            url=url,
            sha256=sha256,
            size_bytes=path.stat().st_size,
            extract="zip",
        ),
        installed,
        metadata,
    )


def check_dependencies(wheels, versions: dict[str, str]) -> None:
    """Reject a closure missing a dependency embedded Python on Windows needs."""
    environment = {
        **default_environment(),
        "sys_platform": "win32",
        "platform_system": "Windows",
        "platform_machine": "AMD64",
        "os_name": "nt",
        "python_version": "3.12",
        "python_full_version": "3.12.10",
        "implementation_name": "cpython",
        "extra": "",
    }
    for _archive, _size, metadata in wheels:
        for value in metadata.get_all("Requires-Dist", []):
            dependency = Requirement(value)
            if dependency.marker and not dependency.marker.evaluate(environment):
                continue
            name = canonicalize_name(dependency.name)
            if name not in versions or not dependency.specifier.contains(
                versions[name], prereleases=True
            ):
                raise SystemExit(
                    f"Missing/incompatible target dependency: {metadata['Name']} needs {dependency}"
                )


def build(component: str, cache: Path, version: str) -> Path:
    spec = RUNTIMES[component]
    output = ROOT / "services" / "local_asr" / spec["output"]
    previous = json.loads(output.read_text(encoding="utf-8"))
    python = next(a for a in previous["archives"] if _wheel_name(a["name"]) is None)
    constraints = current_pins(previous, {canonicalize_name(n) for n in spec["upgrade"]})

    print(f"=> Resolving {component} for win_amd64", flush=True)
    items = resolve(spec["requirements"], spec["extra_index"], constraints)["install"]
    cache.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=6) as pool:
        wheels = list(pool.map(lambda item: pin_wheel(item, cache), items))
    versions = {
        canonicalize_name(item["metadata"]["name"]): item["metadata"]["version"]
        for item in items
    }
    check_dependencies(wheels, versions)

    python_path = download(python["url"], python["sha256"], cache)
    with zipfile.ZipFile(python_path) as archive:
        python_installed = sum(member.file_size for member in archive.infolist())

    runtime = dict(
        version=version,
        component_api=previous.get("component_api", 1),
        platform="win_amd64",
        install_bytes=python_installed + sum(size for _, size, _ in wheels),
        archives=[python, *(archive for archive, _, _ in wheels)],
    )
    output.write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8", newline="\n")
    download_bytes = sum(a["size_bytes"] for a in runtime["archives"])
    print(
        f"Pinned {len(runtime['archives'])} archives "
        f"({download_bytes / 1e6:.1f} MB download, "
        f"{runtime['install_bytes'] / 1e6:.1f} MB installed) "
        f"to {output.relative_to(ROOT)} as {version}",
        flush=True,
    )
    changed = sorted(
        f"{name} {versions[name]}" for name in versions
        if f"{name}=={versions[name]}" not in constraints
    )
    print("Moved: " + ", ".join(changed), flush=True)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=sorted(RUNTIMES))
    parser.add_argument(
        "--cache", type=Path, default=ROOT / ".tmp" / "windows-runtime-cache",
        help="Folder that keeps the downloaded archives between runs",
    )
    parser.add_argument(
        "--version", default=f"{datetime.date.today():%Y-%m-%d}.1",
        help="Component version; installed copies with another version are offered an update",
    )
    args = parser.parse_args()
    build(args.component, args.cache, args.version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
