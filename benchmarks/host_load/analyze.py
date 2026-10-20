"""Aggregate raw runs and plot baseline/candidate on identical axes."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def read_runs(directory, label):
    rows = []
    for path in sorted(directory.rglob("*.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if "summary" not in result or "config" not in result:
            continue
        config = result["config"]
        rows.append(dict(label=label, source_file=str(path), source_commit=result["source"]["commit"],
            profile=config["profile"], engine=config["engine"], clients=config["clients"],
            workload=config["workload"], duration_s=config["duration"], rate=config["rate"],
            seed=config["seed"], **result["summary"]))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="append", required=True, metavar="LABEL=DIRECTORY")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for item in args.run:
        label, directory = item.split("=", 1)
        rows.extend(read_runs(Path(directory), label))
    (args.output / "summary.json").write_text(json.dumps(rows, indent=2) + "\n")
    if not rows:
        raise SystemExit("No runs found")
    with (args.output / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        writer.writeheader()
        writer.writerows(rows)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("CSV/JSON saved; install matplotlib for PNG charts")
        return
    for workload in sorted({r["workload"] for r in rows}):
        for rate in sorted({r["rate"] for r in rows if r["workload"] == workload}):
            subset = [r for r in rows if r["workload"] == workload and r["rate"] == rate]
            fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
            for ax, profile in zip(axes.flat, sorted({r["profile"] for r in rows})):
                for label in sorted({r["label"] for r in subset}):
                    cells = sorted([r for r in subset if r["label"] == label and r["profile"] == profile], key=lambda r:r["clients"])
                    ax.plot([r["clients"] for r in cells], [r["small_final_latency_p95_s"] for r in cells], "o-", label=label)
                ax.axhline(1., color="grey", linestyle="--", linewidth=.7)
                ax.axhline(2., color="grey", linestyle=":", linewidth=.7)
                ax.set(title=profile + " (simulated)", xlabel="Offered clients", ylabel="Successful finals <15s: p95 latency (s)")
                ax.legend()
                ax.grid(alpha=.2)
            fig.suptitle(f"{workload}; {rate:g} releases/s/client; rejected/censored attempts are separate, not zero-latency successes")
            fig.savefig(args.output / f"{workload}-rate{rate:g}-latency.png", dpi=150)
            plt.close(fig)
            fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
            for ax, profile in zip(axes.flat, sorted({r["profile"] for r in rows})):
                for label in sorted({r["label"] for r in subset}):
                    cells = sorted([r for r in subset if r["label"] == label and r["profile"] == profile], key=lambda r:r["clients"])
                    failures = [100. * (r["busy"] + r["errors"] + r["not_started"] + r["not_connected"]) / max(1,r["requests"]) for r in cells]
                    ax.plot([r["clients"] for r in cells], failures, "o-", label=label)
                ax.set(title=profile, xlabel="Offered clients", ylabel="Failed or unserved offered requests (%)", ylim=(-2,102))
                ax.legend()
                ax.grid(alpha=.2)
            fig.suptitle(f"{workload}; {rate:g} releases/s/client; includes connection refusals and bounded-window backlog")
            fig.savefig(args.output / f"{workload}-rate{rate:g}-unserved.png", dpi=150)
            plt.close(fig)
    print(f"Wrote {len(rows)} run summaries to {args.output}")


if __name__ == "__main__":
    main()
