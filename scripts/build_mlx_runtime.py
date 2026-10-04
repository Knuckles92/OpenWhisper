"""Pin Apple Silicon Parakeet wheels and model artifacts without installing them.

Resolve wheels first (Python 3.12, macOS 14 arm64)::

    python -m pip install --dry-run --ignore-installed --only-binary=:all: \
        --platform macosx_14_0_arm64 --python-version 3.12 \
        --implementation cp --abi cp312 --report .tmp/mlx-support/pip-report.json \
        parakeet-mlx==0.5.3 mlx==0.32.3 mlx-metal==0.32.3

Then run this script with that report. The target environment is checked against
wheel dependency markers because pip cross-resolution evaluates some markers on
the build host. No packages or model weights are installed.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from email.parser import BytesParser
from http.client import IncompleteRead
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.error import URLError
from urllib.request import urlopen

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
MODEL_REPO = "mlx-community/parakeet-tdt-0.6b-v3"


def fetch(url, attempts=3):
    # A dropped connection must not abort a pin of dozens of wheels; the
    # digest check in pin_wheel still rejects anything that arrives wrong.
    for attempt in range(attempts):
        try:
            with urlopen(url, timeout=120) as response:
                return response.read()
        except (IncompleteRead, URLError, TimeoutError):
            if attempt + 1 == attempts:
                raise


def pin_wheel(item):
    info = item["download_info"]
    payload = fetch(info["url"])
    sha = hashlib.sha256(payload).hexdigest()
    if sha != info["archive_info"]["hashes"]["sha256"]:
        raise ValueError("Wheel checksum mismatch")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        metadata = BytesParser().parsebytes(
            archive.read(
                next(
                    name
                    for name in archive.namelist()
                    if name.endswith(".dist-info/METADATA")
                )
            )
        )
        installed = sum(member.file_size for member in archive.infolist())
    return (
        dict(
            name=unquote(Path(urlparse(info["url"]).path).name),
            url=info["url"],
            sha256=sha,
            size_bytes=len(payload),
            extract="python-wheel",
        ),
        installed,
        metadata,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text(encoding="utf-8"))
    items = report["install"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        wheels = list(pool.map(pin_wheel, items))
    versions = {
        canonicalize_name(item["metadata"]["name"]): item["metadata"]["version"]
        for item in items
    }
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
                raise ValueError(
                    f"Missing/incompatible target dependency: {metadata['Name']} needs {dependency}"
                )
    archives = [archive for archive, _, _ in wheels]
    fingerprint = hashlib.sha256(
        json.dumps(archives, sort_keys=True).encode()
    ).hexdigest()[:12]
    runtime = dict(
        version="parakeet-mlx-"
        + versions["parakeet-mlx"]
        + "-mlx-"
        + versions["mlx"]
        + "-"
        + fingerprint,
        component_api=1,
        platform="darwin_arm64",
        python_abi="cp312",
        install_bytes=sum(size for _, size, _ in wheels),
        archives=archives,
    )
    output = ROOT / "services/local_asr/mlx_runtime.json"
    output.write_text(json.dumps(runtime, indent=2) + "\n", encoding="utf-8")

    metadata = json.loads(
        fetch(f"https://huggingface.co/api/models/{MODEL_REPO}?blobs=true")
    )
    revision = metadata["sha"]
    files = []
    for name in ("config.json", "model.safetensors"):
        entry = next(f for f in metadata["siblings"] if f["rfilename"] == name)
        url = f"https://huggingface.co/{MODEL_REPO}/resolve/{revision}/{name}"
        if entry.get("lfs"):
            digest = entry["lfs"]["sha256"]
            size = entry["lfs"]["size"]
        else:
            payload = fetch(url)
            digest, size = hashlib.sha256(payload).hexdigest(), len(payload)
        files.append(dict(name=name, url=url, sha256=digest, size_bytes=size))
    output = ROOT / "services/local_asr/models.json"
    models = json.loads(output.read_text(encoding="utf-8-sig"))
    models["parakeet-v3-mlx"] = dict(repo=MODEL_REPO, revision=revision, files=files)
    output.write_text(json.dumps(models, indent=2) + "\n", encoding="utf-8")
    print(
        f"Pinned {len(wheels)} wheels ({sum(a['size_bytes'] for a, _, _ in wheels) / 1e6:.1f} MB); model {revision}"
    )


if __name__ == "__main__":
    main()
