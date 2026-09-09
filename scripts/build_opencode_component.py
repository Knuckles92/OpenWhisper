"""Build the Windows OpenCode component from the lockfile and verified Bun archive.

Run via python scripts/build_component.py meeting-agent-opencode.
No development node_modules are copied, and dependency install scripts stay disabled.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlopen
import zipfile

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from services.opencode_catalog import SDK_VERSION, BUN_VERSION, COMPONENT_VERSION, RELEASE_TAG  # noqa: E402

BUN_ARCHIVE = "bun-windows-x64-baseline.zip"
BUN_SHA256 = "538f9c846355d9e847b2671bc00c47da4229a0befb24df3282b739770f3b475f"
BUN_URL = f"https://github.com/oven-sh/bun/releases/download/bun-v{BUN_VERSION}/{BUN_ARCHIVE}"


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def download(url: str, path: Path):
    with urlopen(url, timeout=60) as response, path.open("wb") as output:
        shutil.copyfileobj(response, output)


def run(command: list[str], cwd: Path, timeout=300):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Component build command failed:\n{result.stdout}\n{result.stderr}")


def build_meeting_agent_opencode() -> None:
    if sys.platform != "win32":
        raise SystemExit("Build the OpenCode component on Windows x64.")
    source = REPO / "sidecar-opencode"
    from services.opencode_component import validate_payload
    temporary_root = REPO / ".tmp"
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="opencode-build-", dir=temporary_root) as temporary:
        work = Path(temporary)
        archive = work / BUN_ARCHIVE
        print("Downloading and verifying Bun " + BUN_VERSION, flush=True)
        download(BUN_URL, archive)
        if sha256(archive) != BUN_SHA256:
            raise RuntimeError("Official Bun archive failed its pinned digest.")
        stage = work / "payload"
        stage.mkdir()
        with zipfile.ZipFile(archive) as zipped:
            (stage / "bun.exe").write_bytes(zipped.read("bun-windows-x64-baseline/bun.exe"))
        runtime = str(stage / "bun.exe")
        for name in ("package.json", "bun.lock"):
            shutil.copy2(source / name, stage / name)
        print("Installing the locked production dependency tree (scripts disabled)", flush=True)
        run([runtime, "install", "--production", "--frozen-lockfile", "--ignore-scripts", "--linker", "hoisted"], stage)
        print("Building the shared runner and offline self-test", flush=True)
        run([runtime, "build", str(source / "src/main.ts"), str(source / "src/self-test.ts"),
             "--outdir", str(stage), "--target=bun", "--packages=external", "--format=esm",
             "--splitting", "--entry-naming=[name].mjs"], source)
        notices = []
        packages = []
        for manifest in sorted((stage / "node_modules").rglob("package.json")):
            # Data/test fixtures may also contain package.json. Only actual package roots.
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (ValueError, UnicodeError):
                continue
            if not data.get("name") or not data.get("version"):
                continue
            packages.append({"name": data["name"], "version": data["version"],
                             "license": data.get("license"), "path": manifest.parent.relative_to(stage).as_posix()})
            notices.append(data["name"] + " " + data["version"] + "\nLicense: " + str(data.get("license", "See package files")))
            for license_path in sorted(manifest.parent.iterdir()):
                if license_path.is_file() and license_path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE")):
                    notices.append(license_path.read_text(encoding="utf-8", errors="replace"))
        download(f"https://raw.githubusercontent.com/oven-sh/bun/bun-v{BUN_VERSION}/LICENSE.md", stage / "BUN-LICENSE.md")
        (stage / "THIRD_PARTY_NOTICES.txt").write_text(
            "Bun " + BUN_VERSION + "\n" + (stage / "BUN-LICENSE.md").read_text(encoding="utf-8") +
            "\n\n" + "\n\n".join(notices), encoding="utf-8")
        (stage / "dependencies.json").write_text(json.dumps(packages, indent=2) + "\n", encoding="utf-8")
        files = {}
        for path in sorted(stage.rglob("*")):
            if path.is_symlink():
                raise RuntimeError("Payload contains a link: " + str(path))
            if path.is_file():
                files[path.relative_to(stage).as_posix()] = sha256(path)
        metadata = {"schema": 1, "sdk_version": SDK_VERSION, "bun_version": BUN_VERSION,
                    "component_version": COMPONENT_VERSION, "files": files}
        (stage / "payload.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        print("Verifying every file and running the offline SDK self-test", flush=True)
        validate_payload(str(stage))
        out = REPO / "dist/components"
        out.mkdir(parents=True, exist_ok=True)
        name = f"meeting-agent-opencode-win_amd64-{COMPONENT_VERSION}.zip"
        zip_path = out / name
        installed_size = 0
        # Fixed ordering, timestamps and metadata make repeat builds comparable.
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
            for path in sorted(stage.rglob("*")):
                if not path.is_file():
                    continue
                content = path.read_bytes()
                installed_size += len(content)
                info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                zipped.writestr(info, content)
        entry = {"published": True, "version": COMPONENT_VERSION, "component_api": 1,
                 "platform": "win_amd64", "install_bytes": installed_size, "archives": [{
                     "name": name,
                     "url": f"https://github.com/Knuckles92/OpenWhisper/releases/download/{RELEASE_TAG}/{name}",
                     "sha256": sha256(zip_path), "size_bytes": zip_path.stat().st_size, "extract": "zip",
                 }]}
        (out / (name + ".catalog.json")).write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
        destination = source / "dist"
        if destination.exists():
            if destination.resolve().parent != source.resolve():
                raise RuntimeError("Unexpected output location")
            shutil.rmtree(destination)
        shutil.copytree(stage, destination)
        print(json.dumps(entry, indent=2), flush=True)
        print("Verified payload: " + str(destination), flush=True)
        print("Release archive: " + str(zip_path), flush=True)


if __name__ == "__main__":
    build_meeting_agent_opencode()
