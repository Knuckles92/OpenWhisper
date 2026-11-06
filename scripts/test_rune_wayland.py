#!/usr/bin/env python3
"""Run clipboard parity checks against a real isolated Wayland compositor."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    with tempfile.TemporaryDirectory(prefix="openwhisper-wayland-") as temporary:
        runtime = Path(temporary)
        runtime.chmod(0o700)
        env = {**os.environ, "XDG_RUNTIME_DIR": str(runtime), "WLR_BACKENDS": "headless",
               "WLR_RENDERER": "pixman", "WLR_LIBINPUT_NO_DEVICES": "1",
               "OPENWHISPER_TEST_WAYLAND": "1", "OPENWHISPER_ROOT": str(ROOT)}
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("DISPLAY", None)
        with (runtime / "sway.log").open("w", encoding="utf-8") as log:
            compositor = subprocess.Popen(["sway", "-c", "/dev/null"], env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    sockets = [path for path in runtime.glob("wayland-*") if path.is_socket()]
                    if sockets:
                        env["WAYLAND_DISPLAY"] = sockets[0].name
                        break
                    if compositor.poll() is not None:
                        raise RuntimeError("The isolated Wayland compositor stopped")
                    time.sleep(0.05)
                else:
                    raise TimeoutError("The isolated Wayland compositor did not start")
                subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests/native", "-p", "test_clipboard.py", "-v"], cwd=ROOT, env=env, check=True, timeout=45)
            except Exception:
                log.flush()
                sys.stderr.write((runtime / "sway.log").read_text(encoding="utf-8")[-8000:])
                raise
            finally:
                compositor.terminate()
                try:
                    compositor.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    compositor.kill()
                    compositor.wait(timeout=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
