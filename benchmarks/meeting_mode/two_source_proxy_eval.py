"""Two-source AMI mic/remote proxy, calibrated on one meeting and held out on another.

Headset 0 is treated as "Me" and the mean of headsets 1-3 as a synthetic
"Others" stream. It is not real OS loopback. A mic-vs-others energy threshold
is selected from the first 25% of IN1009 annotated words, then frozen for all
IN1005 words. Only source identity is evaluated, not ASR or diarization.
"""
from __future__ import annotations

import bisect
import json
from pathlib import Path

import numpy as np

from benchmarks.meeting_mode.ami import annotation_root, parse_reference_words
from benchmarks.meeting_mode.speaker_source_cue_eval import DATA, ROOT, load_audio

OUTPUT = ROOT / "benchmarks/meeting_mode/results/two_source_proxy_eval.json"


def rows(meeting):
    channels, rate = load_audio(meeting)
    words = parse_reference_words(annotation_root(DATA), meeting)
    speakers = sorted({w.speaker for w in words})
    starts = {s: [w.start_s for w in words if w.speaker == s] for s in speakers}
    ends = {s: [w.end_s for w in words if w.speaker == s] for s in speakers}
    result = []
    for word in words:
        duration = word.end_s - word.start_s
        if duration < .08:
            continue
        midpoint = (word.start_s + word.end_s) / 2
        half = min(.06, duration / 2)
        a = max(0, int((midpoint - half) * rate))
        b = min(len(channels[0]), int((midpoint + half) * rate))
        if b <= a:
            continue
        mic = channels[0][a:b].astype(np.float64)
        others = (channels[1][a:b].astype(np.float64) +
                  channels[2][a:b].astype(np.float64) +
                  channels[3][a:b].astype(np.float64)) / 3
        mic_power = np.dot(mic, mic) / len(mic)
        other_power = np.dot(others, others) / len(others)
        ratio_db = float(10 * np.log10((mic_power + 1) / (other_power + 1)))
        overlap = any(
            (index := bisect.bisect_right(starts[s], midpoint) - 1) >= 0 and
            ends[s][index] >= midpoint
            for s in speakers if s != word.speaker)
        result.append({"me": word.speaker == "A", "ratio_db": ratio_db,
                       "overlap": overlap})
    return result


def score(rows_, threshold):
    if not rows_:
        return {"words": 0}
    me = [r for r in rows_ if r["me"]]
    others = [r for r in rows_ if not r["me"]]
    tp = sum(r["ratio_db"] >= threshold for r in me)
    tn = sum(r["ratio_db"] < threshold for r in others)
    sensitivity = tp / len(me) if me else 0
    specificity = tn / len(others) if others else 0
    return {"words": len(rows_), "me_words": len(me), "others_words": len(others),
            "accuracy": (tp + tn) / len(rows_), "me_recall": sensitivity,
            "others_recall": specificity,
            "balanced_accuracy": (sensitivity + specificity) / 2}


def main():
    train_source = rows("IN1009")
    train = [r for r in train_source[:len(train_source) // 4] if not r["overlap"]]
    # Select on a coarse fixed grid, taking the lowest threshold on ties.
    thresholds = [step / 2 for step in range(-40, 81)]
    threshold = max(thresholds, key=lambda t: score(train, t)["balanced_accuracy"])
    test_dev = train_source[len(train_source) // 4:]
    test_holdout = rows("IN1005")
    report = {"protocol": "IN1009 first-quarter isolated words select mic/others RMS ratio threshold; all IN1005 words are a separate-meeting holdout.",
              "threshold_db": threshold, "calibration": score(train, threshold),
              "IN1009_later": {"all": score(test_dev, threshold),
                                "isolated": score([r for r in test_dev if not r["overlap"]], threshold),
                                "overlap": score([r for r in test_dev if r["overlap"]], threshold)},
              "IN1005_holdout": {"all": score(test_holdout, threshold),
                                  "isolated": score([r for r in test_holdout if not r["overlap"]], threshold),
                                  "overlap": score([r for r in test_holdout if r["overlap"]], threshold)},
              "limitations": ["Synthetic mean of three other headsets is not actual system loopback.",
                              "Headset 0 is a close mic with different leakage than consumer microphones.",
                              "Timed human words provide evaluation labels; word alignment and ASR accuracy are not measured."]}
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
