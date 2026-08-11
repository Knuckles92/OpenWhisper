"""Run real AMI meetings through the production Meeting Mode ASR path."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Meeting Mode normally starts through app_qt.py, whose import-time bootstrap
# registers the pip-installed CUDA DLL directories before CTranslate2 loads.
# The benchmark must follow that same native-runtime path or it silently falls
# back to CPU and measures a configuration the desktop app does not use.
import app_qt as _app_bootstrap  # noqa: E402,F401

from benchmarks.meeting_mode.ami import (  # noqa: E402
    MeetingSpec,
    annotation_root,
    audio_path,
    parse_reference_words,
    prepare_corpus,
    select_meetings,
)
from benchmarks.meeting_mode.metrics import (  # noqa: E402
    aggregate_scores,
    normalize_tokens,
    reference_overlap_stats,
    score_timed_transcript,
)
from meeting.asr.engine import DRAFT_PROMPT_WORDS, MeetingAsrEngine  # noqa: E402
from meeting.capture.spool import (  # noqa: E402
    DEFAULT_MAX_SEC,
    DEFAULT_TARGET_SEC,
    MIN_FLUSH_S,
    find_cut_point,
    resample_to_16k,
)
from meeting.interfaces import SpooledChunk  # noqa: E402
from meeting.persist.repository import SqlMeetingRepository  # noqa: E402
from services.database import DatabaseManager  # noqa: E402

BENCHMARK_VERSION = 3

# A deliberately demanding release gate: at least a full workday of natural
# meetings, strict WER (including overlap and fillers) below 30%, no individual
# meeting above 35%, and ample live-processing headroom.
MIN_GATE_MEETINGS = 10
MIN_GATE_AUDIO_HOURS = 8.0
MAX_GATE_MICRO_WER = 0.30
MAX_GATE_MEETING_WER = 0.35
MAX_GATE_RTF = 0.50


def _load_pcm_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav_file:
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        sample_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())
    if sample_width != 2:
        raise ValueError(f"Expected 16-bit PCM audio: {path}")
    audio = np.frombuffer(frames, dtype="<i2")
    if channels > 1:
        audio = audio.reshape(-1, channels).astype(np.int32).mean(axis=1).astype(np.int16)
    return np.ascontiguousarray(audio, dtype=np.int16), int(sample_rate)


def _chunk_ranges(
    audio: np.ndarray,
    sample_rate: int,
    target_sec: float = DEFAULT_TARGET_SEC,
    max_sec: float = DEFAULT_MAX_SEC,
) -> Iterable[tuple[int, int]]:
    """Yield the same quiet-aligned ranges used by the live spool writer."""
    position = 0
    max_samples = max(1, int(round(max_sec * sample_rate)))
    while position < audio.size:
        candidate = audio[position : position + max_samples]
        cut = find_cut_point(candidate, sample_rate, target_sec, max_sec)
        if cut is None:
            cut = candidate.size
        if cut <= 0:
            break
        end = min(audio.size, position + int(cut))
        if end - position < int(round(MIN_FLUSH_S * sample_rate)):
            break
        yield position, end
        position = end


def _write_chunk(path: Path, samples: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(np.ascontiguousarray(samples, dtype=np.int16).tobytes())


def _segment_dict(segment: Any) -> dict[str, Any]:
    return {
        "id": segment.segment_id,
        "start_s": float(segment.start_s),
        "end_s": float(segment.end_s),
        "text": segment.text,
        "channel": segment.channel,
    }


def _create_meeting(repo: SqlMeetingRepository, meeting_id: str, work_dir: Path, model: str) -> None:
    if repo.get_meeting(meeting_id) is not None:
        repo.delete_meeting(meeting_id)
    repo.create_meeting(
        id=meeting_id,
        title=f"AMI benchmark {meeting_id}",
        status="active",
        started_at=datetime.now(timezone.utc).isoformat(),
        host_token="benchmark-host",
        guest_token="benchmark-guest",
        cloud_enabled=False,
        spool_dir=str(work_dir),
        asr_model=model,
        state_json="{}",
    )


def decode_meeting(
    engine: MeetingAsrEngine,
    repo: SqlMeetingRepository,
    spec: MeetingSpec,
    source_path: Path,
    work_dir: Path,
    model_name: str,
    run_revisions: bool = True,
    target_sec: float = DEFAULT_TARGET_SEC,
    max_sec: float = DEFAULT_MAX_SEC,
    draft_prompt_words: int = DRAFT_PROMPT_WORDS,
) -> dict[str, Any]:
    """Decode one meeting using production chunking, ASR, and revision code."""
    pcm, source_rate = _load_pcm_wav(source_path)
    meeting_id = f"bench_{spec.meeting_id}"
    _create_meeting(repo, meeting_id, work_dir, model_name)
    engine.meeting_id = meeting_id
    engine._pending_revise.clear()
    engine._last_revised_frontier.clear()

    draft_segments: list[dict[str, Any]] = []
    draft_context: list[str] = []
    chunk_count = 0
    started = time.perf_counter()
    for seq, (start, end) in enumerate(
        _chunk_ranges(
            pcm,
            source_rate,
            target_sec=target_sec,
            max_sec=max_sec,
        )
    ):
        native = pcm[start:end]
        samples = resample_to_16k(native, source_rate)
        chunk_path = work_dir / f"loopback_{seq:05d}.wav"
        _write_chunk(chunk_path, samples)
        start_s = start / float(source_rate)
        duration_s = samples.size / 16000.0
        chunk_id = repo.register_chunk(
            meeting_id=meeting_id,
            channel="loopback",
            seq=seq,
            file_path=str(chunk_path),
            start_s=start_s,
            duration_s=duration_s,
            sample_rate=16000,
        )
        chunk = SpooledChunk(
            chunk_id=chunk_id,
            meeting_id=meeting_id,
            channel="loopback",
            seq=seq,
            file_path=str(chunk_path),
            start_s=start_s,
            duration_s=duration_s,
            sample_rate=16000,
        )
        prompt = " ".join(draft_context[-draft_prompt_words:]) \
            if draft_prompt_words > 0 else None
        decoded = engine._transcribe_chunk(
            chunk,
            beam_size=5,
            initial_prompt=prompt,
        )
        draft_segments.extend(_segment_dict(segment) for segment in decoded)
        for segment in decoded:
            draft_context.extend(normalize_tokens(segment.text))
        repo.commit_chunk_transcription(meeting_id, chunk_id, decoded)
        if run_revisions:
            engine.schedule_revise("loopback", start_s + duration_s)
            engine.run_pending_revises()
        chunk_count += 1
        if chunk_count % 25 == 0:
            elapsed = time.perf_counter() - started
            audio_done = (end / source_rate) / 60.0
            print(
                f"  {spec.meeting_id}: {audio_done:.1f} audio min, "
                f"{chunk_count} chunks, {elapsed / 60.0:.1f} wall min",
                flush=True,
            )

    if run_revisions:
        engine.run_pending_revises(force=True)
    final_segments = repo.get_segments(meeting_id, after_start_s=-1.0)
    elapsed_s = time.perf_counter() - started
    duration_s = pcm.size / float(source_rate)
    return {
        "meeting_id": spec.meeting_id,
        "description": spec.description,
        "audio_path": str(source_path),
        "duration_s": duration_s,
        "source_rate": source_rate,
        "chunks": chunk_count,
        "target_sec": target_sec,
        "max_sec": max_sec,
        "draft_prompt_words": draft_prompt_words,
        "elapsed_s": elapsed_s,
        "rtf": elapsed_s / duration_s if duration_s else 0.0,
        "draft_segments": draft_segments,
        "final_segments": final_segments,
    }


def _score_result(result: dict[str, Any], annotations_dir: Path) -> dict[str, Any]:
    reference = parse_reference_words(annotations_dir, result["meeting_id"])
    result["reference"] = {
        "words": len(reference),
        "first_s": reference[0].start_s,
        "last_s": reference[-1].end_s,
        **reference_overlap_stats(reference),
    }
    result["draft_score"] = score_timed_transcript(reference, result["draft_segments"])
    result["final_score"] = score_timed_transcript(reference, result["final_segments"])
    return result


def _summary(
    results: Sequence[dict[str, Any]],
    model_name: str,
    language: str,
    revisions_enabled: bool,
    target_sec: float,
    max_sec: float,
    draft_prompt_words: int,
) -> dict[str, Any]:
    duration_s = sum(float(item["duration_s"]) for item in results)
    elapsed_s = sum(float(item["elapsed_s"]) for item in results)
    audio_hours = duration_s / 3600.0
    rtf = elapsed_s / duration_s if duration_s else 0.0
    draft = aggregate_scores(item["draft_score"] for item in results)
    final = aggregate_scores(item["final_score"] for item in results)
    worst_meeting_wer = max(
        (float(item["final_score"]["wer"]) for item in results),
        default=1.0,
    )
    gate_checks = {
        "meeting_count": len(results) >= MIN_GATE_MEETINGS,
        "audio_hours": audio_hours >= MIN_GATE_AUDIO_HOURS,
        "micro_tcwer": final["wer"] <= MAX_GATE_MICRO_WER,
        "worst_meeting_tcwer": worst_meeting_wer <= MAX_GATE_MEETING_WER,
        "runtime_factor": rtf <= MAX_GATE_RTF,
    }
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": model_name,
        "language": language,
        "revisions_enabled": revisions_enabled,
        "target_sec": target_sec,
        "max_sec": max_sec,
        "draft_prompt_words": draft_prompt_words,
        "corpus": "AMI manual annotations 1.6.2 / headset mix",
        "meetings": len(results),
        "audio_hours": audio_hours,
        "elapsed_hours": elapsed_s / 3600.0,
        "rtf": rtf,
        "reference_overlap": {
            "reference_words": sum(
                int(item["reference"]["reference_words"]) for item in results
            ),
            "overlapping_reference_words": sum(
                int(item["reference"]["overlapping_reference_words"])
                for item in results
            ),
        },
        "draft": draft,
        "final": final,
        "quality_gate": {
            "passed": all(gate_checks.values()),
            "checks": gate_checks,
            "thresholds": {
                "minimum_meetings": MIN_GATE_MEETINGS,
                "minimum_audio_hours": MIN_GATE_AUDIO_HOURS,
                "maximum_micro_tcwer": MAX_GATE_MICRO_WER,
                "maximum_meeting_tcwer": MAX_GATE_MEETING_WER,
                "maximum_runtime_factor": MAX_GATE_RTF,
            },
            "worst_meeting_tcwer": worst_meeting_wer,
        },
        "per_meeting": [
            {
                "meeting_id": item["meeting_id"],
                "duration_s": item["duration_s"],
                "chunks": item["chunks"],
                "rtf": item["rtf"],
                "draft_wer": item["draft_score"]["wer"],
                "final_wer": item["final_score"]["wer"],
                "deletion_rate": item["final_score"]["deletion_rate"],
                "insertion_rate": item["final_score"]["insertion_rate"],
            }
            for item in results
        ],
    }


def _write_summary(path: Path, summary: dict[str, Any]) -> None:
    overlap = summary["reference_overlap"]
    overlap_rate = overlap["overlapping_reference_words"] / max(
        1, overlap["reference_words"]
    )
    path.mkdir(parents=True, exist_ok=True)
    (path / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    lines = [
        "# Meeting Mode dogfood benchmark",
        "",
        f"- Model: `{summary['model']}`",
        f"- Language: `{summary['language']}`",
        f"- Rolling revisions: `{summary['revisions_enabled']}`",
        f"- Chunk target / maximum: `{summary['target_sec']}` / `{summary['max_sec']}` seconds",
        f"- Draft prompt context: `{summary['draft_prompt_words']}` words",
        f"- Meetings: {summary['meetings']}",
        f"- Audio: {summary['audio_hours']:.2f} hours",
        f"- Runtime factor: {summary['rtf']:.3f}",
        f"- Exceptional-quality gate: `{'PASS' if summary['quality_gate']['passed'] else 'FAIL'}`",
        f"- Reference words overlapping another speaker: {overlap_rate:.2%}",
        f"- Draft tcWER: {summary['draft']['wer']:.2%}",
        f"- Final tcWER: {summary['final']['wer']:.2%}",
        f"- Final substitutions: {summary['final']['substitution_rate']:.2%}",
        f"- Final deletions: {summary['final']['deletion_rate']:.2%}",
        f"- Final insertions: {summary['final']['insertion_rate']:.2%}",
        "",
        "| Meeting | Minutes | Chunks | Draft WER | Final WER | RTF |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in summary["per_meeting"]:
        lines.append(
            f"| {item['meeting_id']} | {item['duration_s'] / 60.0:.1f} | "
            f"{item['chunks']} | {item['draft_wer']:.2%} | "
            f"{item['final_wer']:.2%} | {item['rtf']:.3f} |"
        )
    (path / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=PROJECT_ROOT / "benchmarks" / "meeting_mode" / "data" / "ami",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=PROJECT_ROOT / "benchmarks" / "meeting_mode" / "results",
    )
    parser.add_argument("--model", default="auto")
    parser.add_argument(
        "--language",
        default="auto",
        help="ISO-639-1 code to pin, or auto for Whisper detection",
    )
    parser.add_argument(
        "--meetings",
        default="",
        help="Comma-separated curated ids; default is all ten natural meetings",
    )
    parser.add_argument("--max-meetings", type=int, default=None)
    parser.add_argument("--download", action="store_true")
    parser.add_argument(
        "--enable-revisions",
        action="store_true",
        help="Opt into experimental rolling transcript rewrites",
    )
    parser.add_argument(
        "--skip-revisions",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--target-sec", type=float, default=DEFAULT_TARGET_SEC)
    parser.add_argument("--max-sec", type=float, default=DEFAULT_MAX_SEC)
    parser.add_argument(
        "--draft-prompt-words",
        type=int,
        default=DRAFT_PROMPT_WORDS,
        help=(
            "Preceding ASR words supplied to each draft decode; "
            "use 0 for the context-free ablation"
        ),
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the benchmark CLI and return a process exit code."""
    args = _parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    ids = args.meetings.split(",") if args.meetings else []
    meetings = list(select_meetings(ids))
    if args.max_meetings is not None:
        meetings = meetings[: max(0, args.max_meetings)]
    if not meetings:
        print("No meetings selected", file=sys.stderr)
        return 2
    if args.download:
        prepare_corpus(
            args.data_dir,
            meetings,
            progress=lambda message: print(message, flush=True),
        )

    annotations_dir = annotation_root(args.data_dir)
    missing = [
        spec.meeting_id
        for spec in meetings
        if not audio_path(args.data_dir, spec.meeting_id).exists()
    ]
    if missing:
        print(
            "Missing AMI audio for " + ", ".join(missing) + ". Re-run with --download.",
            file=sys.stderr,
        )
        return 2

    language = args.language.strip().lower()
    language_code = None if language == "auto" else language
    if args.target_sec <= 0 or args.max_sec < args.target_sec:
        print("Chunk seconds must satisfy 0 < target <= maximum", file=sys.stderr)
        return 2
    if args.draft_prompt_words < 0:
        print("Draft prompt words must be non-negative", file=sys.stderr)
        return 2
    revisions_enabled = args.enable_revisions and not args.skip_revisions
    revision_profile = "revised" if revisions_enabled else "draft-only"
    chunk_profile = f"t{args.target_sec:g}-m{args.max_sec:g}"
    prompt_profile = (
        f"-p{args.draft_prompt_words}" if args.draft_prompt_words else ""
    )
    run_name = (
        f"{args.model.replace('/', '_')}-{language}-{revision_profile}-"
        f"{chunk_profile}{prompt_profile}"
    )
    run_dir = args.results_dir / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    db_path = run_dir / "benchmark.db"
    if args.force and db_path.exists():
        db_path.unlink()
    db = DatabaseManager(db_path=str(db_path))
    repo = SqlMeetingRepository(db)
    engine = MeetingAsrEngine(
        args.model,
        "benchmark",
        repo,
        language=language_code,
        enable_revisions=revisions_enabled,
    )
    if not engine.is_available:
        print(f"Meeting ASR model is unavailable: {args.model}", file=sys.stderr)
        return 1

    results: list[dict[str, Any]] = []
    try:
        for index, spec in enumerate(meetings, start=1):
            result_path = run_dir / f"{spec.meeting_id}.json"
            if result_path.exists() and not args.force:
                print(f"[{index}/{len(meetings)}] Reusing {result_path}")
                result = json.loads(result_path.read_text(encoding="utf-8"))
                # Audio decoding is expensive, but scoring is cheap and the
                # metric implementation is versioned. Always rescore cached
                # segment output so a normalization or diagnostic fix cannot
                # leave a mixed-version summary behind.
                result = _score_result(result, annotations_dir)
                result["benchmark_version"] = BENCHMARK_VERSION
                result_path.write_text(
                    json.dumps(result, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                results.append(result)
                continue
            print(f"[{index}/{len(meetings)}] Decoding {spec.meeting_id}: {spec.description}")
            work_dir = run_dir / "work" / spec.meeting_id
            if work_dir.exists():
                shutil.rmtree(work_dir)
            work_dir.mkdir(parents=True)
            try:
                result = decode_meeting(
                    engine,
                    repo,
                    spec,
                    audio_path(args.data_dir, spec.meeting_id),
                    work_dir,
                    args.model,
                    run_revisions=revisions_enabled,
                    target_sec=args.target_sec,
                    max_sec=args.max_sec,
                    draft_prompt_words=args.draft_prompt_words,
                )
                result = _score_result(result, annotations_dir)
                result["benchmark_version"] = BENCHMARK_VERSION
                result_path.write_text(
                    json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                results.append(result)
                print(
                    f"  done: draft {result['draft_score']['wer']:.2%}, "
                    f"final {result['final_score']['wer']:.2%}, RTF {result['rtf']:.3f}"
                )
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)
    finally:
        engine.stop()
        db.close()

    summary = _summary(
        results,
        args.model,
        language,
        revisions_enabled=revisions_enabled,
        target_sec=args.target_sec,
        max_sec=args.max_sec,
        draft_prompt_words=args.draft_prompt_words,
    )
    _write_summary(run_dir, summary)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
