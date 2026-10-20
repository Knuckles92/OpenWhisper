"""Launch independent process cells and retain every command/exit status."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
import subprocess
import sys
import time

from benchmarks.host_load.profiles import PROFILES


def run_cell(command, output):
    started = time.time()
    with output.with_suffix(".log").open("w", encoding="utf-8") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=1800)
    row = dict(command=command, output=str(output), returncode=result.returncode,
               started_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
               elapsed_s=time.time()-started)
    print(json.dumps(row), flush=True)
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profiles", default=",".join(PROFILES))
    parser.add_argument("--clients", default="1,2,3,4,6,8,12,16,24,32")
    parser.add_argument("--workloads", default="dictation")
    parser.add_argument("--duration", type=float, default=60.)
    parser.add_argument("--rate", type=float, default=.05)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--preview", choices=("window", "native"), default="window")
    parser.add_argument("--chatty-history", action="store_true")
    parser.add_argument("--max-connections", type=int)
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    commands = []
    for workload in args.workloads.split(","):
        for profile in args.profiles.split(","):
            for clients in map(int, args.clients.split(",")):
                output = args.output_dir / f"{workload}-{profile}-n{clients:02d}-s{args.seed}.json"
                if args.skip_existing and output.exists():
                    continue
                command = [sys.executable, "-m", "benchmarks.host_load", "--source-root", str(args.source_root),
                    "--output", str(output), "--profile", profile, "--clients", str(clients),
                    "--workload", workload, "--duration", str(args.duration), "--rate", str(args.rate),
                    "--seed", str(args.seed), "--preview", args.preview]
                if args.chatty_history:
                    command.append("--chatty-history")
                if args.max_connections:
                    command.extend(["--max-connections", str(args.max_connections)])
                commands.append((command, output))
    plan = dict(config={k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
                cells=[dict(command=c, output=str(o)) for c,o in commands],
                note="jobs>1 shares the machine with other cells; host-process resource metrics exclude co-runners.")
    (args.output_dir / "plan.json").write_text(json.dumps(plan, indent=2)+"\n")
    results = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(run_cell, c, o) for c,o in commands]
        for future in as_completed(futures):
            results.append(future.result())
            (args.output_dir / "runs.json").write_text(json.dumps(results, indent=2)+"\n")
    if any(row["returncode"] for row in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
