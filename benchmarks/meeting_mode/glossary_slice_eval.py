"""Short paired glossary pilot on AMI slices containing predeclared terms.

The glossary terms come solely from meeting descriptions. Manual annotations
are used *only* to choose evaluation slices containing an exact target phrase
and to score them. This is conditional entity evaluation, not an estimate of
whole-meeting accuracy. Both arms decode the identical slice and use production
5/20-second chunking plus a rolling 50-word prompt.

Run:
    venv/Scripts/python.exe -m benchmarks.meeting_mode.glossary_slice_eval
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from pathlib import Path
from typing import Any

import main as _app_bootstrap  # noqa: F401  CUDA DLL registration

from benchmarks.meeting_mode.ami import annotation_root, audio_path, parse_reference_words
from benchmarks.meeting_mode.metrics import normalize_tokens, score_timed_transcript
from benchmarks.meeting_mode.run import _chunk_ranges, _load_pcm_wav, _segment_dict, _write_chunk
from meeting.asr.engine import MeetingAsrEngine
from meeting.capture.spool import resample_to_16k
from meeting.interfaces import SpooledChunk

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "benchmarks" / "meeting_mode" / "data" / "ami"
OUTPUT = ROOT / "benchmarks" / "meeting_mode" / "results" / "glossary_slice_eval.json"
GLOSSARIES = {
    "IN1005": ("PLSA", "probabilistic latent semantic analysis", "web page indexing"),
    "IN1007": ("keyword spotting", "spectral transforms", "Fourier transform", "spectrogram"),
    "IN1012": ("Interspeech 2005", "speech recognition", "phonetics"),
}


def _reference_hits(meeting_id: str) -> list[dict]:
    words = parse_reference_words(annotation_root(DATA), meeting_id)
    hits = []
    for term in GLOSSARIES[meeting_id]:
        phrase = normalize_tokens(term)
        for speaker in sorted({word.speaker for word in words}):
            speaker_words = [word for word in words if word.speaker == speaker]
            tokens = [normalize_tokens(word.text) for word in speaker_words]
            # The AMI word files are usually one lexical token per <w>. This
            # exact matching deliberately avoids transcript-derived synonyms.
            flat = [(token, word.start_s) for word, group in zip(speaker_words, tokens)
                    for token in group]
            for i in range(len(flat) - len(phrase) + 1):
                if [token for token, _ in flat[i:i + len(phrase)]] == phrase:
                    hits.append({"term": term, "start_s": flat[i][1], "speaker": speaker})
    hits.sort(key=lambda row: (row["start_s"], row["term"]))
    return hits


def _phrase_count(text: str, term: str) -> int:
    words, phrase = normalize_tokens(text), normalize_tokens(term)
    return sum(words[i:i + len(phrase)] == phrase
               for i in range(max(0, len(words) - len(phrase) + 1)))


def _decode(engine: MeetingAsrEngine, samples: Any, rate: int, start_s: float,
            meeting_id: str, condition: str) -> dict:
    engine.meeting_id = f"slice_{meeting_id}_{condition}"
    engine._draft_context.clear()
    engine._language_votes.clear()
    segments = []
    primer = ", ".join(GLOSSARIES[meeting_id]) + "." if condition == "glossary" else ""
    with tempfile.TemporaryDirectory(prefix=f"{meeting_id}_{condition}_",
                                     dir=ROOT / "benchmarks" / "meeting_mode" / "results") as temp:
        tmp = Path(temp)
        if not tmp.resolve().is_relative_to((ROOT / "benchmarks" / "meeting_mode" / "results").resolve()):
            raise RuntimeError("Temporary directory escaped result root")
        started = time.perf_counter()
        for seq, (a, b) in enumerate(_chunk_ranges(samples, rate)):
            path = tmp / f"chunk_{seq:04d}.wav"
            chunk_samples = resample_to_16k(samples[a:b], rate)
            _write_chunk(path, chunk_samples)
            chunk = SpooledChunk(
                chunk_id=seq + 1, meeting_id=engine.meeting_id,
                channel="loopback", seq=seq, file_path=str(path),
                start_s=start_s + a / rate,
                duration_s=len(chunk_samples) / 16000,
                sample_rate=16000,
            )
            context = engine._draft_prompt(chunk)
            prompt = " ".join(part for part in (primer, context) if part) or None
            decoded = engine._transcribe_chunk(chunk, beam_size=5, initial_prompt=prompt)
            segments.extend(_segment_dict(segment) for segment in decoded)
            engine._remember_draft_segments(chunk, decoded)
        elapsed = time.perf_counter() - started
    return {"segments": segments, "elapsed_s": elapsed}


def run_one(engine: MeetingAsrEngine, meeting_id: str, duration_s: float) -> dict:
    hits = _reference_hits(meeting_id)
    if not hits:
        return {"meeting_id": meeting_id, "status": "no_exact_reference_term"}
    pcm, rate = _load_pcm_wav(audio_path(DATA, meeting_id))
    # Choose the densest slice by predeclared-term reference frequency alone,
    # with no knowledge of either ASR condition. This intentionally tests
    # recognition of relevant terms and is not representative meeting WER.
    meeting_duration_s = len(pcm) / rate
    candidates = []
    for index, hit in enumerate(hits):
        candidate_start = min(max(0.0, hit["start_s"] - 30.0),
                              max(0.0, meeting_duration_s - duration_s))
        count = sum(candidate_start + 5 <= other["start_s"] < candidate_start + duration_s - 5
                    for other in hits)
        candidates.append((count, -candidate_start, -index, hit, candidate_start))
    _, _, _, focus, start_s = max(candidates)
    end_s = min(len(pcm) / rate, start_s + duration_s)
    if end_s - start_s < 30:
        raise RuntimeError("Selected slice is too short")
    samples = pcm[int(start_s * rate):int(end_s * rate)]
    all_reference = parse_reference_words(annotation_root(DATA), meeting_id)
    score_start = start_s + 5.0
    score_end = end_s - 5.0
    reference = [word for word in all_reference
                 if score_start <= word.start_s < score_end]
    arms = {}
    for condition in ("baseline", "glossary"):
        decoded = _decode(engine, samples, rate, start_s, meeting_id, condition)
        in_scope = [segment for segment in decoded["segments"]
                    if score_start <= segment["start_s"] < score_end]
        score = score_timed_transcript(reference, in_scope)
        detected = _phrase_count(" ".join(segment["text"] for segment in in_scope), focus["term"])
        arms[condition] = {
            "score": {key: score[key] for key in (
                "reference_words", "hypothesis_words", "substitutions",
                "deletions", "insertions", "wer")},
            "elapsed_s": decoded["elapsed_s"],
            "focus_term_hypothesis_count": detected,
            "segments": decoded["segments"],
        }
        print(f"{meeting_id} {condition}: {score['wer']:.2%} tcWER, "
              f"{detected} focus-term hits, {decoded['elapsed_s']:.1f}s", flush=True)
    reference_count = sum(_phrase_count(
        " ".join(word.text for word in reference if word.speaker == speaker), focus["term"])
        for speaker in {word.speaker for word in reference})
    return {
        "meeting_id": meeting_id,
        "status": "scored",
        "glossary": GLOSSARIES[meeting_id],
        "selection": "densest clip by exact predeclared glossary phrase frequency in reference; evaluation sampling only",
        "focus": focus,
        "focus_term_reference_count": reference_count,
        "slice_start_s": start_s,
        "slice_end_s": end_s,
        "score_start_s": score_start,
        "score_end_s": score_end,
        "baseline": arms["baseline"],
        "glossary_result": arms["glossary"],
        "delta_wer_pp": 100 * (arms["glossary"]["score"]["wer"] - arms["baseline"]["score"]["wer"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meetings", default="IN1005")
    parser.add_argument("--duration-s", type=float, default=120.0)
    parser.add_argument("--show-slices", action="store_true")
    parser.add_argument("--cpu-base", action="store_true",
                        help="Use locally cached Whisper base on CPU instead of product auto model")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    ids = [part.strip().upper() for part in args.meetings.split(",") if part.strip()]
    for meeting_id in ids:
        if meeting_id not in GLOSSARIES:
            parser.error(f"No prespecified glossary for {meeting_id}")
    if args.show_slices:
        for meeting_id in ids:
            print(meeting_id, _reference_hits(meeting_id)[:10])
        return
    if args.cpu_base:
        from transcriber.local_backend import LocalWhisperBackend

        engine = MeetingAsrEngine("base", "slice", None, language=None, defer_load=True)
        engine._backend = LocalWhisperBackend(
            model_name="base", device="cpu", compute_type="int8"
        )
        engine.is_available = engine._backend.is_available()
    else:
        engine = MeetingAsrEngine("auto", "slice", None, language=None)
    if not engine.is_available:
        raise RuntimeError("Local Whisper model unavailable")
    try:
        results = [run_one(engine, meeting_id, args.duration_s) for meeting_id in ids]
    finally:
        engine.stop()
    output = {
        "experiment": "predeclared_glossary_targeted_slices",
        "terms_source": "meeting descriptions in ami.py, independent of reference",
        "slice_selection": "densest clip by exact glossary phrase frequency in AMI reference; conditional sampling, no whole-meeting inference",
        "model": "Whisper base CPU int8" if args.cpu_base else "Whisper auto",
        "product_model_match": not args.cpu_base,
        "language": "auto",
        "chunking": "production 5/20 seconds",
        "context_words": 50,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(args.output)


if __name__ == "__main__":
    main()
