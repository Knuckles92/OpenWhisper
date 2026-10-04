"""Summarize frozen paired CPU rescue runs, separating development and holdout.

Run after the three selected-chunk experiments:
    venv/Scripts/python.exe -m benchmarks.meeting_mode.targeted_retry_summary
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "benchmarks" / "meeting_mode" / "results"
INPUTS = {
    "IN1009": RESULTS / "targeted_retry_cpu_slice_eval.json",
    "IN1007": RESULTS / "targeted_retry_cpu_IN1007.json",
    "IN1014": RESULTS / "targeted_retry_cpu_IN1014.json",
}
OUTPUT = RESULTS / "targeted_retry_cpu_summary.json"


def _sum_scores(scores: list[dict]) -> dict:
    keys = ("words", "hypothesis_words", "substitutions", "deletions", "insertions", "errors")
    counts = {key: sum(int(score[key]) for score in scores) for key in keys}
    counts["wer"] = counts["errors"] / max(1, counts["words"])
    return counts


def _summarize(meetings: list[dict]) -> dict:
    chunks = [row for meeting in meetings for row in meeting["chunks"]]
    rules = {
        "ordinary": lambda row: row["ordinary"],
        "rescue_all": lambda row: row["rescue"],
        "rescue_word_gain_gated": lambda row: row["rescue"] if row["accept_rescue"] else row["ordinary"],
        "rescue_hard_cut_only": lambda row: row["rescue"] if row["reason"] == "hard_cut" else row["ordinary"],
    }
    scores = {name: _sum_scores([select(row) for row in chunks])
              for name, select in rules.items()}
    regressions = {name: sum(select(row)["errors"] > row["ordinary"]["errors"]
                             for row in chunks)
                   for name, select in rules.items()}
    return {
        "meeting_ids": [meeting["meeting_id"] for meeting in meetings],
        "selected_chunks": len(chunks),
        "selected_audio_s": sum(row["duration_s"] for row in chunks),
        "hard_cut_chunks": sum(row["reason"] == "hard_cut" for row in chunks),
        "sparse_chunks": sum(row["reason"] == "sparse_speech" for row in chunks),
        "scores": scores,
        "regressed_chunks": regressions,
        "ordinary_elapsed_s": sum(meeting["ordinary_elapsed_s"] for meeting in meetings),
        "rescue_elapsed_s": sum(meeting["rescue_elapsed_s"] for meeting in meetings),
    }


def main() -> None:
    runs = {meeting_id: json.loads(path.read_text(encoding="utf-8"))
            for meeting_id, path in INPUTS.items()}
    if any(run["meeting_id"] != meeting_id for meeting_id, run in runs.items()):
        raise ValueError("Meeting ID mismatch in paired retry results")
    result = {
        "experiment": "paired_cpu_base_targeted_retry",
        "model": "Whisper base CPU int8, different from product auto/turbo",
        "metric": "exact word-level Levenshtein on reference-selected chunk intervals; not whole-meeting tcWER",
        "selection": "reference-blind suspect chunks from product draft word density, RMS, and hard-cut rule; max 6 per meeting",
        "hard_cut_only_rule": "derived from IN1009 development run, frozen before IN1007 and IN1014",
        "timing_caveat": "CPU/GPU contention from concurrent user app and source experiment; raw times are not clean latency measurements",
        "development": _summarize([runs["IN1009"]]),
        "holdout": _summarize([runs["IN1007"], runs["IN1014"]]),
        "all": _summarize(list(runs.values())),
        "per_meeting": {meeting_id: _summarize([run]) for meeting_id, run in runs.items()},
        "input_paths": {key: str(value) for key, value in INPUTS.items()},
    }
    OUTPUT.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for name in ("development", "holdout", "all"):
        group = result[name]
        print(name, group["selected_chunks"], "chunks,", group["scores"]["ordinary"]["words"],
              "reference words")
        for rule, score in group["scores"].items():
            print(f"  {rule}: {score['errors']}/{score['words']} = {score['wer']:.2%}, "
                  f"{group['regressed_chunks'][rule]} chunk regressions")
    print(OUTPUT)


if __name__ == "__main__":
    main()
