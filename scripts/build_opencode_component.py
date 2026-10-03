"""Build this platform's OpenCode component from the lockfile and a verified Bun archive.

Run via python scripts/build_component.py meeting-agent-opencode on each target
(Windows x64, Linux x86_64, Linux aarch64): the offline self-test runs the payload.
No development node_modules are copied, and dependency install scripts stay disabled.
``--pin FILE.catalog.json ...`` copies measured pins into services/opencode_catalog.py.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.request import urlopen
import zipfile

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from services.opencode_catalog import (  # noqa: E402
    SDK_VERSION, BUN_VERSION, COMPONENT_VERSION, RELEASE_URL, archive_name,
)

# Official archives and digests from https://github.com/oven-sh/bun/releases/download/
# bun-v1.3.14/SHASUMS256.txt. Baseline x64 builds run on CPUs without AVX2.
BUN_ARCHIVES = {
    "win_amd64": ("bun-windows-x64-baseline",
                  "538f9c846355d9e847b2671bc00c47da4229a0befb24df3282b739770f3b475f"),
    "linux_x86_64": ("bun-linux-x64-baseline",
                     "a063908ae08b7852ca10939bbdc6ceed3ddabce8fb9402dce83d65d73b36e6c7"),
    "linux_aarch64": ("bun-linux-aarch64",
                      "a27ffb63a8310375836e0d6f668ae17fa8d8d18b88c37c821c65331973a19a3b"),
}


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
    from services.components import current_platform_tag
    from services.opencode_component import runtime_name, validate_payload
    platform = current_platform_tag()
    if platform not in BUN_ARCHIVES:
        raise SystemExit("Build the OpenCode component on Windows x64 or Linux x86_64/aarch64.")
    bun_name, bun_sha256 = BUN_ARCHIVES[platform]
    windows = platform == "win_amd64"
    source = REPO / "sidecar-opencode"
    temporary_root = REPO / ".tmp"
    temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="opencode-build-", dir=temporary_root) as temporary:
        work = Path(temporary)
        archive = work / (bun_name + ".zip")
        print(f"Downloading and verifying Bun {BUN_VERSION} for {platform}", flush=True)
        download(f"https://github.com/oven-sh/bun/releases/download/bun-v{BUN_VERSION}/{archive.name}", archive)
        if sha256(archive) != bun_sha256:
            raise RuntimeError("Official Bun archive failed its pinned digest.")
        stage = work / "payload"
        stage.mkdir()
        runtime = stage / runtime_name(windows)
        with zipfile.ZipFile(archive) as zipped:
            runtime.write_bytes(zipped.read(f"{bun_name}/{runtime.name}"))
        if not windows:
            runtime.chmod(0o755)
        for name in ("package.json", "bun.lock"):
            shutil.copy2(source / name, stage / name)
        print("Installing the locked production dependency tree (scripts disabled)", flush=True)
        run([str(runtime), "install", "--production", "--frozen-lockfile", "--ignore-scripts", "--linker", "hoisted"], stage)
        # Package-manager command shims (symlinks on Linux) are unused: Bun runs main.mjs directly.
        for shims in sorted((stage / "node_modules").rglob(".bin"), reverse=True):
            if shims.is_dir() and not shims.is_symlink():
                shutil.rmtree(shims)
        print("Building the shared runner and offline self-test", flush=True)
        run([str(runtime), "build", str(source / "src/main.ts"), str(source / "src/self-test.ts"),
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
            "\n\n" + "\n\n".join(notices), encoding="utf-8", newline="\n")
        (stage / "dependencies.json").write_text(json.dumps(packages, indent=2) + "\n", encoding="utf-8", newline="\n")
        files = {}
        executables = []
        for path in sorted(stage.rglob("*")):
            if path.is_symlink():
                raise RuntimeError("Payload contains a link: " + str(path))
            if path.is_file():
                name = path.relative_to(stage).as_posix()
                files[name] = sha256(path)
                if not windows and path.stat().st_mode & stat.S_IXUSR:
                    executables.append(name)
        metadata = {"schema": 1, "sdk_version": SDK_VERSION, "bun_version": BUN_VERSION,
                    "component_version": COMPONENT_VERSION, "platform": platform, "files": files}
        if not windows:
            # Zip extraction drops POSIX modes; the installer restores these.
            metadata["executables"] = executables
        (stage / "payload.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8", newline="\n")
        print("Verifying every file and running the offline SDK self-test", flush=True)
        validate_payload(str(stage))
        out = REPO / "dist/components"
        out.mkdir(parents=True, exist_ok=True)
        name = archive_name(platform)
        zip_path = out / name
        installed_size = 0
        # Fixed ordering, timestamps and metadata make repeat builds comparable.
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
            for path in sorted(stage.rglob("*")):
                if not path.is_file():
                    continue
                content = path.read_bytes()
                installed_size += len(content)
                relative = path.relative_to(stage).as_posix()
                info = zipfile.ZipInfo(relative, (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o100755 if relative in executables else 0o100644) << 16
                zipped.writestr(info, content)
        pins = {"published": True, "sha256": sha256(zip_path), "size_bytes": zip_path.stat().st_size,
                "install_bytes": installed_size}
        entry = {"platform": platform, "version": COMPONENT_VERSION, "name": name,
                 "url": RELEASE_URL + name, "pins": pins}
        (out / (name + ".catalog.json")).write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8", newline="\n")
        destination = source / "dist"
        if destination.exists():
            if destination.resolve().parent != source.resolve():
                raise RuntimeError("Unexpected output location")
            shutil.rmtree(destination)
        shutil.copytree(stage, destination)
        print(json.dumps(entry, indent=2), flush=True)
        print("After uploading the archive to that URL and downloading it back, pin it with:", flush=True)
        print(f"    python scripts/build_opencode_component.py --pin dist/components/{name}.catalog.json", flush=True)
        print("Verified payload: " + str(destination), flush=True)
        print("Release archive: " + str(zip_path), flush=True)


def pin_catalog(catalog_files: list[Path], catalog: Path = REPO / "services/opencode_catalog.py") -> None:
    """Copy measured pins from built .catalog.json files into services/opencode_catalog.py.

    Run only after each archive is uploaded under RELEASE_TAG and downloaded back.
    """
    import re
    text = catalog.read_text(encoding="utf-8")
    for path in catalog_files:
        entry = json.loads(path.read_text(encoding="utf-8"))
        platform, pins = entry["platform"], entry["pins"]
        if entry["version"] != COMPONENT_VERSION or entry["name"] != archive_name(platform):
            raise SystemExit(f"{path} was built for another component version")
        archive = path.with_name(entry["name"])
        if archive.exists() and (sha256(archive), archive.stat().st_size) != (pins["sha256"], pins["size_bytes"]):
            raise SystemExit(f"{archive} does not match {path}")
        line = (f'    "{platform}": {{"published": True, "sha256": "{pins["sha256"]}",\n'
                f'        "size_bytes": {pins["size_bytes"]:_}, "install_bytes": {pins["install_bytes"]:_}}},')
        pattern = re.compile(rf'^    "{platform}": (?:dict\(_PLACEHOLDER\)|\{{[^}}]*\}}),$', re.MULTILINE)
        text, count = pattern.subn(line.replace("\\", "\\\\"), text)
        if count != 1:
            raise SystemExit(f"No single ARCHIVES entry for {platform} in {catalog}")
        print(f"Pinned {platform}: {pins['sha256']} ({pins['size_bytes']:_} bytes)", flush=True)
    catalog.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--pin"]:
        pin_catalog([Path(name) for name in sys.argv[2:]])
    else:
        build_meeting_agent_opencode()
