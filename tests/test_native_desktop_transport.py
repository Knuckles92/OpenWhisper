"""Exercise a real service process, backup archive and concurrent prompts."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_native_service_protocol_and_backup_need_no_gui(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "openwhisper_settings.json").write_text(json.dumps({"selected_model": "api"}))
    code = """
import sys
class NoGui:
    def find_spec(self, fullname, *_args):
        if fullname.split('.')[0] in ('PyQt6', 'ui_qt'):
            raise AssertionError('Forbidden GUI import: ' + fullname)
sys.meta_path.insert(0, NoGui())
from services.native_desktop.bootstrap import main
raise SystemExit(main())
"""
    process = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT,
        env=dict(os.environ, OPENWHISPER_DATA_DIR=str(data), HF_HUB_OFFLINE="1",
                 HUGGINGFACE_HUB_CACHE=str(tmp_path / "models")),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    packets = queue.Queue()
    errors = []
    threading.Thread(target=lambda: [packets.put(line) for line in process.stdout], daemon=True).start()
    threading.Thread(target=lambda: errors.extend(process.stderr), daemon=True).start()
    request_id = 0

    def request(method, params=None):
        nonlocal request_id
        request_id += 1
        current = request_id
        process.stdin.write(json.dumps({"id": current, "method": method, "params": params or {}}) + "\n")
        process.stdin.flush()
        while True:
            packet = json.loads(packets.get(timeout=10))
            if packet.get("event") == "fatal":
                raise AssertionError(packet)
            if packet.get("id") == current:
                assert "error" not in packet, packet
                return packet["result"]

    def wait_for(method, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = request(method)
            if predicate(result):
                return result
            time.sleep(0.01)
        raise AssertionError(f"Timed out waiting for {method}")

    try:
        snapshot = wait_for("snapshot", lambda value: value["ready"])
        assert snapshot["runtime"] == "native"
        request("hotkeys.capture", {"active": True})
        assert request("snapshot")["hotkeys_suspended"] is True
        assert not request("hotkeys.event", {"key": "f24", "pressed": True})["handled"]
        request("hotkeys.capture", {"active": False})
        assert request("snapshot")["hotkeys_suspended"] is False
        for method in ("remote.state", "backup.state", "updates.state", "stats.get", "setup.state", "mcp.state"):
            assert isinstance(request(method), dict)
        assert request("history.list")["items"] == []
        archive = tmp_path / "portable.owbackup"
        request("scratchpad.update", {"text": "Notes included in this backup — 日本語"})
        request("backup.create", {"path": str(archive), "include_recordings": False})
        backup = wait_for("backup.state", lambda value: not value["busy"])
        assert not backup.get("error"), backup
        assert archive.is_file()
        with zipfile.ZipFile(archive) as saved:
            assert saved.read("data/scratchpad.txt").decode("utf-8") == "Notes included in this backup — 日本語"
        request("backup.validate", {"path": str(archive)})
        inspected = wait_for("backup.state", lambda value: bool(value.get("inspection")))
        assert inspected["inspection"]["valid"] is True
        assert inspected["inspection"]["path"] == str(archive)
        request("setup.skip")
        assert request("setup.state")["state"] == "skipped"
        # A model-consent request keeps its original RPC open while snapshots
        # and the response to the prompt continue through the same pipe.
        process.stdin.write(json.dumps({"id": 10000, "method": "models.download", "params": {"id": "tiny.en"}}) + "\n")
        process.stdin.flush()
        while True:
            packet = json.loads(packets.get(timeout=10))
            if packet.get("event") == "prompt":
                prompt = packet["data"]
                break
            assert packet.get("id") != 10000, packet
        assert request("snapshot")["prompts"][0]["id"] == prompt["id"]
        request("prompt.resolve", {"id": prompt["id"], "value": "cancel"})
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    assert process.returncode == 0, "".join(errors)
    assert "Forbidden GUI import" not in "".join(errors)
