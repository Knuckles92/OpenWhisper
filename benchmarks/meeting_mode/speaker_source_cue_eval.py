"""AMI source-separation proxy using individual headset energy at word times.

This measures how much a distinct source can identify the active speaker. It
does not measure diarization or ASR accuracy. The letter-to-headset mapping is
fit on the first 25% of a meeting's annotated words and frozen for the rest.

Run ``python -m benchmarks.meeting_mode.speaker_source_cue_eval --download``.
The official AMI headset recordings are CC BY 4.0 and stay in ignored data/.
"""
from __future__ import annotations

import argparse
import bisect
from concurrent.futures import ThreadPoolExecutor
import itertools
import json
from pathlib import Path
import urllib.request
import wave

import numpy as np

from benchmarks.meeting_mode.ami import annotation_root, parse_reference_words

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "benchmarks/meeting_mode/data/ami"
OUTPUT = ROOT / "benchmarks/meeting_mode/results/speaker_source_cue_eval.json"
BASE_URL = "https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus"
MEETINGS = ("IN1009", "IN1005")


def headset_path(meeting, channel):
    return DATA / "audio/individual" / f"{meeting}.Headset-{channel}.wav"


def ensure_audio(meeting):
    def one(channel):
        destination = headset_path(meeting, channel)
        if destination.exists():
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        url = f"{BASE_URL}/{meeting}/audio/{meeting}.Headset-{channel}.wav"
        partial = destination.with_suffix(".wav.part")
        request = urllib.request.Request(url, headers={"User-Agent": "OpenWhisper/benchmark"})
        try:
            with urllib.request.urlopen(request, timeout=90) as response, partial.open("wb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
            partial.replace(destination)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, range(4)))


def load_audio(meeting):
    channels = []
    rate = None
    for channel in range(4):
        with wave.open(str(headset_path(meeting, channel)), "rb") as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise ValueError("Expected mono 16-bit headset WAV")
            if rate is not None and (wav.getframerate() != rate or wav.getnframes() != len(channels[0])):
                raise ValueError("Headsets are not time aligned")
            rate = wav.getframerate()
            samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
            channels.append(samples)
    return channels, rate


def run_meeting(meeting, external_mapping=None, external_majority=None):
    channels, rate = load_audio(meeting)
    words = parse_reference_words(annotation_root(DATA), meeting)
    speakers = sorted({w.speaker for w in words})
    if len(speakers) != 4:
        raise ValueError(f"Expected four speakers in {meeting}: {speakers}")
    starts = {s: [w.start_s for w in words if w.speaker == s] for s in speakers}
    ends = {s: [w.end_s for w in words if w.speaker == s] for s in speakers}
    rows = []
    for word in words:
        duration = word.end_s - word.start_s
        if duration < .08:
            continue
        midpoint = (word.start_s + word.end_s) / 2
        # A fixed 120 ms centered window avoids including the neighboring
        # speaker on very short turns and does not use transcript content.
        half_window = min(.06, duration / 2)
        a = max(0, int((midpoint - half_window) * rate))
        b = min(len(channels[0]), int((midpoint + half_window) * rate))
        if b <= a:
            continue
        energy = []
        for channel in channels:
            excerpt = channel[a:b].astype(np.float64)
            energy.append(float(np.dot(excerpt, excerpt) / len(excerpt)))
        overlap = False
        for speaker in speakers:
            if speaker == word.speaker:
                continue
            index = bisect.bisect_right(starts[speaker], midpoint) - 1
            if index >= 0 and ends[speaker][index] >= midpoint:
                overlap = True
                break
        ranking = sorted(range(4), key=lambda i: energy[i], reverse=True)
        margin_db = 10 * np.log10((energy[ranking[0]] + 1) / (energy[ranking[1]] + 1))
        rows.append({"speaker": word.speaker, "start_s": word.start_s,
                     "channel": ranking[0], "margin_db": float(margin_db),
                     "overlap": overlap})
    if external_mapping is None:
        boundary = len(rows) // 4
        train, test = rows[:boundary], rows[boundary:]
        mapping = max(itertools.permutations(range(4)), key=lambda perm: sum(
            row["channel"] == perm[speakers.index(row["speaker"])]
            for row in train if not row["overlap"]))
        majority = max(speakers, key=lambda s: sum(row["speaker"] == s for row in train))
    else:
        train, test = [], rows
        mapping = tuple(external_mapping[s] for s in speakers)
        majority = external_majority
    channel_to_speaker = {channel: speaker for speaker, channel in zip(speakers, mapping)}

    def score(selected):
        if not selected:
            return {"words": 0}
        correct = sum(channel_to_speaker[r["channel"]] == r["speaker"] for r in selected)
        majority_correct = sum(majority == r["speaker"] for r in selected)
        return {"words": len(selected), "source_accuracy": correct / len(selected),
                "majority_baseline_accuracy": majority_correct / len(selected),
                "mean_margin_db": sum(r["margin_db"] for r in selected) / len(selected),
                "per_speaker": {speaker: {"words": sum(r["speaker"] == speaker for r in selected),
                                         "accuracy": sum(r["speaker"] == speaker and
                                                         channel_to_speaker[r["channel"]] == speaker
                                                         for r in selected) /
                                                     max(1, sum(r["speaker"] == speaker for r in selected))}
                                for speaker in speakers}}
    return {"meeting": meeting, "mapping": dict(zip(speakers, mapping)),
            "majority_speaker": majority,
            "calibration_words": len(train), "test_words": len(test),
            "test_all": score(test),
            "test_isolated": score([r for r in test if not r["overlap"]]),
            "test_overlap": score([r for r in test if r["overlap"]]),
            "test_high_margin": score([r for r in test if r["margin_db"] >= 3]),
            "time_split": ("first 25% annotated words for mapping, remaining 75% test"
                           if external_mapping is None else
                           "no calibration on this meeting; all words tested using prior-meeting mapping")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--meetings", nargs="+", default=list(MEETINGS))
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    for meeting in args.meetings:
        if args.download:
            ensure_audio(meeting)
        if any(not headset_path(meeting, c).exists() for c in range(4)):
            parser.error(f"Missing individual headset audio for {meeting}; pass --download")
    results = [run_meeting(meeting) for meeting in args.meetings]
    cross_meeting = None
    if len(args.meetings) >= 2:
        cross_meeting = run_meeting(args.meetings[1], results[0]["mapping"],
                                    results[0]["majority_speaker"])
    report = {"protocol": "Individual-headset RMS at AMI annotated word midpoints; train channel-to-speaker mapping on first 25%, test last 75%.",
              "interpretation": "Source cue proxy only. The product has mic/loopback, not four wearer headsets; mono diarization and transcript gains are unmeasured.",
              "results": results,
              "cross_meeting_holdout": cross_meeting}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
