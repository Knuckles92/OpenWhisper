"""Source launcher for the optional native Rune desktop."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


def main(arguments: list[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    if getattr(sys, "frozen", False):
        print("The Rune desktop is distributed as a separate native package. Launch OpenWhisper from that package, or run python main.py --rune from a source checkout. See docs/rune-desktop.md.", file=sys.stderr)
        return 1
    target = Path(os.environ.get("CARGO_TARGET_DIR", root / "native" / "target"))
    if not target.is_absolute():
        target = root / target
    binary = target / "debug" / ("openwhisper-rune.exe" if os.name == "nt" else "openwhisper-rune")
    cargo = shutil.which("cargo")
    if cargo:
        result = subprocess.run(
            [cargo, "build", "--locked", "--manifest-path", str(root / "native" / "Cargo.toml")],
            cwd=root,
        )
        if result.returncode:
            return result.returncode
    elif not binary.is_file():
        print("Install Rust with Cargo, then run this command again. See docs/rune-desktop.md.", file=sys.stderr)
        return 1
    env = os.environ.copy()
    env["OPENWHISPER_ROOT"] = str(root)
    env["OPENWHISPER_PYTHON"] = sys.executable
    return subprocess.call([str(binary), *arguments], cwd=root, env=env)
