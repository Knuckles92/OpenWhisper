"""Measure word loss on long and short clips under each Local Whisper decode option set.

Reproduces the overnight report's #1 (75 s+ sections lose most of their
words), #2 (short sections empty or in another language) and #4 (long
dictations split into 30 s windows lose sentences) with real speech and a
real faster-whisper model, and compares the option sets proposed to fix
them. It builds clips from LibriSpeech (``--corpus`` folder of 16 kHz WAV +
.txt pairs, or the small HF test set when omitted), so the reference text is
known exactly.

    python scripts/bench_section_decode.py --model base --runs 3 --load

``--load`` keeps every other CPU core busy while decoding, which the report
says makes the loss worse. Results go to stdout and ``--output`` (JSON).
"""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import re
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RATE = 16000


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def wer(reference: str, hypothesis: str) -> float:
    ref, hyp = _words(reference), _words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, 1):
        current = [i] + [0] * len(hyp)
        for j, other in enumerate(hyp, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1,
                             previous[j - 1] + (word != other))
        previous = current
    return previous[-1] / len(ref)


def recall(reference: str, hypothesis: str) -> float:
    """Share of the reference words that appear, in order (an LCS ratio)."""
    ref, hyp = _words(reference), _words(hypothesis)
    if not ref:
        return 1.0
    previous = [0] * (len(hyp) + 1)
    for word in ref:
        current = [0] * (len(hyp) + 1)
        for j, other in enumerate(hyp, 1):
            current[j] = previous[j - 1] + 1 if word == other else max(previous[j], current[j - 1])
        previous = current
    return previous[-1] / len(ref)


def load_corpus(folder: str | None) -> list[tuple[np.ndarray, str]]:
    if folder:
        items = []
        for wav_path in sorted(Path(folder).glob("*.wav")):
            text = wav_path.with_suffix(".txt").read_text(encoding="utf-8").strip()
            with wave.open(str(wav_path)) as source:
                assert source.getframerate() == RATE and source.getsampwidth() == 2
                audio = np.frombuffer(source.readframes(source.getnframes()), "<i2")
            items.append((audio.astype(np.float32) / 32768.0, text))
        return items
    import io

    import pyarrow.parquet as pq
    import soundfile
    from huggingface_hub import hf_hub_download

    path = hf_hub_download("hf-internal-testing/librispeech_asr_dummy",
                           "clean/validation-00000-of-00001.parquet", repo_type="dataset")
    table = pq.read_table(path).to_pylist()
    items = []
    for row in table:
        audio, rate = soundfile.read(io.BytesIO(row["audio"]["bytes"]), dtype="float32")
        assert rate == RATE
        items.append((audio, row["text"]))
    return items


def noise_floor(length: int, rng) -> np.ndarray:
    # About -60 dBFS: a quiet room through a laptop microphone.
    return (rng.standard_normal(length) * 0.001).astype(np.float32)


def build(items, rng, nprng, *, target_s: float, gap_s: float) -> tuple[np.ndarray, str, list[tuple[float, str]]]:
    """Utterances joined with pauses until ``target_s``; returns audio, text, utterance ends."""
    pieces, texts, ends, total = [], [], [], 0.0
    order = list(range(len(items)))
    rng.shuffle(order)
    for index in order:
        audio, text = items[index]
        pieces.append(audio)
        gap = int(gap_s * RATE)
        pieces.append(np.zeros(gap, np.float32))
        total += (len(audio) + gap) / RATE
        texts.append(text)
        ends.append((total - gap_s / 2, text))
        if total >= target_s:
            break
    audio = np.concatenate(pieces)
    return audio + noise_floor(len(audio), nprng), " ".join(texts), ends


def write_wav(path: Path, audio: np.ndarray) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(pcm.tobytes())


# ---- option sets ----

BEAM, MIN_SILENCE_MS = 5, 500


def _join(segments) -> str:
    return re.sub(r"\s+", " ", " ".join(s.text for s in segments)).strip()


def main_file(model, path, language=None):
    """What main's LocalWhisperBackend.transcribe passes today."""
    segments, info = model.transcribe(path, beam_size=BEAM, vad_filter=True,
                                      vad_parameters=dict(min_silence_duration_ms=MIN_SILENCE_MS),
                                      language=language)
    return _join(segments), info.language


def no_condition(model, path, language=None):
    """PR #61: the same, without conditioning on the previous window's text."""
    segments, info = model.transcribe(path, beam_size=BEAM, vad_filter=True,
                                      vad_parameters=dict(min_silence_duration_ms=MIN_SILENCE_MS),
                                      condition_on_previous_text=False, language=language)
    return _join(segments), info.language


