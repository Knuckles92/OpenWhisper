"""Engine-generation transition contract over real TLS, not model reload timing."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading

if __package__:
    from .policy import (ControlledEngine, admission_counts, join_all, one_request, spawn, wait_for)
else:
    from policy import (ControlledEngine, admission_counts, join_all, one_request, spawn, wait_for)
from benchmarks.host_load.runner import BenchmarkHost, parser as load_parser


class GenerationEngine(ControlledEngine):
    def __init__(self, generation):
        super().__init__(delay_s=.01)
        self.generation = generation
        self.invoked = 0
        self.invocation_lock = threading.Lock()

    @property
    def identity(self):
        return ("controlled-generation", self.generation)

    def transcribe(self, audio, language):
        with self.invocation_lock:
            self.invoked += 1
        result = super().transcribe(audio, language)
        result["generation"] = self.generation
        return result


def experiment(args):
    rows, lock, workers = [], threading.Lock(), []
    with BenchmarkHost(args) as fixture:
        old = fixture.engine = GenerationEngine(0)
        connections = [fixture.new_client(index) for index in range(4)]
        try:
            for index in range(3):
                workers.append(spawn(connections[index], rows, lock, f"pending-{index}"))
                wait_for(lambda: sum(admission_counts(fixture).values()) == index + 1)
            # Two requests have passed the identity check; only one holds the
            # decoder lock. The third is still in host admission.
            wait_for(lambda: old.invoked == 2)
            before = admission_counts(fixture)
            new = fixture.engine = GenerationEngine(1)
            new.gate.set()
            closed_idle = fixture.host.engine_changed()
            wait_for(lambda: not connections[3].alive)
        finally:
            old.gate.set()
            join_all(workers)
        one_request(connections[0], rows, lock, "next-request-old-connection")
        replacement = fixture.new_client(0)
        one_request(replacement, rows, lock, "reconnected")
        return {"before_change": before, "idle_connections_closed": closed_idle,
                "old_engine_calls": len(old.calls), "new_engine_calls": len(new.calls),
                "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    options = parser.parse_args()
    sys.path.insert(0, str(options.source_root.resolve()))
    args = load_parser().parse_args([])
    args.source_root = options.source_root
    trials = [experiment(args) for _ in range(options.repeats)]
    commit = subprocess.check_output(["git", "-C", str(options.source_root), "rev-parse", "HEAD"], text=True).strip()
    options.output.write_text(json.dumps({"source_commit": commit,
        "measurement": "real TLS with controlled engine replacement; no actual model reload", "trials": trials}, indent=2) + "\n")
    print(options.output)


if __name__ == "__main__":
    main()
