"""Bounded loopback TLS controls and server-only idle resource sensitivity.

Use the safe-env.sh loopback audit guard. No ASR throughput/timing claim.
"""

# Select the checkout and temporary data directory before application imports.
# ruff: noqa: E402
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--repo", type=Path, required=True)
parser.add_argument("--output", type=Path)
parser.add_argument("--serve", action="store_true")
parser.add_argument("--cap", type=int, default=64)
args = parser.parse_args()
sys.path.insert(0, str(args.repo.resolve()))
root = tempfile.TemporaryDirectory(prefix="ow-local-connections-")
os.environ["OPENWHISPER_DATA_DIR"] = root.name
os.environ["ORT_DISABLE_TELEMETRY"] = "1"
import numpy as np
import psutil
import websockets

from services.remote_asr import client, tailscale
from services.remote_asr.engines import HostEngine
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.tls import ensure_host_identity

tailscale.status = lambda timeout=4: tailscale.TailscaleStatus("not_installed")
tailscale.whois = lambda address, timeout=4: None


class Engine(HostEngine):
    identity = ("synthetic", "transport-only")

    def describe(self):
        return {
            "family": "synthetic",
            "label": "transport-only",
            "available": True,
            "streaming": False,
        }

    def transcribe(self, audio, language):
        return {"text": "synthetic", "segments": []}


def metrics(host):
    p = psutil.Process()
    return {
        "rss_bytes": p.memory_info().rss,
        "os_threads": p.num_threads(),
        "python_threads": threading.active_count(),
        "paired_slots": args.cap - host._connection_slots._value,
        "anonymous_slots_free": host._handshake_slots._value,
    }


def serve():
    entries = []

    def save(values):
        entries[:] = [dict(v) for v in values]

    engine = Engine()
    host = SpeechHost(
        engine_provider=lambda: engine,
        registry=DeviceRegistry(lambda: list(entries), save),
        identity=ensure_host_identity(root.name),
        max_connections=args.cap,
    )
    host.start(port=0, bind="127.0.0.1")
    try:
        pairs = [
            client.pair_with_host(
                "127.0.0.1", host.port, host.open_pairing(), f"synthetic-{i}"
            )
            for i in range(64)
        ]
        # Ephemeral test credentials travel only to this script's parent pipe.
        print(
            json.dumps(
                {
                    "port": host.port,
                    "pairs": [
                        {"token": p.token, "fingerprint": p.fingerprint} for p in pairs
                    ],
                }
            ),
            flush=True,
        )
        for line in sys.stdin:
            if line.strip() == "metrics":
                print(json.dumps(metrics(host)), flush=True)
            elif line.strip() == "stop":
                host.stop()
                print(json.dumps({"after": metrics(host)}), flush=True)
                break
            else:
                break
    finally:
        host.stop()


def run_case(history=False, saturation=False):
    cap = 12 if saturation else 64
    process = subprocess.Popen(
        [
            sys.executable,
            __file__,
            "--repo",
            str(args.repo),
            "--serve",
            "--cap",
            str(cap),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    sockets = []

    def command(cmd):
        process.stdin.write(cmd + "\n")
        process.stdin.flush()
        return json.loads(process.stdout.readline())

    try:
        details = json.loads(process.stdout.readline())

        def connect(index=0, history=False):
            p = details["pairs"][index]
            options = {"history_enabled": True} if history else {}
            c = client.RemoteConnection(
                "127.0.0.1", details["port"], p["token"], p["fingerprint"], **options
            )
            sockets.append(c)
            c.connect()
            return c

        before = command("metrics")
        if saturation:
            for _ in range(12):
                connect()
            try:
                connect(1)
                other = {"connected": True}
            except Exception as exc:
                other = {"connected": False, "exception": type(exc).__name__}
            server = command("metrics")
            assert server["paired_slots"] == 12
            assert other == {"connected": False, "exception": "RemoteHostBusy"}
            return {"same_token_sockets": 12, "other_device": other, "server": server}
        rows = []
        for count in [1, 4, 8, 16, 32] if history else [1, 8, 16, 32, 64]:
            while len(sockets) < count * (2 if history else 1):
                index = len(sockets) // (2 if history else 1)
                connect(index)
                if history:
                    connect(index, history=True)
            completed = 0
            for _ in range(10):
                reply = sockets[0].request(
                    "transcribe", audio=np.zeros(1600, dtype=np.float32), timeout=5
                )
                assert reply["text"] == "synthetic"
                completed += 1
            server = command("metrics")
            assert server["paired_slots"] == len(sockets), (
                "Not all requested sockets remain admitted"
            )
            rows.append(
                {
                    "devices": count,
                    "sockets": len(sockets),
                    "server": server,
                    "successful_requests": completed,
                }
            )
        stop = command("stop")
        assert stop["after"]["paired_slots"] == 0
        return {
            "history": history,
            "capacity_override": cap,
            "before": before,
            "rows": rows,
            "stop": stop,
        }
    finally:
        for c in sockets:
            with contextlib.suppress(Exception):
                c.close()
        _, err = process.communicate("\n", timeout=15)
        if err:
            print(err, file=sys.stderr)


if args.serve:
    serve()
else:
    results = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "websockets": websockets.__version__,
        "commit": subprocess.check_output(
            ["git", "-C", str(args.repo), "rev-parse", "HEAD"], text=True
        ).strip(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "Real loopback TLS, synthetic engine, server-only RSS/threads; shared machine, no timing claims",
    }
    for name, kw in [
        ("same_token_capacity", {"saturation": True}),
        ("speech", {}),
        ("speech_history", {"history": True}),
    ]:
        results[name] = run_case(**kw)
        if args.output:
            args.output.write_text(json.dumps(results, indent=2) + "\n")
        print(name + " complete", flush=True)
root.cleanup()
