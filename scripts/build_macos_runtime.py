"""Pin an Apple Silicon speech runtime's wheels without installing them.

Usage::

    python scripts/build_macos_runtime.py asr-moonshine
    python scripts/build_macos_runtime.py asr-qwen

pip resolves the runtime's dependency closure for Python 3.12 on macOS arm64
(``--dry-run``; nothing is installed). Every wheel is downloaded, checked
against the digest pip reported, and measured, and the result is written to
the runtime's ``services/local_asr/*_macos_runtime.json``. The installer
extracts the wheels into the component's ``site-packages``, which the speech
worker puts ahead of the app's own packages, the same layout as Parakeet MLX
(``scripts/build_mlx_runtime.py``).

The newest macOS a wheel targets becomes the runtime's ``macos_min``, so a Mac
that cannot load it is never offered the download.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packaging.markers import default_environment  # noqa: E402
from packaging.requirements import Requirement  # noqa: E402
from packaging.utils import canonicalize_name  # noqa: E402

from scripts.build_mlx_runtime import pin_wheel  # noqa: E402

RUNTIMES = {
    # The macOS 15 wheel is the only Apple build moonshine-voice publishes.
    "asr-moonshine": dict(
        requirements=["moonshine-voice==0.1.5"],
        platform="macosx_15_0_arm64",
        output="moonshine_macos_runtime.json",
        name="moonshine-voice",
    ),
    # The versions the Windows runtime ships (qwen_runtime.json); this torch
    # build runs on the CPU and on the Apple GPU through MPS.
    "asr-qwen": dict(
        requirements=["qwen-asr==0.0.6", "torch==2.6.0"],
        platform="macosx_14_0_arm64",
        output="qwen_macos_runtime.json",
        name="qwen-asr",
    ),
}


def resolve(requirements: list[str], platform: str) -> dict:
    with tempfile.TemporaryDirectory() as work:
        report = Path(work) / "report.json"
        command = [
            sys.executable, "-m", "pip", "install", "--dry-run", "--quiet",
            "--ignore-installed", "--only-binary=:all:",
            "--platform", platform, "--python-version", "3.12",
            "--implementation", "cp", "--abi", "cp312",
            "--report", str(report), *requirements,
        ]
        print("   $", " ".join(command), flush=True)
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise SystemExit("pip could not resolve the runtime:\n" + result.stderr)
        return json.loads(report.read_text(encoding="utf-8"))


def macos_minimum(names: list[str]) -> str:
    """The newest ``macosx_<major>_<minor>`` any wheel requires."""
    versions = [(11, 0)]
    for name in names:
        versions += [(int(a), int(b)) for a, b in re.findall(r"macosx_(\d+)_(\d+)_", name)]
    major, minor = max(versions)
    return f"{major}.{minor}"


def check_dependencies(wheels, versions: dict[str, str]) -> None:
    """Reject a closure missing a dependency the Mac itself would need.

    pip evaluates some markers against the build host, not the target.
    """
    environment = {
        **default_environment(),
        "sys_platform": "darwin",
        "platform_system": "Darwin",
        "platform_machine": "arm64",
        "python_version": "3.12",
        "python_full_version": "3.12.10",
        "extra": "",
    }
    for _archive, _size, metadata in wheels:
        for value in metadata.get_all("Requires-Dist", []):
            dependency = Requirement(value)
            if dependency.marker and not dependency.marker.evaluate(environment):
                continue
            name = canonicalize_name(dependency.name)
            if name not in versions or versions[name] not in dependency.specifier:
                raise SystemExit(
                    f"Missing/incompatible target dependency: {metadata['Name']} needs {dependency}"
                )


def build(component: str) -> Path:
    spec = RUNTIMES[component]
    print(f"=> Resolving {component} for {spec['platform']}", flush=True)
    items = resolve(spec["requirements"], spec["platform"])["install"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        wheels = list(pool.map(pin_wheel, items))
    versions = {
        canonicalize_name(item["metadata"]["name"]): item["metadata"]["version"]
        for item in items
    }
    check_dependencies(wheels, versions)
    archives = [archive for archive, _, _ in wheels]
    fingerprint = hashlib.sha256(
        json.dumps(archives, sort_keys=True).encode()
    ).hexdigest()[:12]
    runtime = dict(
        version=f"{spec['name']}-{versions[canonicalize_name(spec['name'])]}-{fingerprint}",
        component_api=1,
        platform="darwin_arm64",
        python_abi="cp312",
        macos_min=macos_minimum([archive["name"] for archive in archives]),
        install_bytes=sum(size for _, size, _ in wheels),
        archives=archives,
    )
    output = ROOT / "services" / "local_asr" / spec["output"]
    output.write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")
    print(
        f"Pinned {len(archives)} wheels "
        f"({sum(a['size_bytes'] for a in archives) / 1e6:.1f} MB download, "
        f"{runtime['install_bytes'] / 1e6:.1f} MB installed, macOS {runtime['macos_min']}+) "
        f"to {output.relative_to(ROOT)}",
        flush=True,
    )
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", choices=sorted(RUNTIMES))
    build(parser.parse_args().component)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
