"""Benchmark DSP and optional installed production ASR adapters on paired clips.

python -m benchmarks.audio_dsp.run manifest.json --output .tmp/dsp/run
No model downloads, microphone access, API calls, or app-settings changes.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import numpy as np

from benchmarks.audio_dsp.audio import (
    FRAME,
    RATE,
    read_audio,
    signal_metrics,
    write_audio,
)
from benchmarks.audio_dsp.processors import (
    PROFILES,
    make_processor,
    processor_identity,
    render,
)
from benchmarks.meeting_mode.metrics import score_text
from benchmarks.provenance import file_identity, identity, model_identity


def make_backend(name, device):
    from services import native_libraries

    native_libraries.register_cuda_dll_directories()
    native_libraries.preload_cuda_libraries()
    from services.local_asr.catalog import MODELS
    from transcriber.local_backend import LocalWhisperBackend
    from transcriber.optional_backend import LocalSpeechBackend

    if name in MODELS:
        backend = LocalSpeechBackend(MODELS[name].backend, name, device)
        backend.reload_model()
    else:
        backend = LocalWhisperBackend(
            name, device=device, compute_type="int8" if device == "cpu" else "float16"
        )
    if not backend.is_available() or backend.device != device:
        message = backend.device_info
        backend.cleanup()
        raise RuntimeError(f"Requested {device}, loaded {backend.device}: {message}")
    return backend


def frame_summary(runs, budget_ms):
    values = [value for run in runs for value in (run.get("frame_ms") or [])]
    result = {
        "algorithmic_latency_ms": runs[0]["algorithmic_latency_ms"],
        "timing_scope": "Python frame calls including native DSP"
        if values
        else "offline AudioForge simulation; no callback timing",
        "frame_duration_ms": FRAME / RATE * 1000,
        "frame_samples": len(values),
        "budget_ms": budget_ms,
        "frame_p50_ms": float(np.percentile(values, 50)) if values else None,
        "frame_p95_ms": float(np.percentile(values, 95)) if values else None,
        "frame_p99_ms": float(np.percentile(values, 99)) if values else None,
        "frame_max_ms": max(values) if values else None,
        "frames_over_budget": sum(t > budget_ms for t in values) if values else None,
        "frames_over_deadline": sum(t > FRAME / RATE * 1000 for t in values)
        if values
        else None,
        "runs": runs,
    }
    result["p95_within_budget"] = (
        result["frame_p95_ms"] <= budget_ms if values else None
    )
    return result


def render_audioforge(audio, args, profile, directory):
    if not args.audioforge_python or not args.audioforge_root:
        raise ValueError(
            "AudioForge profiles require --audioforge-python and --audioforge-root (built upstream checkout)"
        )
    directory.mkdir(parents=True, exist_ok=True)
    source, output, metadata = (
        directory / name for name in ("input.npy", "output.npy", "metadata.json")
    )
    np.save(source, audio, allow_pickle=False)
    worker = Path(__file__).with_name("audioforge_worker.py")
    started = time.perf_counter()
    process = subprocess.run(
        [
            str(args.audioforge_python.resolve()),
            str(worker),
            str(source),
            str(output),
            str(metadata),
            "--root",
            str(args.audioforge_root.resolve()),
            "--profile",
            profile,
        ],
        capture_output=True,
        text=True,
        timeout=args.timeout_s,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if process.returncode:
        raise RuntimeError(f"AudioForge worker failed: {process.stderr[-3000:]}")
    result = json.loads(metadata.read_text(encoding="utf-8"))
    result["worker_wall_ms"] = (time.perf_counter() - started) * 1000
    return np.load(output, allow_pickle=False), result


def aggregate(rows, profile, model):
    selected = [
        r
        for r in rows
        if r["profile"] == profile
        and model in r.get("asr", {})
        and "error" not in r["asr"][model]
    ]
    result = {}
    for group in sorted({r["group"] for r in selected}):
        members = [r for r in selected if r["group"] == group]
        counts = {
            key: sum(r["asr"][model]["score"][key] for r in members)
            for key in ("words", "errors", "substitutions", "deletions", "insertions")
        }
        seconds = sum(r["duration_s"] for r in members)
        result[group] = {
            **counts,
            "clips": len(members),
            "wer": counts["errors"] / counts["words"] if counts["words"] else None,
            "hallucinated_words": counts["insertions"] if not counts["words"] else None,
            "decode_rtf": sum(r["asr"][model]["decode_s"] for r in members) / seconds,
        }
    return result


def summarize(rows, profiles, models, expected_clips):
    result = {}
    raw = {r["id"]: r for r in rows if r["profile"] == "raw"}
    for profile in profiles:
        selected = [r for r in rows if r["profile"] == profile]
        entry = {
            "expected_clips": expected_clips,
            "completed_dsp_clips": sum("error" not in r for r in selected),
            "models": {},
        }
        for model in models:
            paired = [
                r
                for r in selected
                if model in r.get("asr", {})
                and "error" not in r["asr"][model]
                and r["id"] in raw
                and model in raw[r["id"]].get("asr", {})
                and "error" not in raw[r["id"]]["asr"][model]
            ]
            candidate = aggregate(paired, profile, model)
            baseline = aggregate([raw[r["id"]] for r in paired], "raw", model)
            for group, metrics in candidate.items():
                base = baseline[group]
                metrics["raw_wer"] = base["wer"]
                metrics["wer_delta_pp"] = (
                    100 * (metrics["wer"] - base["wer"])
                    if metrics["wer"] is not None
                    else None
                )
                metrics["deletion_delta"] = metrics["deletions"] - base["deletions"]
                metrics["insertion_delta"] = metrics["insertions"] - base["insertions"]
            entry["models"][model] = {
                "paired_clips": len(paired),
                "complete": len(paired) == expected_clips,
                "groups": candidate,
            }
        result[profile] = entry
    return result


def load_clips(manifest_path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    clips = manifest["clips"]
    ids = [clip["id"] for clip in clips]
    if not clips or len(set(ids)) != len(ids):
        raise ValueError("Corpus needs nonempty clips with unique IDs")
    inputs = [manifest_path]
    loaded = []
    for index, clip in enumerate(clips):
        if not isinstance(clip.get("reference"), str):
            raise ValueError("Every clip needs a reference string (empty for silence)")
        path = (manifest_path.parent / clip["audio_path"]).resolve()
        reference_path = (
            (manifest_path.parent / clip["reference_audio_path"]).resolve()
            if clip.get("reference_audio_path")
            else None
        )
        expected_hashes = manifest.get("generated_sha256", {})
        for item in (path, *([reference_path] if reference_path else [])):
            if (
                item.name in expected_hashes
                and file_identity(item) != expected_hashes[item.name]
            ):
                raise ValueError(f"Corpus hash mismatch: {item}")
            inputs.append(item)
        audio = read_audio(path)
        reference = read_audio(reference_path) if reference_path else None
        if reference is not None and reference.shape != audio.shape:
            raise ValueError("Reference audio must have the same duration as input")
        # Artifact names use numeric indexes, never arbitrary corpus IDs.
        loaded.append((index, clip, path, audio, reference))
    return loaded, inputs


def run(args):
    if args.repeats < 1 or not np.isfinite(args.budget_ms) or args.budget_ms <= 0:
        raise ValueError("Repeat count and latency budget must be positive")
    profiles = list(dict.fromkeys(["raw", *args.profiles.split(",")]))
    if set(profiles) - set(PROFILES):
        raise ValueError(f"Profiles must be selected from {PROFILES}")
    models = list(dict.fromkeys(filter(None, args.models.split(","))))
    loaded, inputs = load_clips(args.manifest)
    if args.rnnoise_library:
        inputs.append(args.rnnoise_library)
    args.output.mkdir(parents=True, exist_ok=True)
    report_path = args.output / "results.json"
    if report_path.exists():
        raise ValueError("Choose a new run directory; results will not be overwritten")
    upstream = None
    if args.audioforge_root:
        root = args.audioforge_root.resolve()
        upstream = {
            str(p.relative_to(root)): file_identity(p)
            for p in sorted((root / "rust-core" / "src").rglob("*.rs"))
        }
        for name in ("Cargo.lock", "pyproject.toml"):
            inputs.append(root / name)
    report = {
        "schema": 1,
        "issue": "https://github.com/Knuckles92/OpenWhisper/issues/45",
        "accuracy_requested": bool(models),
        "accuracy_measured": False,
        "sample_rate": RATE,
        "models": {},
        "results": [],
        "provenance": identity(
            inputs=inputs,
            settings={
                "profiles": profiles,
                "models": models,
                "device": args.device,
                "repeats": args.repeats,
                "budget_ms": args.budget_ms,
            },
        ),
        "upstream_source_sha256": upstream,
        "runner_sha256": file_identity(__file__),
        "limitations": [
            "Frame timing excludes devices, recorder queues, resampling and ASR; it is not end-to-end latency.",
            "AudioForge simulation exposes whole-capture runtime, not per-callback p95.",
            "Energy thresholds are diagnostics, not production VAD false-trigger counts.",
            "Signal level and SI-SDR do not establish transcription accuracy.",
            "Derived conditions reuse speakers/utterances; group WER is word-weighted; repeats do not increase sample size.",
        ],
    }

    def save():
        report["accuracy_measured"] = any(
            "score" in result
            for row in report["results"]
            for result in row["asr"].values()
        )
        report["summary"] = summarize(report["results"], profiles, models, len(loaded))
        report_path.write_text(
            json.dumps(report, indent=2, allow_nan=False), encoding="utf-8"
        )

    with ExitStack() as context:
        context.enter_context(
            patch.dict(
                os.environ,
                {
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "OPENWHISPER_DATA_DIR": str((args.output / "settings").resolve()),
                },
            )
        )
        backends = {}
        for model in models:
            backend = None
            try:
                started = time.perf_counter()
                backend = make_backend(model, args.device)
                context.callback(backend.cleanup)
                report["models"][model] = {
                    "load_s": time.perf_counter() - started,
                    "identity": model_identity(backend),
                }
                started = time.perf_counter()
                backend.transcribe(str(loaded[0][2]))
                report["models"][model]["warmup_s"] = time.perf_counter() - started
                backends[model] = backend
            except Exception as exc:
                report["models"][model] = {"error": str(exc)}
        for index, clip, _path, audio, reference in loaded:
            # Alternating order reduces systematic first/last inference effects.
            ordered = profiles if index % 2 == 0 else list(reversed(profiles))
            for profile in ordered:
                print(f"DSP {clip['id']} {profile}", flush=True)
                row = {
                    "id": clip["id"],
                    "source_id": clip.get("source_id", clip["id"]),
                    "group": clip.get("group", "all"),
                    "profile": profile,
                    "duration_s": len(audio) / RATE,
                    "asr": {},
                }
                try:
                    timings = []
                    outputs = []
                    for repeat in range(args.repeats):
                        if profile.startswith("audioforge_"):
                            output, timing = render_audioforge(
                                audio,
                                args,
                                profile,
                                args.output
                                / "workers"
                                / str(index)
                                / profile
                                / str(repeat),
                            )
                        else:
                            output, timing = render(
                                audio,
                                lambda p=profile: make_processor(
                                    p, args.rnnoise_library
                                ),
                            )
                        outputs.append(output)
                        timings.append(timing)
                    row["dsp"] = frame_summary(timings, args.budget_ms)
                    row["processor"] = processor_identity(profile, args.rnnoise_library)
                    row["repeat_max_sample_delta"] = max(
                        float(np.max(np.abs(out - outputs[0]))) for out in outputs
                    )
                    row["signal"] = signal_metrics(
                        audio, outputs[0], reference, clip.get("speech_intervals_s", [])
                    )
                    output_path = args.output / "audio" / f"{index:04d}-{profile}.wav"
                    write_audio(output_path, outputs[0])
                    row["output_audio_path"] = str(output_path.resolve())
                    row["output_sha256"] = file_identity(output_path)
                    for model in models:
                        if model not in backends:
                            row["asr"][model] = {
                                "error": report["models"][model]["error"]
                            }
                            continue
                        try:
                            started = time.perf_counter()
                            transcript = backends[model].transcribe(
                                str(output_path.resolve())
                            )
                            row["asr"][model] = {
                                "transcript": transcript,
                                "decode_s": time.perf_counter() - started,
                                "score": score_text(clip["reference"], transcript),
                            }
                        except Exception as exc:
                            row["asr"][model] = {"error": str(exc)}
                except Exception as exc:
                    row["error"] = str(exc)
                report["results"].append(row)
                save()
    report["complete"] = not any(
        "error" in row or any("error" in value for value in row["asr"].values())
        for row in report["results"]
    )
    save()
    print(f"Report: {report_path}")
    return 0 if report["complete"] else 1


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("manifest", type=Path)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--profiles", default="raw,highpass,gate_agc")
    result.add_argument(
        "--models",
        default="",
        help="Comma-separated installed Whisper aliases/paths or local ASR catalog keys; omitted = DSP only",
    )
    result.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    result.add_argument(
        "--repeats",
        type=int,
        default=3,
        help="DSP replays, fresh state each time; ASR decodes once per variant",
    )
    result.add_argument("--budget-ms", type=float, default=5)
    result.add_argument("--rnnoise-library", type=Path)
    result.add_argument("--audioforge-python", type=Path)
    result.add_argument("--audioforge-root", type=Path)
    result.add_argument("--timeout-s", type=float, default=120)
    return result


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
