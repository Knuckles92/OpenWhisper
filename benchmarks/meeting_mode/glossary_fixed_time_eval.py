"""Reference-blind fixed-time AMI glossary evaluation on new meetings.

IN1013 and IN1014 were not decoded in the prior glossary slice pilot. Terms
come from their public one-line meeting descriptions, before reference reads.
Two 90-second windows per meeting are fixed at 25% and 75% of recording time.
Human words are used solely for scoring, never choosing windows or prompts.

Run ``python -m benchmarks.meeting_mode.glossary_fixed_time_eval``.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import main as _app_bootstrap  # noqa: F401  CUDA DLL registration

from benchmarks.meeting_mode.ami import annotation_root, audio_path, parse_reference_words
from benchmarks.meeting_mode.glossary_slice_eval import DATA, ROOT, _decode, _phrase_count
from benchmarks.meeting_mode.metrics import score_timed_transcript
from benchmarks.meeting_mode.run import _load_pcm_wav
from meeting.asr.engine import MeetingAsrEngine

OUTPUT = ROOT / "benchmarks/meeting_mode/results/glossary_fixed_time_eval.json"
TERMS = {
    "IN1005": ("PLSA", "probabilistic latent semantic analysis", "web page indexing"),
    "IN1013": ("spectral information", "spectral", "Fourier transform", "FDLP"),
    "IN1014": ("microphone array", "microphone", "headset", "recording equipment"),
}


def run_meeting(engine, meeting_id, duration_s):
    pcm, rate = _load_pcm_wav(audio_path(DATA, meeting_id))
    meeting_duration = len(pcm) / rate
    words = parse_reference_words(annotation_root(DATA), meeting_id)
    clips = []
    # Fixed fractions of available start time. No reference lookup here.
    starts = [.25 * (meeting_duration - duration_s),
              .75 * (meeting_duration - duration_s)]
    for index, start in enumerate(starts):
        end = start + duration_s
        samples = pcm[int(start * rate):int(end * rate)]
        score_start, score_end = start + 5, end - 5
        reference = [w for w in words if score_start <= w.start_s < score_end]
        reference_text = " ".join(w.text for w in reference)
        arms = {}
        # Alternate order across clips to reduce fixed warm-cache bias.
        order = ("baseline", "glossary") if index == 0 else ("glossary", "baseline")
        for condition in order:
            engine.meeting_id = f"fixed_{meeting_id}_{index}_{condition}"
            engine._draft_context.clear()
            engine._language_votes.clear()
            # _decode reads this module's fixed glossary map from the pilot;
            # patch only within this single call to reuse identical chunking.
            from benchmarks.meeting_mode import glossary_slice_eval as pilot
            previous = pilot.GLOSSARIES.get(meeting_id)
            pilot.GLOSSARIES[meeting_id] = TERMS[meeting_id]
            try:
                decoded = _decode(engine, samples, rate, start, meeting_id, condition)
            finally:
                if previous is None:
                    del pilot.GLOSSARIES[meeting_id]
                else:
                    pilot.GLOSSARIES[meeting_id] = previous
            segments = [s for s in decoded["segments"] if score_start <= s["start_s"] < score_end]
            scored = score_timed_transcript(reference, segments)
            hypothesis_text = " ".join(s["text"] for s in segments)
            arms[condition] = {"score": {k: scored[k] for k in
                          ("reference_words", "hypothesis_words", "substitutions", "deletions", "insertions", "wer")},
                      "elapsed_s": decoded["elapsed_s"],
                      "term_counts": {term: {"reference": _phrase_count(reference_text, term),
                                            "hypothesis": _phrase_count(hypothesis_text, term)}
                                      for term in TERMS[meeting_id]},
                      "segments": decoded["segments"]}
        clips.append({"start_s": start, "end_s": end,
                      "score_start_s": score_start, "score_end_s": score_end,
                      "baseline": arms["baseline"], "glossary": arms["glossary"]})
        print(f"{meeting_id} clip {index + 1}: {arms['baseline']['score']['wer']:.2%} -> "
              f"{arms['glossary']['score']['wer']:.2%}", flush=True)
    return {"meeting": meeting_id, "terms": TERMS[meeting_id], "clips": clips}


def summarize(meetings):
    out = {}
    for arm in ("baseline", "glossary"):
        scores = [clip[arm]["score"] for meeting in meetings for clip in meeting["clips"]]
        errors = sum(s["substitutions"] + s["deletions"] + s["insertions"] for s in scores)
        reference = sum(s["reference_words"] for s in scores)
        term_ref = sum(v["reference"] for meeting in meetings for clip in meeting["clips"]
                       for v in clip[arm]["term_counts"].values())
        term_hyp = sum(min(v["reference"], v["hypothesis"])
                       for meeting in meetings for clip in meeting["clips"]
                       for v in clip[arm]["term_counts"].values())
        out[arm] = {"errors": errors, "reference_words": reference,
                    "micro_wer": errors / reference, "term_instances_reference": term_ref,
                    "term_instances_hypothesis_capped": term_hyp,
                    "term_count_recall_proxy": term_hyp / term_ref if term_ref else None,
                    "decode_seconds": sum(clip[arm]["elapsed_s"] for meeting in meetings
                                          for clip in meeting["clips"])}
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meetings", nargs="+", default=["IN1013", "IN1014"])
    parser.add_argument("--duration-s", type=float, default=90)
    parser.add_argument("--language", choices=("auto", "en"), default="auto")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    if any(mid not in TERMS for mid in args.meetings):
        parser.error("Meeting has no predeclared glossary")
    from transcriber.local_backend import LocalWhisperBackend
    engine = MeetingAsrEngine("base", "fixed", None,
                              language=None if args.language == "auto" else args.language,
                              defer_load=True)
    engine._backend = LocalWhisperBackend(model_name="base", device="cpu", compute_type="int8")
    engine.is_available = engine._backend.is_available()
    if not engine.is_available:
        raise RuntimeError("Cached Whisper base CPU model unavailable")
    started = time.perf_counter()
    try:
        meetings = [run_meeting(engine, mid, args.duration_s) for mid in args.meetings]
    finally:
        engine.stop()
    report = {"protocol": "90s clips at 25% and 75% of each natural AMI recording; human words used only for scoring; production 5/20s chunking and 50-word context; base CPU int8 Whisper.",
              "language": args.language,
              "terms_source": "Public AMI meeting descriptions and related meeting topic, not target reference words.",
              "meetings": meetings, "summary": summarize(meetings),
              "total_wall_seconds": time.perf_counter() - started,
              "limitations": ["Whisper base CPU is not product auto/turbo model.",
                              "Exact phrase count proxy is not time-aligned entity recall.",
                              "Only four fixed clips across two meetings; no whole-meeting claim."]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
