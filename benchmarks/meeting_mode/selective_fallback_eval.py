"""Reference-blind, cached-hypothesis pilot for selective ASR fallback.

This is an accuracy feasibility test, not a deployable latency measurement:
Parakeet's cached candidate was decoded from the complete session. The
selector sees only transcript word counts in fixed five-minute windows and
never sees the AMI reference or either hypothesis's error counts.

Run from the repository root:
    venv/Scripts/python.exe -m benchmarks.meeting_mode.selective_fallback_eval
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "benchmarks" / "meeting_mode" / "results"
WHISPER = RESULTS / "auto-auto-draft-only-t5-m20-p50"
PARAKEET = RESULTS / "parakeet-v3-en-draft-only-t5-m20-p50-offline"
IDS = (
    "IN1001", "IN1002", "IN1005", "IN1007", "IN1008",
    "IN1009", "IN1012", "IN1013", "IN1014", "IN1016",
)


def _counts(window: dict) -> dict[str, int]:
    return {key: int(window[key]) for key in
            ("reference_words", "hypothesis_words", "substitutions", "deletions", "insertions")}


def _totals(rows: list[dict]) -> dict:
    sums = {key: sum(int(row[key]) for row in rows) for key in
            ("reference_words", "hypothesis_words", "substitutions", "deletions", "insertions")}
    sums["errors"] = sums["substitutions"] + sums["deletions"] + sums["insertions"]
    sums["wer"] = sums["errors"] / max(1, sums["reference_words"])
    return sums


def evaluate() -> dict:
    meetings = []
    all_baseline = []
    all_candidate = []
    all_selected = []
    picked_windows = 0
    total_windows = 0
    picked_seconds = 0.0
    total_seconds = 0.0

    for meeting_id in IDS:
        whisper = json.loads((WHISPER / f"{meeting_id}.json").read_text(encoding="utf-8"))
        parakeet = json.loads((PARAKEET / f"{meeting_id}.json").read_text(encoding="utf-8"))
        if abs(float(whisper["duration_s"]) - float(parakeet["duration_s"])) > .001:
            raise ValueError(f"Audio duration mismatch: {meeting_id}")
        baseline_windows = whisper["draft_score"]["windows"]
        candidate_windows = parakeet["offline_score"]["windows"]
        if len(baseline_windows) != len(candidate_windows):
            raise ValueError(f"Window count mismatch: {meeting_id}")
        baseline = []
        candidate = []
        selected = []
        picks = []
        duration_s = float(whisper["duration_s"])
        for a, b in zip(baseline_windows, candidate_windows):
            if float(a["start_s"]) != float(b["start_s"]):
                raise ValueError(f"Window boundary mismatch: {meeting_id}")
            # Two fixed, reference-blind signs that Whisper missed speech:
            # at least 20 fewer words and at least 15% fewer than candidate.
            a_words = int(a["hypothesis_words"])
            b_words = int(b["hypothesis_words"])
            pick = b_words - a_words >= 20 and b_words >= 1.15 * max(1, a_words)
            baseline.append(_counts(a))
            candidate.append(_counts(b))
            selected.append(_counts(b if pick else a))
            if pick:
                picks.append(float(a["start_s"]))
                picked_windows += 1
                picked_seconds += max(0.0, min(duration_s, float(a["end_s"])) - float(a["start_s"]))
            total_windows += 1
        total_seconds += duration_s
        all_baseline += baseline
        all_candidate += candidate
        all_selected += selected
        a_score = _totals(baseline)
        b_score = _totals(candidate)
        s_score = _totals(selected)
        meetings.append({
            "meeting_id": meeting_id,
            "duration_s": duration_s,
            "selected_window_starts_s": picks,
            "windows": len(baseline),
            "baseline": a_score,
            "candidate_all": b_score,
            "selected": s_score,
            "delta_wer_pp": 100 * (s_score["wer"] - a_score["wer"]),
        })
    return {
        "experiment": "selective_parakeet_5min_fallback_cached_proxy",
        "selector": "candidate has >=20 more words and >=15% more words than live Whisper within a fixed 300-second window",
        "reference_blind": True,
        "candidate_compute_measurement": "cached full-session Parakeet decode; selective compute not measured",
        "source_paths": [str(WHISPER), str(PARAKEET)],
        "selected_windows": picked_windows,
        "total_windows": total_windows,
        "selected_audio_hours": picked_seconds / 3600,
        "total_audio_hours": total_seconds / 3600,
        "baseline": _totals(all_baseline),
        "candidate_all": _totals(all_candidate),
        "selected": _totals(all_selected),
        "meeting_regressions": sum(row["delta_wer_pp"] > 0 for row in meetings),
        "meetings": meetings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=RESULTS / "selective_parakeet_fallback_eval.json")
    args = parser.parse_args()
    result = evaluate()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"selected {result['selected_windows']}/{result['total_windows']} windows "
          f"({result['selected_audio_hours']:.2f}/{result['total_audio_hours']:.2f} audio h)")
    print(f"baseline {result['baseline']['wer']:.2%}; "
          f"all candidate {result['candidate_all']['wer']:.2%}; "
          f"selective {result['selected']['wer']:.2%}; "
          f"meeting regressions {result['meeting_regressions']}")
    for row in result["meetings"]:
        print(f"{row['meeting_id']}: {len(row['selected_window_starts_s'])}/{row['windows']} "
              f"windows, {row['baseline']['wer']:.2%} -> {row['selected']['wer']:.2%} "
              f"({row['delta_wer_pp']:+.2f} pp)")
    print(args.output)


if __name__ == "__main__":
    main()
