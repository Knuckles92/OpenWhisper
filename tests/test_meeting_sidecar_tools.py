"""Execute sidecar createMeetingTools polishOnly tests (TypeScript via esbuild)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SIDECAR_ROOT = Path(__file__).resolve().parents[1] / "sidecar"
RUNNER = SIDECAR_ROOT / "scripts" / "run-tools-test.mjs"


def test_polish_only_write_filter():
    """policy.polishOnly must be the write gate on the Pi tool path."""
    if not RUNNER.is_file():
        pytest.fail(f"missing {RUNNER}")
    esbuild = SIDECAR_ROOT / "node_modules" / "esbuild"
    if not esbuild.exists():
        pytest.skip("sidecar esbuild is not installed")
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")

    result = subprocess.run(
        [node, str(RUNNER)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(SIDECAR_ROOT),
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
