"""Summarize batch timings, reference WER, memory, and drift from serial output."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from benchmarks.meeting_mode.metrics import normalize_tokens, score_text


def summarize(report):
    def scores(texts):
        clips = {}
        for window, text in zip(report["windows"], texts):
            clip = clips.setdefault(window["clip_id"], dict(reference=window["reference"], texts=[]))
            clip["texts"].append(text)
        scores = [score_text(clip["reference"], " ".join(clip["texts"])) for clip in clips.values()]
        words = sum(score["words"] for score in scores)
        errors = sum(score["errors"] for score in scores)
        return dict(reference_words=words, errors=errors, wer=errors / words if words else None)

    baseline = next(row for row in report["runs"] if row["batch_size"] == 1 and "texts" in row)
    baseline_times = [row["elapsed_s"] for row in report["runs"] if row["batch_size"] == 1 and "texts" in row]
    result = dict(model=Path(report["model_path"]).name,
                  device=report["device"], gpu=report["gpu"], torch=report["torch"],
                  duration_s=report["duration_s"], windows=len(report["windows"]),
                  manifest_sha256=report["manifest_sha256"],
                  serial_hypothesis_words=sum(len(normalize_tokens(text)) for text in baseline["texts"]),
                  serial_reference_score=scores(baseline["texts"]), batches=[])
    for size in sorted({row["batch_size"] for row in report["runs"]}):
        rows = [row for row in report["runs"] if row["batch_size"] == size]
        successes = [row for row in rows if "texts" in row]
        row = dict(batch_size=size, failures=[r["error"] for r in rows if "error" in r])
        if successes:
            elapsed = statistics.median(r["elapsed_s"] for r in successes)
            row.update(times_s=[r["elapsed_s"] for r in successes], median_s=elapsed,
                       speedup=statistics.median(baseline_times) / elapsed,
                       drift_errors=[sum(score_text(a, b)["errors"] for a, b in zip(baseline["texts"], r["texts"])) for r in successes],
                       changed_windows=[sum(normalize_tokens(a) != normalize_tokens(b) for a, b in zip(baseline["texts"], r["texts"])) for r in successes],
                       reference_scores=[scores(r["texts"]) for r in successes])
            for key in ("peak_allocated_mib", "peak_reserved_mib"):
                if key in successes[0]:
                    row[key] = max(r[key] for r in successes)
        result["batches"].append(row)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = {path.stem: summarize(json.loads(path.read_text(encoding="utf-8"))) for path in args.reports}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
