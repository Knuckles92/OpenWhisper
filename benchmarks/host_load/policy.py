"""Controlled before/after scheduling experiments using real loopback TLS.

The gate and service delays make ordering reproducible, not hardware predictions.
Run once per source checkout, sequentially, without concurrent CPU benchmarks.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import subprocess
import threading
import time

import numpy as np

from benchmarks.host_load.runner import BenchmarkHost, parser as load_parser


class ControlledEngine:
    """One serialized decoder; the first request holds a known arrival window."""
    identity = ("controlled-policy-benchmark",)
    accepts_phrases = False

    def __init__(self, delay_s=.03):
        self.delay_s = delay_s
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.lock = threading.Lock()
        self.calls = []

    def describe(self):
        return {"family": "controlled", "model": "gate", "label": "Controlled service",
                "available": True, "streaming": True, "device": "simulated"}

    def _decode(self, op, audio):
        with self.lock:
            self.entered.set()
            if not self.gate.wait(15):
                raise TimeoutError("Experiment did not release the gate")
            self.calls.append({"op": op, "samples": len(audio), "at": time.perf_counter()})
            time.sleep(self.delay_s)
        return str(len(audio))

    def transcribe(self, audio, language):
        return {"text": self._decode("transcribe", audio), "segments": []}

    def stream(self, session, audio, language, finish):
        return {"events": [{"text": self._decode("stream", audio), "final": finish}]}

    def cancel_stream(self, session):
        pass


def wait_for(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.002)
    if not predicate():
        raise TimeoutError("Controlled experiment did not reach its arrival barrier")


def admission_counts(fixture):
    admission = fixture.host._work_admission
    with admission._condition:
        return {"active": admission._active, "queued": len(admission._queue)}


def one_request(connection, results, lock, label, *, op="transcribe", **fields):
    began = time.perf_counter()
    try:
        result, host_s = connection.request_timed(op, audio=np.ones(1600, np.float32),
                                                  language="en", timeout=15, **fields)
        status = "preview_skipped" if result.get("preview_skipped") else "ok"
        row = {"label": label, "status": status, "host_s": host_s, "result": result}
    except Exception as error:
        row = {"label": label, "status": getattr(error, "code", None) or "error",
               "error": str(error), "exception": type(error).__name__}
    row["latency_s"] = time.perf_counter() - began
    with lock:
        results.append(row)


def spawn(connection, results, lock, label, **kwargs):
    worker = threading.Thread(target=one_request, args=(connection, results, lock, label), kwargs=kwargs)
    worker.start()
    return worker


def join_all(workers):
    for worker in workers:
        worker.join(20)
        if worker.is_alive():
            raise TimeoutError("A policy experiment request did not finish")


def fairness(args):
    results, lock, workers = [], threading.Lock(), []
    with BenchmarkHost(args) as fixture:
        engine = fixture.engine = ControlledEngine()
        attackers = [fixture.new_client(0) for _ in range(6)]
        normals = [fixture.new_client(index) for index in range(1, 6)]
        try:
            for index, connection in enumerate(attackers):
                workers.append(spawn(connection, results, lock, f"attacker-{index}"))
                wait_for(lambda: len(results) + sum(admission_counts(fixture).values()) >= index + 1)
            after_attack = admission_counts(fixture)
            for index, connection in enumerate(normals):
                workers.append(spawn(connection, results, lock, f"normal-{index}"))
                wait_for(lambda: len(results) + sum(admission_counts(fixture).values()) >= index + 7)
            before_release = admission_counts(fixture)
        finally:
            engine.gate.set()
            join_all(workers)
        summary = {}
        for group in ("attacker", "normal"):
            rows = [row for row in results if row["label"].startswith(group)]
            summary[group] = {status: sum(row["status"] == status for row in rows)
                              for status in ("ok", "busy", "error")}
        return {"name": "six_attacker_connections_five_normal_devices",
                "limits": {"connections": 12, "active": 2, "queued": 4},
                "after_attack": after_attack, "before_release": before_release,
                "summary": summary, "rows": results}


def preview_pressure(args):
    results, lock, workers = [], threading.Lock(), []
    with BenchmarkHost(args) as fixture:
        engine = fixture.engine = ControlledEngine(delay_s=.12)
        connections = [fixture.new_client(index) for index in range(7)]
        try:
            workers.append(spawn(connections[0], results, lock, "blocking-final"))
            assert engine.entered.wait(3)
            # Five devices send disposable windows before the final arrives.
            for index in range(1, 6):
                workers.append(spawn(connections[index], results, lock, f"preview-{index}", preview=True))
                wait_for(lambda: len(results) + sum(admission_counts(fixture).values()) >= index + 1)
            workers.append(spawn(connections[6], results, lock, "normal-final"))
            wait_for(lambda: len(results) + sum(admission_counts(fixture).values()) >= 7)
            before_release = admission_counts(fixture)
        finally:
            engine.gate.set()
            join_all(workers)
        return {"name": "five_disposable_windows_before_final", "before_release": before_release,
                "rows": results, "decode_count": len(engine.calls)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    commit = subprocess.check_output(["git", "-C", str(args.source_root), "rev-parse", "HEAD"], text=True).strip()
    load = load_parser().parse_args([])
    load.clients = 11
    trials = []
    for repeat in range(args.repeats):
        for experiment in (fairness, preview_pressure):
            trials.append({"repeat": repeat, **experiment(load)})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"source_root": str(args.source_root), "source_commit": commit,
        "measurement": "real loopback TLS; controlled serialized engine, not real ASR",
        "trials": trials}, indent=2) + "\n", encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
