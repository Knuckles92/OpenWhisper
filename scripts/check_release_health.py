"""Run the isolated synthetic lifecycle gate, preserving metrics and enforcing a deadline.

Use --executable to check a frozen application. Use --baseline to reject timing
or RSS regressions on the same runner; synthetic results are not ASR benchmarks.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LIMITS = {"fixture_ready_s": 15, "stop_to_result_p95_s": 2, "cancel_s": 2,
          "restart_s": 2, "cleanup_s": 2, "peak_rss_mb": 1500}
REQUIRED_CHECKS = ("recording_recovery", "backup_restore", "previous_data_preserved")


def failures(report, baseline=None) -> list[str]:
    if not isinstance(report, dict) or report.get("kind") != "synthetic_service_lifecycle" or report.get("passed") is not True:
        return ["Missing successful synthetic lifecycle result"]
    metrics = report.get("metrics")
    if not isinstance(metrics, dict):
        return ["Missing lifecycle metrics"]
    failed = []
    checks = report.get("checks", {})
    for name in REQUIRED_CHECKS:
        if not isinstance(checks, dict) or checks.get(name) is not True:
            failed.append(f"{name}: missing successful data-safety check")
    for key, limit in LIMITS.items():
        value = metrics.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            failed.append(f"{key}: missing or invalid measurement")
        elif value > limit:
            failed.append(f"{key}: {value:.3f} exceeds {limit}")
    if baseline is not None:
        if failures(baseline):
            failed.append("Baseline is not a valid passing synthetic report")
        else:
            for key in LIMITS:
                old = baseline["metrics"][key]
                # Avoid unstable ratios on sub-millisecond synthetic work.
                allowance = 50 if key == "peak_rss_mb" else .1
                value = metrics.get(key)
                if isinstance(value, (int, float)) and value > max(old * 1.5, old + allowance):
                    failed.append(f"{key}: regression versus baseline ({old:.3f} -> {value:.3f})")
    return failed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, help="Frozen OpenWhisper executable; defaults to source main.py")
    parser.add_argument("--output", type=Path, default=Path("release-health.json"))
    parser.add_argument("--baseline", type=Path, help="Previously accepted report from equivalent hardware")
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be positive and finite")
    if args.baseline and args.baseline.resolve() == args.output.resolve():
        parser.error("--baseline and --output must be different files")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Remove a stale report so a failed child cannot appear to have passed.
    if args.output.exists():
        args.output.unlink()
    command = ([str(args.executable.resolve())] if args.executable else
               [sys.executable, str(ROOT / "main.py")])
    try:
        completed = subprocess.run([*command, "--workflow-smoke", str(args.output.resolve())],
                                   cwd=ROOT, timeout=args.timeout, check=False)
    except subprocess.TimeoutExpired:
        print("Release lifecycle exceeded the process exit deadline", file=sys.stderr)
        return 1
    if completed.returncode != 0 or not args.output.is_file():
        print("Release lifecycle failed or produced no report", file=sys.stderr)
        return 1
    report = json.loads(args.output.read_text(encoding="utf-8"))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else None
    errors = failures(report, baseline)
    for error in errors:
        print(error, file=sys.stderr)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
