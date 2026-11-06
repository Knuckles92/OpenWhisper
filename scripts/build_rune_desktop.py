#!/usr/bin/env python3
"""Build an independently launchable Rune desktop and frozen Qt-free service."""
from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def build(argv=None):
    from _version import __version__

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-only", action="store_true", help="Include service source instead of freezing Python")
    parser.add_argument("--skip-native-build", action="store_true", help="Use an existing release native binary")
    parser.add_argument("--profile", choices=("debug", "release"), default="release")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "rune")
    args = parser.parse_args(argv)
    suffix = ".exe" if os.name == "nt" else ""
    target = Path(os.environ.get("CARGO_TARGET_DIR", ROOT / "native" / "target"))
    if not target.is_absolute():
        target = ROOT / target
    if not args.skip_native_build:
        command = ["cargo", "build", "--locked", "--manifest-path", str(ROOT / "native/Cargo.toml")]
        if args.profile == "release":
            command.append("--release")
        subprocess.run(command, cwd=ROOT, check=True)
    executable = target / args.profile / ("openwhisper-rune" + suffix)
    if not executable.is_file():
        parser.error(f"Native release binary is missing: {executable}")
    platform_id = f"{platform.system().lower()}-{platform.machine().lower()}"
    output = args.output.absolute()
    package = output / f"OpenWhisper-native-{platform_id}"
    if package.exists():
        shutil.rmtree(package)
    package.mkdir(parents=True)
    resources = package
    native_path = "OpenWhisper" + suffix
    if sys.platform == "darwin":
        resources = package / "OpenWhisper.app/Contents/Resources"
        resources.mkdir(parents=True)
        native_path = "OpenWhisper.app/Contents/MacOS/OpenWhisper"
        plist = {"CFBundleName": "OpenWhisper", "CFBundleDisplayName": "OpenWhisper",
                 "CFBundleIdentifier": "tech.fiorilabs.openwhisper.rune", "CFBundleExecutable": "OpenWhisper",
                 "CFBundlePackageType": "APPL", "CFBundleShortVersionString": __version__,
                 "CFBundleVersion": __version__, "LSMinimumSystemVersion": "14.0",
                 "NSHighResolutionCapable": True,
                 "NSMicrophoneUsageDescription": "OpenWhisper records your voice for dictation, meetings and walkthroughs.",
                 "NSScreenCaptureUsageDescription": "OpenWhisper captures screenshots and system audio when you start a walkthrough or meeting."}
        (package / "OpenWhisper.app/Contents/Info.plist").write_bytes(plistlib.dumps(plist))
    (package / native_path).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(executable, package / native_path)
    if sys.platform == "linux":
        clipboard = target / args.profile / "openwhisper-clipboard"
        if not clipboard.is_file():
            raise RuntimeError("The native Wayland clipboard helper is missing; build every native binary")
        (resources / "native/bin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(clipboard, resources / "native/bin/openwhisper-clipboard")
    if args.source_only:
        for name in ("main.py", "_version.py", "config.py", "services", "meeting", "transcriber", "webui/dist", "native/assets/fonts", "requirements-native.txt"):
            source = ROOT / name
            destination = resources / name
            if source.is_dir():
                shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            elif source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
    else:
        build_root = ROOT / "build" / "rune-service"
        service_dist = build_root / "dist"
        subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(service_dist), "--workpath", str(build_root / "work"), str(ROOT / "native/OpenWhisperService.spec")], cwd=ROOT, check=True)
        service = service_dist / "openwhisper-service"
        if not (service / ("openwhisper-service" + suffix)).is_file():
            raise RuntimeError("PyInstaller did not produce the native service")
        shutil.copytree(service, resources / "native/bin/openwhisper-service")
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md", "docs/rune-desktop.md"):
        source = ROOT / name
        if source.exists():
            shutil.copy2(source, package / source.name)
    manifest = {"format": 1, "version": __version__, "platform": platform_id, "runtime": "native", "qt": False,
                "native": native_path, "service": "source" if args.source_only else str((resources / ("native/bin/openwhisper-service/openwhisper-service" + suffix)).relative_to(package)).replace("\\", "/")}
    (package / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if sys.platform == "darwin":
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(package / "OpenWhisper.app")], check=True)
    archive_format = "zip" if os.name == "nt" else "gztar"
    archive = shutil.make_archive(str(output / package.name), archive_format, root_dir=output, base_dir=package.name)
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