def short_utterance(model, path, language=None):
    """PR #63 for clips under 30 s: one utterance, no no-speech skip."""
    with wave.open(str(path)) as source:
        duration = source.getnframes() / source.getframerate()
    options = dict(beam_size=BEAM, vad_filter=duration >= 2.0,
                   vad_parameters=dict(min_silence_duration_ms=MIN_SILENCE_MS) if duration >= 2.0 else None,
                   language=language)
    if duration < 30:
        options.update(without_timestamps=True, no_speech_threshold=None)
    segments, info = model.transcribe(path, **options)
    return _join(segments), info.language


def windows_30s(model, path, language=None):
    """How a paired host decodes (#4): independent windows cut at quiet points."""
    from services.local_asr.audio import split_point

    with wave.open(str(path)) as source:
        audio = np.frombuffer(source.readframes(source.getnframes()), "<i2").astype(np.float32) / 32768
    texts, start, detected = [], 0, ""
    while start < len(audio):
        end = start + split_point(audio[start:])
        segments, info = model.transcribe(audio[start:end], beam_size=BEAM, vad_filter=True,
                                          language=language)
        texts.append(_join(segments))
        detected = detected or info.language
        start = end
    return " ".join(t for t in texts if t), detected


OPTION_SETS = {
    "main": main_file,
    "no_condition": no_condition,
    "short_utterance": short_utterance,
    "windows_30s": windows_30s,
}


def _burn(stop):
    while not stop.is_set():
        sum(i * i for i in range(10000))


def run(args) -> dict:
    from faster_whisper import WhisperModel

    rng = random.Random(args.seed)
    nprng = np.random.default_rng(args.seed)
    items = load_corpus(args.corpus)
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    workdir = Path(tempfile.mkdtemp(prefix="ow-bench-"))
    cases = []
    for run_index in range(args.runs):
        for target in (80.0, 165.0, 240.0):
            audio, text, _ends = build(items, rng, nprng, target_s=target, gap_s=0.5)
            path = workdir / f"long-{run_index}-{int(target)}.wav"
            write_wav(path, audio)
            cases.append(("long", path, text))
        # Walkthrough-like: each utterance is one section, cut mid-pause,
        # with a little lead-in like a screenshot taken between sentences.
        audio, _text, ends = build(items, rng, nprng, target_s=45.0, gap_s=1.2)
        previous = 0.0
        for number, (end, utterance) in enumerate(ends, 1):
            clip = audio[int(previous * RATE):int(end * RATE)]
            previous = end
            path = workdir / f"section-{run_index}-{number}.wav"
            write_wav(path, clip)
            cases.append(("section", path, utterance))
    stop = multiprocessing.Event()
    burners = []
    if args.load:
        for _ in range(max(1, (os.cpu_count() or 2) - 1)):
            process = multiprocessing.Process(target=_burn, args=(stop,), daemon=True)
            process.start()
            burners.append(process)
    results = []
    try:
        for kind, path, reference in cases:
            for name in args.options:
                started = time.perf_counter()
                text, language = OPTION_SETS[name](model, str(path), args.language)
                results.append(dict(kind=kind, clip=path.name, option=name,
                                    seconds=round(time.perf_counter() - started, 2),
                                    language=language, recall=round(recall(reference, text), 3),
                                    wer=round(wer(reference, text), 3),
                                    empty=not text.strip(), text=text[:200]))
                print(json.dumps(results[-1]), flush=True)
    finally:
        stop.set()
        for process in burners:
            process.join(2)
    summary = {}
    for kind in ("long", "section"):
        for name in args.options:
            rows = [r for r in results if r["kind"] == kind and r["option"] == name]
            if not rows:
                continue
            summary[f"{kind}/{name}"] = dict(
                clips=len(rows),
                mean_recall=round(sum(r["recall"] for r in rows) / len(rows), 3),
                lost_over_30pct=sum(r["recall"] < 0.7 for r in rows),
                empty=sum(r["empty"] for r in rows),
                not_english=sum(r["language"] != "en" for r in rows),
                mean_seconds=round(sum(r["seconds"] for r in rows) / len(rows), 2),
            )
    return dict(model=args.model, load=args.load, summary=summary, results=results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="base")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--corpus")
    parser.add_argument("--runs", type=int, default=2)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--language", default=None, help="Pin a language; default detects, like 'auto'")
    parser.add_argument("--load", action="store_true", help="Keep the other CPU cores busy")
    parser.add_argument("--options", nargs="+", default=list(OPTION_SETS), choices=list(OPTION_SETS))
    parser.add_argument("--output")
    args = parser.parse_args()
    report = run(args)
    print(json.dumps(report["summary"], indent=2))
    if args.output:
        Path(args.output).write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
