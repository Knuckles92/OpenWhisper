"""Whole-meeting CPU Whisper-base pilot of a frozen hard-cut retry policy.

The baseline uses the production Meeting Mode spool chunking, prompt tail,
_transcribe_chunk, and SQLite persistence paths through decode_meeting(). A
cached local CPU Whisper-base int8 model is injected only to avoid a heavily
contended GPU. The treatment retries up to 20 reference-blind hard-cut chunks
per meeting with +/-2 seconds, VAD off, beam 5, and word timestamps; it keeps
the durable live draft and computes a candidate final transcript separately.

Run from repository root:
    venv/Scripts/python.exe -m benchmarks.meeting_mode.targeted_retry_full_cpu_eval
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from faster_whisper import WhisperModel

import main as _app_bootstrap  # noqa: F401  native runtime registration

from benchmarks.meeting_mode.ami import annotation_root, audio_path, parse_reference_words, select_meetings
from benchmarks.meeting_mode.metrics import aggregate_scores, score_timed_transcript
from benchmarks.meeting_mode.run import _load_pcm_wav, _score_result, decode_meeting
from benchmarks.meeting_mode.targeted_retry_cpu_slice_eval import _chunk_infos, _retry, _select
from meeting.asr.engine import MeetingAsrEngine
from meeting.persist.repository import SqlMeetingRepository
from services.database import DatabaseManager

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "benchmarks" / "meeting_mode" / "data" / "ami"
OUTPUT = ROOT / "benchmarks" / "meeting_mode" / "results" / "targeted_retry_full_cpu"


def _baseline(engine: MeetingAsrEngine, repo: SqlMeetingRepository,
              meeting_id: str, output: Path, force: bool) -> dict:
    path = output / f"{meeting_id}_baseline.json"
    if path.exists() and not force:
        print(f"Reusing {path}", flush=True)
        return json.loads(path.read_text(encoding="utf-8"))
    spec = select_meetings([meeting_id])[0]
    with tempfile.TemporaryDirectory(prefix=f"{meeting_id}_live_", dir=output) as temp:
        work_dir = Path(temp)
        if not work_dir.resolve().is_relative_to(output.resolve()):
            raise RuntimeError("Temporary benchmark directory escaped results root")
        result = decode_meeting(
            engine, repo, spec, audio_path(DATA, meeting_id),
            work_dir, "base-cpu-int8", run_offline=False,
        )
    result = _score_result(result, annotation_root(DATA))
    result["model_profile"] = "cached Whisper base CPU int8 direct faster-whisper"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{meeting_id} live baseline: {result['draft_score']['wer']:.2%} tcWER, "
          f"{result['elapsed_s']:.1f}s", flush=True)
    return result


def _treatment(model: WhisperModel, baseline: dict, meeting_id: str,
               max_hardcuts: int) -> dict:
    pcm, rate = _load_pcm_wav(audio_path(DATA, meeting_id))
    segments = list(baseline["draft_segments"])
    infos = _chunk_infos(pcm, rate, segments)
    reassembled = [segment for info in infos for segment in info["segments"]]
    if [segment["id"] for segment in reassembled] != [segment["id"] for segment in segments]:
        raise RuntimeError(f"Baseline segments not assigned exactly once in order: {meeting_id}")
    selected_all, threshold = _select(infos, len(infos))
    hardcuts = [row for row in selected_all if row["suspect_reason"] == "hard_cut"]
    selected = hardcuts[:max_hardcuts]
    replacements = {}
    retries = []
    for info in selected:
        decoded, elapsed = _retry(model, pcm, rate, info, segments)
        replacements[info["seq"]] = decoded
        retries.append({
            "seq": info["seq"], "start_s": info["start_s"],
            "end_s": info["end_s"], "baseline_words": info["words"],
            "retry_words": len(decoded), "retry_elapsed_s": elapsed,
        })
        print(f"  {meeting_id} hard cut {info['seq']}: {info['words']} -> "
              f"{len(decoded)} words, {elapsed:.1f}s", flush=True)
    candidate = []
    for info in infos:
        candidate.extend(replacements.get(info["seq"], info["segments"]))
    reference = parse_reference_words(annotation_root(DATA), meeting_id)
    score = score_timed_transcript(reference, candidate)
    return {
        "meeting_id": meeting_id,
        "duration_s": baseline["duration_s"],
        "chunks": len(infos),
        "suspect_hardcuts": len(hardcuts),
        "retried_hardcuts": len(selected),
        "selector_rms_threshold": threshold,
        "baseline_elapsed_s": baseline["elapsed_s"],
        "retry_elapsed_s": sum(item["retry_elapsed_s"] for item in retries),
        "retry_audio_s": sum(float(item["end_s"]) - float(item["start_s"]) + 4 for item in retries),
        "baseline_score": baseline["draft_score"],
        "candidate_score": score,
        "delta_wer_pp": 100 * (score["wer"] - baseline["draft_score"]["wer"]),
        "retry_details": retries,
        "candidate_segments": candidate,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meetings", default="IN1002,IN1012")
    parser.add_argument("--max-hardcuts", type=int, default=20)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    ids = [part.strip().upper() for part in args.meetings.split(",") if part.strip()]
    select_meetings(ids)
    args.output.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(db_path=str(args.output / "benchmark.db"))
    repo = SqlMeetingRepository(db)
    model = WhisperModel("base", device="cpu", compute_type="int8", local_files_only=True)
    engine = MeetingAsrEngine("base", "benchmark", repo, language=None, defer_load=True)
    engine._backend = SimpleNamespace(model=model)
    engine.is_available = True
    rows = []
    try:
        for meeting_id in ids:
            path = args.output / f"{meeting_id}_retry.json"
            if path.exists() and not args.force:
                print(f"Reusing {path}", flush=True)
                row = json.loads(path.read_text(encoding="utf-8"))
            else:
                baseline = _baseline(engine, repo, meeting_id, args.output, args.force)
                row = _treatment(model, baseline, meeting_id, args.max_hardcuts)
                path.write_text(json.dumps(row, indent=2, ensure_ascii=False), encoding="utf-8")
            rows.append(row)
            print(f"{meeting_id}: {row['baseline_score']['wer']:.2%} -> "
                  f"{row['candidate_score']['wer']:.2%} tcWER "
                  f"({row['delta_wer_pp']:+.2f} pp); "
                  f"{row['retried_hardcuts']} hard cuts, "
                  f"extra {row['retry_elapsed_s']:.1f}s", flush=True)
    finally:
        engine.stop()
        db.close()
    total_duration = sum(row["duration_s"] for row in rows)
    baseline_score = aggregate_scores(row["baseline_score"] for row in rows)
    candidate_score = aggregate_scores(row["candidate_score"] for row in rows)
    baseline_elapsed = sum(row["baseline_elapsed_s"] for row in rows)
    retry_elapsed = sum(row["retry_elapsed_s"] for row in rows)
    summary = {
        "experiment": "full_meeting_cpu_base_hard_cut_retry",
        "corpus": "AMI manual 1.6.2, headset mix",
        "model": "Whisper base CPU int8; product auto/turbo not evaluated",
        "baseline_path": "production Meeting Mode decode_meeting() with 5/20s spool and 50-word prompt tail",
        "treatment": "frozen hard-cut-only policy, up to 20 lowest word-density hard cuts per meeting; +/-2s, VAD off, beam5, word timestamps clipped to chunk; durable live draft retained",
        "meetings": ids,
        "audio_hours": total_duration / 3600,
        "baseline": baseline_score,
        "candidate": candidate_score,
        "baseline_elapsed_s": baseline_elapsed,
        "retry_elapsed_s": retry_elapsed,
        "baseline_rtf": baseline_elapsed / total_duration,
        "combined_rtf": (baseline_elapsed + retry_elapsed) / total_duration,
        "meeting_regressions": sum(row["delta_wer_pp"] > 0 for row in rows),
        "per_meeting": [{
            "meeting_id": row["meeting_id"],
            "duration_s": row["duration_s"],
            "chunks": row["chunks"],
            "suspect_hardcuts": row["suspect_hardcuts"],
            "retried_hardcuts": row["retried_hardcuts"],
            "baseline_wer": row["baseline_score"]["wer"],
            "candidate_wer": row["candidate_score"]["wer"],
            "delta_wer_pp": row["delta_wer_pp"],
            "baseline_elapsed_s": row["baseline_elapsed_s"],
            "retry_elapsed_s": row["retry_elapsed_s"],
        } for row in rows],
    }
    (args.output / "summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(f"TOTAL: {baseline_score['wer']:.2%} -> {candidate_score['wer']:.2%} tcWER; "
          f"{summary['meeting_regressions']}/{len(rows)} meeting regressions; "
          f"RTF {summary['baseline_rtf']:.3f} -> {summary['combined_rtf']:.3f}", flush=True)
    print(args.output / "summary.json")


if __name__ == "__main__":
    main()
