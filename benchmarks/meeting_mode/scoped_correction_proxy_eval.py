"""Real-meeting oracle proxy for one-occurrence vs meeting-wide corrections.

Find high-confidence single-word ASR/reference substitutions in the first
quarter of an AMI meeting: the neighboring two words must match exactly on
both sides. Treat each as a hypothetical user correction, then apply the
meeting-wide rule to the untouched final 75% of the same meeting. Score the
result against human annotations in 15-second windows. A one-occurrence edit
would leave this future portion unchanged. No actual human edits are logged;
these are oracle-derived candidates, not measured user behavior.
"""
from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
import json
from pathlib import Path

from benchmarks.meeting_mode.ami import annotation_root, parse_reference_words
from benchmarks.meeting_mode.metrics import edit_counts, normalize_tokens

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "benchmarks/meeting_mode/data/ami"
SOURCE = ROOT / "benchmarks/meeting_mode/results/auto-auto-draft-only-t5-m20-p50-offline"
OUTPUT = ROOT / "benchmarks/meeting_mode/results/scoped_correction_proxy_eval.json"
MEETINGS = ("IN1005", "IN1009", "IN1012")
WINDOW_S = 15


def windows(meeting):
    data = json.loads((SOURCE / f"{meeting}.json").read_text(encoding="utf-8"))
    words = parse_reference_words(annotation_root(DATA), meeting)
    total = int(data["duration_s"] // WINDOW_S) + 1
    ref = [[] for _ in range(total)]
    hyp = [[] for _ in range(total)]
    for word in words:
        ref[min(total - 1, int(word.start_s // WINDOW_S))].extend(normalize_tokens(word.text))
    for seg in data["draft_segments"]:
        hyp[min(total - 1, int(seg["start_s"] // WINDOW_S))].extend(normalize_tokens(seg["text"]))
    return ref, hyp


def candidates(ref_windows, hyp_windows, train_count):
    found = Counter()
    for ref, hyp in zip(ref_windows[:train_count], hyp_windows[:train_count]):
        matcher = SequenceMatcher(None, ref, hyp, autojunk=False)
        opcodes = matcher.get_opcodes()
        for i, (tag, a0, a1, b0, b1) in enumerate(opcodes):
            if tag != "replace" or a1 - a0 != 1 or b1 - b0 != 1:
                continue
            before = opcodes[i - 1] if i else None
            after = opcodes[i + 1] if i + 1 < len(opcodes) else None
            if not before or not after or before[0] != "equal" or after[0] != "equal":
                continue
            if before[2] - before[1] < 2 or after[2] - after[1] < 2:
                continue
            source, target = hyp[b0], ref[a0]
            if len(source) >= 3 and len(target) >= 3 and source != target:
                found[(source, target)] += 1
    return found


def test_rule(ref_windows, hyp_windows, source, target):
    before_errors = after_errors = reference_words = changed_occurrences = 0
    for ref, hyp in zip(ref_windows, hyp_windows):
        occurrences = hyp.count(source)
        if not occurrences:
            continue
        changed_occurrences += occurrences
        corrected = [target if token == source else token for token in hyp]
        before_errors += edit_counts(ref, hyp).errors
        after_errors += edit_counts(ref, corrected).errors
        reference_words += len(ref)
    return {"test_occurrences_changed": changed_occurrences,
            "errors_before_affected_windows": before_errors,
            "errors_after_affected_windows": after_errors,
            "delta_errors": after_errors - before_errors,
            "reference_words_affected_windows": reference_words}


def run_meeting(meeting):
    ref, hyp = windows(meeting)
    train_count = max(1, len(ref) // 4)
    proposals = candidates(ref, hyp, train_count)
    future_hyp = Counter(token for window in hyp[train_count:] for token in window)
    eligible = [(source, target, n) for (source, target), n in proposals.items()
                if future_hyp[source] >= 2]
    rows = []
    for source, target, n in sorted(eligible):
        rows.append({"source": source, "target": target,
                     "train_confirmed_substitutions": n,
                     **test_rule(ref[train_count:], hyp[train_count:], source, target)})
    base_counts = [edit_counts(a, b) for a, b in zip(ref[train_count:], hyp[train_count:])]
    return {"meeting": meeting, "train_windows": train_count,
            "test_windows": len(ref) - train_count,
            "future_reference_words": sum(map(len, ref[train_count:])),
            "future_baseline_errors": sum(c.errors for c in base_counts),
            "eligible_rules": len(rows),
            "rules_improved": sum(r["delta_errors"] < 0 for r in rows),
            "rules_regressed": sum(r["delta_errors"] > 0 for r in rows),
            "rules_tied": sum(r["delta_errors"] == 0 for r in rows),
            "rules": rows}


def main():
    meetings = [run_meeting(meeting) for meeting in MEETINGS]
    rules = [r for meeting in meetings for r in meeting["rules"]]
    summary = {"rules": len(rules), "improved": sum(r["delta_errors"] < 0 for r in rules),
               "regressed": sum(r["delta_errors"] > 0 for r in rules),
               "tied": sum(r["delta_errors"] == 0 for r in rules),
               "largest_regressions": sorted(rules, key=lambda r: r["delta_errors"], reverse=True)[:5],
               "largest_improvements": sorted(rules, key=lambda r: r["delta_errors"])[:5]}
    report = {"protocol": "Oracle correction candidates from first-quarter aligned human words; apply globally only to the remaining three quarters of the transcript; 15s WER windows.",
              "scope": "Three natural AMI meetings with cached production Whisper draft transcripts and human annotations.",
              "limitations": ["Correction candidates are derived from human reference; no actual user edits or acceptance rates are measured.",
                              "Greedy word alignment misses multiword errors and may still mispair overlapping speakers.",
                              "Each rule is evaluated separately; aggregate deltas are not a combined transcript edit."],
              "summary": summary, "meetings": meetings}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
