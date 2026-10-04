"""Training-meeting-only calibration of cached AMI evidence-check scores.

The split is IN1005 + IN1009 for threshold selection, IN1007 for evaluation.
This is an exploratory companion to evidence_check_eval.py, not a blinded
prospective validation. All labels come from a prior model audit.
"""
from __future__ import annotations

import json
from pathlib import Path

from benchmarks.meeting_mode.evidence_check_eval import DEFAULT_OUTPUT

TRAIN = frozenset(("IN1005", "IN1009"))
TEST = "IN1007"


def candidates():
    # One-dimensional gates only, to limit overfitting on a tiny training set.
    for field in ("support", "acceptance"):
        for threshold in (.1, .2, .3, .4, .5, .6, .7, .8, .9):
            yield field, ">=", threshold
    for threshold in (.1, .2, .3, .4, .5, .6, .7, .8, .9):
        yield "asr_uncertain", "<=", threshold


def passes(row, gate):
    field, direction, threshold = gate
    score = row["review"]["scores"].get(field)
    return score is not None and (score >= threshold if direction == ">=" else score <= threshold)


def metrics(rows, gate):
    good = [r for r in rows if r["label"] == "supported"]
    nongood = [r for r in rows if r["label"] != "supported"]
    retained = [r for r in rows if passes(r, gate)]
    retained_good = [r for r in retained if r["label"] == "supported"]
    retained_bad = [r for r in retained if r["label"] in ("overstated", "contradicted")]
    flagged_nongood = [r for r in nongood if not passes(r, gate)]
    sensitivity = len(retained_good) / len(good) if good else 0
    specificity = len(flagged_nongood) / len(nongood) if nongood else 0
    return {"cases": len(rows), "supported": len(good), "non_supported": len(nongood),
            "retained": len(retained), "retained_supported": len(retained_good),
            "retained_overstated": len(retained_bad),
            "retained_precision_vs_audit": len(retained_good) / len(retained) if retained else None,
            "supported_recall": sensitivity,
            "non_supported_flag_recall": specificity,
            "balanced_accuracy": (sensitivity + specificity) / 2}


def select_gate(train):
    # Require a useful nonempty record. Optimize balanced accuracy over the
    # training meetings, then retention precision, then supported recall.
    eligible = []
    for gate in candidates():
        result = metrics(train, gate)
        if result["retained"] < 3:
            continue
        key = (result["balanced_accuracy"],
               result["retained_precision_vs_audit"],
               result["supported_recall"])
        eligible.append((key, gate, result))
    return max(eligible, key=lambda entry: entry[0])


def main():
    source = json.loads(Path(DEFAULT_OUTPUT).read_text(encoding="utf-8"))
    rows = source["items"]
    train = [r for r in rows if r["meeting"] in TRAIN]
    test = [r for r in rows if r["meeting"] == TEST]
    _, gate, train_metrics = select_gate(train)
    report = {"protocol": "Choose a single-score threshold using IN1005+IN1009 labels only; evaluate unchanged on IN1007.",
              "caveat": "Exploratory meeting-separated split, chosen after score inspection; prior labels are a model audit, not human adjudication.",
              "source": str(DEFAULT_OUTPUT), "gate": {"field": gate[0], "direction": gate[1], "threshold": gate[2]},
              "train": train_metrics, "test": metrics(test, gate),
              "test_all_pass": metrics(test, ("support", ">=", 0.0))}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
