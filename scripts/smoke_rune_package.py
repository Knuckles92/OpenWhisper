#!/usr/bin/env python3
"""Relocation and real frozen-service protocol smoke for a native package."""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def smoke(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="OpenWhisper native relocation ") as temporary:
        folder = Path(temporary)
        shutil.unpack_archive(str(args.archive.absolute()), folder)
        packages = list(folder.glob("OpenWhisper-native-*"))
        if len(packages) != 1:
            raise RuntimeError("Expected one native package directory")
        package = packages[0]
        manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
        if manifest.get("qt") is not False or manifest["service"] == "source":
            raise RuntimeError("Smoke requires the independently launchable native package")
        if manifest["platform"].startswith("linux-") and not (package / "native/bin/openwhisper-clipboard").is_file():
            raise RuntimeError("The Linux package is missing its native Wayland clipboard helper")
        service = package / manifest["service"]
        desktop = package / manifest["native"]
        if os.name != "nt":
            service.chmod(service.stat().st_mode | 0o111)
            desktop.chmod(desktop.stat().st_mode | 0o111)
        if any("pyqt" in path.name.lower() or path.name.lower() in ("qt6core.dll", "libqt6core.so.6") for path in package.rglob("*")):
            raise RuntimeError("Qt was found in the native service package")
        data = folder / "User data"
        data.mkdir()
        (data / "openwhisper_settings.json").write_text(json.dumps({
            "selected_model": "api", "hf_access_policy": "never", "auto_paste": False,
            "transcript_cleanup_enabled": False, "update_check_enabled": False,
            "guided_setup_state": "existing", "hotkeys": {},
        }), encoding="utf-8")
        env = {**os.environ, "OPENWHISPER_DESKTOP_RUNTIME": "native", "OPENWHISPER_DATA_DIR": str(data),
               "OPENWHISPER_ROOT": str(package), "OPENWHISPER_SERVICE": str(service),
               "HF_HUB_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"}
        # The frozen runtime must run without the source checkout or a venv.
        env.pop("PYTHONPATH", None)
        env.pop("VIRTUAL_ENV", None)
        result = subprocess.run([service, "--self-test"], cwd=folder, env=env, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"Frozen service self-test failed:\n{result.stderr[-16000:]}")
        health = json.loads(result.stdout.strip().splitlines()[-1])
        if health != {"ok": True, "runtime": "native", "qt_loaded": False}:
            raise RuntimeError(f"Unexpected service health: {health}")
        output = queue.Queue()
        with (folder / "service.log").open("w", encoding="utf-8") as log:
            child = subprocess.Popen([service], cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, text=True)
            def read():
                for line in child.stdout:
                    try:
                        output.put(json.loads(line))
                    except ValueError:
                        output.put({"invalid": line})
                output.put({"eof": True})
            threading.Thread(target=read, daemon=True).start()
            sequence = 0
            def request(method, params=None):
                nonlocal sequence
                sequence += 1
                child.stdin.write(json.dumps({"id": sequence, "method": method, "params": params or {}}) + "\n")
                child.stdin.flush()
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    packet = output.get(timeout=max(0.1, deadline - time.monotonic()))
                    if "invalid" in packet or "eof" in packet:
                        raise RuntimeError(f"Service transport failed: {packet}")
                    if packet.get("id") == sequence:
                        if "error" in packet:
                            raise RuntimeError(f"{method}: {packet['error']}")
                        return packet["result"]
                raise TimeoutError(method)
            try:
                deadline = time.monotonic() + 60
                while not (state := request("snapshot")).get("ready"):
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Service database did not become ready")
                    time.sleep(0.1)
                if state.get("runtime") != "native":
                    raise RuntimeError("Service did not use the native runtime")
                schema = request("settings.schema")
                if not schema:
                    raise RuntimeError("Settings schema is empty")
                for method in ("models.list", "history.list", "meetings.list", "walkthroughs.list"):
                    request(method)
                request("settings.update", {"key": "ui_theme", "value": "dark"})
                request("snapshot")
                notes = "Native package relocation — saved notes 你好 مرحبا"
                request("scratchpad.update", {"text": notes})
                if request("scratchpad.get")["text"] != notes:
                    raise RuntimeError("The frozen service did not retain scratchpad text")
                request("scratchpad.flush")
                request("app.quit")
                # Acknowledged quit includes durable persistence preflight.
                child.stdin.close()
                if child.wait(timeout=30):
                    raise RuntimeError("The native service did not shut down cleanly")
            except Exception:
                child.kill()
                child.wait(timeout=10)
                log.flush()
                sys.stderr.write((folder / "service.log").read_text(encoding="utf-8")[-16000:])
                raise
        image = folder / "studio.png"
        subprocess.run([desktop, "--scene", "studio", "--snap", image, "--size", "1220x980"], cwd=folder, env=env, check=True, timeout=90)
        if not image.is_file() or image.stat().st_size < 10_000:
            raise RuntimeError("The relocated native desktop did not render a usable screenshot")
        print(json.dumps({"ok": True, "platform": manifest["platform"], "relocated": True,
                          "qt": False, "service_protocol": True, "native_render": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(smoke())
