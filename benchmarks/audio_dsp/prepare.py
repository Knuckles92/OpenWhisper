"""Derive paired stress cases from an existing audio_path/reference manifest.

python -m benchmarks.audio_dsp.prepare corpus.json --output .tmp/dsp/corpus
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from benchmarks.audio_dsp.audio import FRAME, RATE, db, read_audio, rms, write_audio
from benchmarks.provenance import file_identity


def intervals_for(clean):
    """Clean-energy annotation for diagnostics only; never passed to the DSP."""
    threshold = max(-65, db(float(np.max(np.abs(clean)))) - 40)
    intervals = []
    for start in range(0, len(clean), FRAME):
        end = min(start + FRAME, len(clean))
        if db(rms(clean[start:end])) > threshold:
            if intervals and abs(intervals[-1][1] - start / RATE) < 1e-9:
                intervals[-1][1] = end / RATE
            else:
                intervals.append([start / RATE, end / RATE])
    return intervals


def noise_signals(length, rng):
    """Explicit synthetic proxies, not recordings of a real fan or keyboard."""
    time = np.arange(length) / RATE
    fan = rng.normal(0, 0.25, length) + np.sin(2 * np.pi * 60 * time)
    fan += 0.4 * np.sin(2 * np.pi * 120 * time)
    keyboard = np.zeros(length)
    for start in range(RATE // 5, length, RATE // 3):
        size = min(RATE // 40, length - start)
        keyboard[start : start + size] += rng.normal(0, 1, size) * np.exp(
            -np.arange(size) / (RATE * 0.004)
        )
    return fan.astype(np.float32), keyboard.astype(np.float32)


def mix_at_snr(clean, noise, snr_db, mask):
    power = rms(clean[mask])
    if power <= 0 or rms(noise[mask]) <= 0:
        raise ValueError("SNR mixing needs non-silent speech and noise")
    noisy = clean + noise * (power / rms(noise[mask])) * 10 ** (-snr_db / 20)
    # Scale the pair together; clipping would introduce an uncontrolled distortion.
    gain = min(1, 0.95 / max(float(np.max(np.abs(noisy))), 1e-12))
    return (noisy * gain).astype(np.float32), (clean * gain).astype(np.float32)


def prepare(args):
    if not np.isfinite(args.snr_db):
        raise ValueError("SNR must be finite")
    source = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    clips = [
        c for c in source["clips"] if not args.group or c.get("group") == args.group
    ]
    clips = [
        c
        for c in clips
        if isinstance(c.get("reference"), str) and c["reference"].strip()
    ]
    clips = clips[: args.limit]
    if not clips or args.limit < 1:
        raise ValueError("Select at least one transcribed speech clip")
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "manifest.json").exists():
        raise ValueError(
            "Choose a new corpus directory; an existing manifest will not be overwritten"
        )
    rng = np.random.default_rng(args.seed)
    recorded_noise = read_audio(args.noise_file) if args.noise_file else None
    inputs = [args.manifest, *([args.noise_file] if args.noise_file else [])]
    output = {
        "schema": 1,
        "sample_rate": RATE,
        "seed": args.seed,
        "snr_db": args.snr_db,
        "source_manifest": source,
        "clips": [],
        "notes": [
            "Derived conditions reuse source utterances; they are paired stress tests, not independent samples.",
            "Quiet/variable gain is not real whispered speech. Fan/keyboard noise is synthetic unless a noise file is supplied.",
            "Speech intervals use clean energy, not human timestamps or a production VAD.",
        ],
    }
    for index, clip in enumerate(clips):
        path = (args.manifest.parent / clip["audio_path"]).resolve()
        inputs.append(path)
        clean = np.pad(read_audio(path), (RATE // 4, RATE // 2))
        intervals = intervals_for(clean)
        mask = np.zeros(len(clean), dtype=bool)
        for start, end in intervals:
            mask[round(start * RATE) : round(end * RATE)] = True
        if not mask.any():
            raise ValueError(f"No speech energy in {path}")
        quiet = clean * 10 ** (-18 / 20)
        variable = clean.copy()
        variable[RATE // 4 : len(variable) // 2] *= 10 ** (-18 / 20)
        fan, keyboard = noise_signals(len(clean), rng)
        variants = {
            "clean": (clean, clean),
            "quiet": (quiet, quiet),
            "variable_level": (variable, variable),
        }
        for name, noise in (("fan_proxy", fan), ("keyboard_proxy", keyboard)):
            variants[name] = mix_at_snr(clean, noise, args.snr_db, mask)
        variants["quiet_noisy"] = mix_at_snr(quiet, fan + keyboard, args.snr_db, mask)
        if recorded_noise is not None:
            offset = int(rng.integers(len(recorded_noise)))
            noise = np.resize(np.roll(recorded_noise, offset), len(clean))
            variants["recorded_noise"] = mix_at_snr(clean, noise, args.snr_db, mask)
        for condition, (audio, reference_audio) in variants.items():
            stem = f"{index:04d}-{condition}"
            write_audio(args.output / f"{stem}.wav", audio)
            write_audio(args.output / f"{stem}-reference.wav", reference_audio)
            output["clips"].append(
                {
                    "id": stem,
                    "source_id": clip.get("id", clip["audio_path"]),
                    "speaker_id": clip.get("speaker_id"),
                    "group": condition,
                    "audio_path": f"{stem}.wav",
                    "reference_audio_path": f"{stem}-reference.wav",
                    "reference": clip["reference"],
                    "speech_intervals_s": intervals,
                }
            )
    for condition, audio in (
        ("silence", np.zeros(3 * RATE, dtype=np.float32)),
        ("noise_only", noise_signals(3 * RATE, rng)[0] * 0.005),
    ):
        write_audio(args.output / f"{condition}.wav", audio)
        output["clips"].append(
            {
                "id": condition,
                "source_id": condition,
                "group": condition,
                "audio_path": f"{condition}.wav",
                "reference": "",
                "speech_intervals_s": [],
            }
        )
    output["input_sha256"] = {str(p.resolve()): file_identity(p) for p in inputs}
    output["generated_sha256"] = {
        p.name: file_identity(p) for p in sorted(args.output.glob("*.wav"))
    }
    (args.output / "manifest.json").write_text(
        json.dumps(output, indent=2, allow_nan=False), encoding="utf-8"
    )
    print(f"Prepared {len(output['clips'])} clips in {args.output}")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("manifest", type=Path)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--group", help="Select an existing clean-speech group")
    result.add_argument("--limit", type=int, default=8)
    result.add_argument("--seed", type=int, default=45)
    result.add_argument("--snr-db", type=float, default=10)
    result.add_argument(
        "--noise-file",
        type=Path,
        help="Optional captured background noise, mixed at the same SNR",
    )
    return result


if __name__ == "__main__":
    prepare(parser().parse_args())
