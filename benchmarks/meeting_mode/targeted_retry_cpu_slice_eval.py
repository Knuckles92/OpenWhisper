"""Paired CPU Whisper-base pilot of padded rescue on suspect AMI chunks.

Suspect intervals come from reference-blind production draft/audio checks
defined below. Both arms use the same cached, local CPU base model:
ordinary production-style VAD-on chunk decode versus +/-2s padded VAD-off
decode clipped to the original interval. This is a mechanism check, not the
product turbo model's expected gain.

Run:
    venv/Scripts/python.exe -m benchmarks.meeting_mode.targeted_retry_cpu_slice_eval
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import main as _app_bootstrap  # noqa: F401  native runtime registration

from benchmarks.meeting_mode.ami import annotation_root, audio_path, parse_reference_words
from benchmarks.meeting_mode.metrics import normalize_tokens, score_text
from benchmarks.meeting_mode.run import _chunk_ranges, _load_pcm_wav, _segment_dict, _write_chunk
from meeting.asr.audio import prepare_for_whisper
from meeting.asr.hallucination import is_hallucination
from meeting.asr.engine import MeetingAsrEngine
from meeting.capture.spool import resample_to_16k
from meeting.interfaces import SpooledChunk
from faster_whisper import WhisperModel

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "benchmarks" / "meeting_mode" / "data" / "ami"
BASELINE = ROOT / "benchmarks" / "meeting_mode" / "results" / "auto-auto-draft-only-t5-m20-p50"
OUTPUT = ROOT / "benchmarks" / "meeting_mode" / "results" / "targeted_retry_cpu_slice_eval.json"


def _word_count(segments: list[dict]) -> int:
    return sum(len(normalize_tokens(str(segment["text"]))) for segment in segments)


def _chunk_infos(pcm: np.ndarray, rate: int, segments: list[dict]) -> list[dict]:
    infos = []
    for seq, (start, end) in enumerate(_chunk_ranges(pcm, rate)):
        start_s, end_s = start / rate, end / rate
        chunk_segments = [segment for segment in segments
                          if start_s <= float(segment["start_s"]) < end_s]
        samples = pcm[start:end].astype(np.float32)
        rms = float(np.sqrt(np.mean(samples * samples))) if samples.size else 0.0
        tail = samples[-min(samples.size, int(.4 * rate)):]
        tail_rms = float(np.sqrt(np.mean(tail * tail))) if tail.size else 0.0
        infos.append({
            "seq": seq, "start": start, "end": end,
            "start_s": start_s, "end_s": end_s,
            "duration_s": end_s - start_s,
            "segments": chunk_segments,
            "words": _word_count(chunk_segments),
            "rms": rms, "tail_rms": tail_rms,
        })
    return infos


def _select(infos: list[dict], max_retries: int) -> tuple[list[dict], float]:
    speech_rms = [row["rms"] for row in infos if row["words"] >= 3]
    threshold = max(100.0, .25 * float(np.median(speech_rms))) if speech_rms else 100.0
    for row in infos:
        sparse = row["duration_s"] >= 3 and row["words"] / row["duration_s"] < .15
        hard_cut = row["duration_s"] >= 19.5 and row["tail_rms"] >= threshold
        row["suspect_reason"] = (
            "sparse_speech" if sparse and row["rms"] >= threshold
            else "hard_cut" if hard_cut else ""
        )
    selected = sorted(
        (row for row in infos if row["suspect_reason"]),
        key=lambda row: (0 if row["suspect_reason"] == "sparse_speech" else 1,
                         row["words"] / row["duration_s"], row["seq"]),
    )[:max_retries]
    return selected, threshold


def _retry(model: WhisperModel, pcm: np.ndarray, rate: int, info: dict,
           preceding_segments: list[dict]) -> tuple[list[dict], float]:
    start_s, end_s = float(info["start_s"]), float(info["end_s"])
    padded_start = max(0, int(round((start_s - 2.0) * rate)))
    padded_end = min(pcm.size, int(round((end_s + 2.0) * rate)))
    audio = prepare_for_whisper(pcm[padded_start:padded_end], rate)
    prompt_words = [word for segment in preceding_segments
                    if float(segment["end_s"]) <= start_s + 1e-6
                    for word in str(segment["text"]).split()]
    prompt = " ".join(prompt_words[-50:]) or None
    started = time.perf_counter()
    decoded, _ = model.transcribe(
        audio, beam_size=5, vad_filter=False, word_timestamps=True,
        language=None, condition_on_previous_text=False, initial_prompt=prompt,
    )
    result = []
    ordinal = 0
    padded_start_s = padded_start / rate
    for segment in decoded:
        if is_hallucination(segment):
            continue
        for word in segment.words or []:
            word_start = padded_start_s + float(word.start)
            word_end = padded_start_s + float(word.end)
            if start_s <= (word_start + word_end) / 2 < end_s:
                result.append({
                    "id": f"retry_{info['seq']}_{ordinal}",
                    "start_s": max(start_s, word_start),
                    "end_s": min(end_s, word_end),
                    "text": word.word.strip(),
                    "channel": "loopback",
                })
                ordinal += 1
    return result, time.perf_counter() - started


def _aggregate(rows: list[dict]) -> dict:
    keys = ("words", "hypothesis_words", "substitutions", "deletions", "insertions", "errors")
    sums = {key: sum(int(row[key]) for row in rows) for key in keys}
    sums["wer"] = sums["errors"] / max(1, sums["words"])
    return sums


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meeting", default="IN1009")
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    source = json.loads((BASELINE / f"{args.meeting}.json").read_text(encoding="utf-8"))
    pcm, rate = _load_pcm_wav(audio_path(DATA, args.meeting))
    infos = _chunk_infos(pcm, rate, source["draft_segments"])
    reassembled = [segment for info in infos for segment in info["segments"]]
    if [segment["id"] for segment in reassembled] != [segment["id"] for segment in source["draft_segments"]]:
        raise RuntimeError("Production baseline segments were not reassembled exactly once")
    selected, threshold = _select(infos, args.max_retries)
    reference = parse_reference_words(annotation_root(DATA), args.meeting)
    engine = MeetingAsrEngine("base", "retry_slice", None, language=None, defer_load=True)
    # The app's isolated worker intentionally serializes only segment text and
    # segment timestamps, dropping the word timestamps needed to clip padding.
    # This benchmark uses the same cached faster-whisper weights directly.
    model = WhisperModel("base", device="cpu", compute_type="int8", local_files_only=True)
    engine._backend = SimpleNamespace(model=model)
    engine.is_available = True
    if not engine.is_available:
        raise RuntimeError("Cached CPU Whisper base model is unavailable")
    rows = []
    try:
        with tempfile.TemporaryDirectory(prefix="retry_cpu_", dir=OUTPUT.parent) as temp:
            work_dir = Path(temp)
            if not work_dir.resolve().is_relative_to(OUTPUT.parent.resolve()):
                raise RuntimeError("Temporary path escaped result root")
            for info in selected:
                path = work_dir / f"chunk_{info['seq']:04d}.wav"
                frames = resample_to_16k(pcm[info["start"]:info["end"]], rate)
                _write_chunk(path, frames)
                chunk = SpooledChunk(
                    chunk_id=info["seq"] + 1,
                    meeting_id=engine.meeting_id,
                    channel="loopback", seq=info["seq"],
                    file_path=str(path),
                    start_s=info["start_s"],
                    duration_s=len(frames) / 16000,
                    sample_rate=16000,
                )
                preceding = [word for segment in source["draft_segments"]
                             if float(segment["end_s"]) <= info["start_s"] + 1e-6
                             for word in str(segment["text"]).split()]
                prompt = " ".join(preceding[-50:]) or None
                started = time.perf_counter()
                ordinary = [_segment_dict(seg) for seg in engine._transcribe_chunk(
                    chunk, beam_size=5, initial_prompt=prompt)]
                ordinary_elapsed = time.perf_counter() - started
                rescued, rescue_elapsed = _retry(
                    engine._backend.model, pcm, rate, info, source["draft_segments"])
                ref_text = " ".join(word.text for word in reference
                                    if info["start_s"] <= word.start_s < info["end_s"])
                ordinary_score = score_text(
                    ref_text, " ".join(seg["text"] for seg in ordinary))
                rescue_score = score_text(
                    ref_text, " ".join(seg["text"] for seg in rescued))
                accept = (
                    rescue_score["hypothesis_words"] >= ordinary_score["hypothesis_words"] + 2
                    and rescue_score["hypothesis_words"] <= max(5, 5 * info["duration_s"])
                )
                rows.append({
                    "seq": info["seq"], "start_s": info["start_s"],
                    "end_s": info["end_s"], "duration_s": info["duration_s"],
                    "reason": info["suspect_reason"],
                    "production_draft_words": info["words"],
                    "ordinary": ordinary_score,
                    "rescue": rescue_score,
                    "accept_rescue": accept,
                    "ordinary_elapsed_s": ordinary_elapsed,
                    "rescue_elapsed_s": rescue_elapsed,
                })
                print(f"chunk {info['seq']}: {ordinary_score['hypothesis_words']} -> "
                      f"{rescue_score['hypothesis_words']} words; "
                      f"{ordinary_score['errors']} -> {rescue_score['errors']} errors; "
                      f"{'accept' if accept else 'keep'}", flush=True)
    finally:
        engine.stop()
    ordinary = _aggregate([row["ordinary"] for row in rows])
    rescue = _aggregate([row["rescue"] for row in rows])
    gated = _aggregate([row["rescue"] if row["accept_rescue"] else row["ordinary"]
                        for row in rows])
    # IN1009 development result showed that sparse voiced chunks can contain
    # no reference speech and that a word-gain gate can accept hallucinations.
    # Freeze this hard-cut-only rule before evaluating IN1007/IN1014.
    hard_cut_only = _aggregate([
        row["rescue"] if row["reason"] == "hard_cut" else row["ordinary"]
        for row in rows
    ])
    result = {
        "experiment": "cpu_base_padded_retry_selected_chunks",
        "meeting_id": args.meeting,
        "corpus": "AMI manual 1.6.2, headset mix",
        "model": "Whisper base CPU int8; differs from product auto/turbo",
        "selection_source": "reference-blind product auto/turbo draft and audio RMS",
        "selection_rms_threshold": threshold,
        "selected_chunks": len(rows),
        "selected_audio_s": sum(row["duration_s"] for row in rows),
        "ordinary": ordinary,
        "rescue_all": rescue,
        "rescue_word_gain_gated": gated,
        "rescue_hard_cut_only": hard_cut_only,
        "ordinary_elapsed_s": sum(row["ordinary_elapsed_s"] for row in rows),
        "rescue_elapsed_s": sum(row["rescue_elapsed_s"] for row in rows),
        "chunk_regressions_rescue_all": sum(row["rescue"]["errors"] > row["ordinary"]["errors"] for row in rows),
        "chunk_regressions_gated": sum(
            (row["rescue"] if row["accept_rescue"] else row["ordinary"])["errors"] > row["ordinary"]["errors"]
            for row in rows),
        "chunk_regressions_hard_cut_only": sum(
            row["reason"] == "hard_cut" and row["rescue"]["errors"] > row["ordinary"]["errors"]
            for row in rows),
        "chunks": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"ordinary {ordinary['wer']:.2%}, all rescue {rescue['wer']:.2%}, "
          f"word-gain gated {gated['wer']:.2%} on {ordinary['words']} reference words", flush=True)
    print(args.output)


if __name__ == "__main__":
    main()
