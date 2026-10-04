"""Bounded, reproducible capture-health and source-separation pilots.

Run from the repository root:

    venv/Scripts/python.exe -m benchmarks.meeting_mode.capture_source_eval health
    venv/Scripts/python.exe -m benchmarks.meeting_mode.capture_source_eval sources

The health experiment injects synthetic faults into *real* annotated AMI
speech. The source experiment uses the four real IN1009 individual headsets,
which are a source-separation upper bound rather than the product's ordinary
mic + system-audio layout. Results and downloaded WAVs remain under ignored
benchmarks/meeting_mode/{results,data} paths.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree

import numpy as np

from benchmarks.meeting_mode.ami import DEFAULT_MEETINGS, parse_reference_words
from benchmarks.meeting_mode.metrics import score_timed_transcript

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "benchmarks" / "meeting_mode" / "data" / "ami"
RESULTS = ROOT / "benchmarks" / "meeting_mode" / "results" / "capture-source-20261003"
MEETING = "IN1009"
SLICE_S = 300.0
ASR_SLICE_S = 60.0
PROBE_S = 3.0
PROBE_MIN_WORDS = 4
PROBE_RMS_THRESHOLD = 300.0  # Current spool quiet threshold, int16 units.


def load_wav(path: Path, limit_s: float | None = None) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wav_file:
        rate = wav_file.getframerate()
        frames = wav_file.getnframes()
        if limit_s is not None:
            frames = min(frames, int(round(limit_s * rate)))
        raw = wav_file.readframes(frames)
        channels = wav_file.getnchannels()
    audio = np.frombuffer(raw, dtype="<i2")
    if channels > 1:
        audio = audio.reshape(-1, channels).astype(np.int32).mean(axis=1).astype(np.int16)
    return audio, rate


def write_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(rate)
        wav_file.writeframes(np.asarray(audio, dtype="<i2").tobytes())


def max_short_rms(audio: np.ndarray, rate: int, hop_s: float = 0.25) -> float:
    frame_n = max(1, int(round(rate * hop_s)))
    count = audio.size // frame_n
    if count == 0:
        return 0.0
    frames = audio[: count * frame_n].reshape(count, frame_n).astype(np.float64)
    return float(np.sqrt(np.mean(frames * frames, axis=1)).max())


def health() -> dict:
    """Score a guided speech probe against healthy and injected-muted windows."""
    positives = 0
    false_alerts = 0
    muted_caught = 0
    muted_missed = 0
    gain_fault_caught = {"1pct_gain": 0, "5pct_gain": 0, "10pct_gain": 0}
    healthy_peak_rms = []
    all_windows = quiet_all = zero_word_windows = quiet_zero_word = 0
    wrong_source_caught = 0
    by_meeting = []
    # Seed with a voiced probe from the final meeting. Subsequent meetings
    # substitute a voiced probe from the preceding meeting, never themselves.
    seed_spec = DEFAULT_MEETINGS[-1]
    seed_audio, seed_rate = load_wav(DATA / "audio" / seed_spec.audio_filename)
    seed_reference = parse_reference_words(DATA / "annotations", seed_spec.meeting_id)
    wrong_probe = next(
        seed_audio[int(index * PROBE_S * seed_rate):int((index + 1) * PROBE_S * seed_rate)].copy()
        for index in range(int((len(seed_audio) / seed_rate) // PROBE_S))
        if sum(index * PROBE_S <= word.start_s < (index + 1) * PROBE_S
               for word in seed_reference) >= PROBE_MIN_WORDS
    )
    wrong_rate = seed_rate
    for spec in DEFAULT_MEETINGS:
        audio, rate = load_wav(DATA / "audio" / spec.audio_filename)
        reference = parse_reference_words(DATA / "annotations", spec.meeting_id)
        meeting_healthy = meeting_false = 0
        next_wrong_probe = None
        duration = len(audio) / rate
        for index in range(int(duration // PROBE_S)):
            start_s = index * PROBE_S
            end_s = start_s + PROBE_S
            words = sum(start_s <= word.start_s < end_s for word in reference)
            snippet = audio[int(start_s * rate):int(end_s * rate)]
            peak_rms = max_short_rms(snippet, rate)
            all_windows += 1
            quiet_all += int(peak_rms <= PROBE_RMS_THRESHOLD)
            if words == 0:
                zero_word_windows += 1
                quiet_zero_word += int(peak_rms <= PROBE_RMS_THRESHOLD)
            if words < PROBE_MIN_WORDS:
                continue
            if next_wrong_probe is None:
                next_wrong_probe = snippet.copy()
            healthy_peak_rms.append(peak_rms)
            detected = peak_rms > PROBE_RMS_THRESHOLD
            positives += 1
            meeting_healthy += 1
            if not detected:
                false_alerts += 1
                meeting_false += 1
            # The paired fault is the identical annotated speech interval with
            # all samples replaced by zero. Device-open checks still pass it.
            muted = np.zeros_like(snippet)
            muted_detected_as_fault = (
                max_short_rms(muted, rate) <= PROBE_RMS_THRESHOLD)
            muted_caught += int(muted_detected_as_fault)
            muted_missed += int(not muted_detected_as_fault)
            for name, gain in (("1pct_gain", 0.01), ("5pct_gain", 0.05),
                               ("10pct_gain", 0.10)):
                attenuated = (snippet.astype(np.float32) * gain).astype(np.int16)
                gain_fault_caught[name] += int(
                    max_short_rms(attenuated, rate) <= PROBE_RMS_THRESHOLD)
            if wrong_rate != rate:
                raise ValueError("AMI replacement probes must share sample rate")
            other_peak = max_short_rms(wrong_probe, rate)
            matched = np.clip(wrong_probe.astype(np.float32)
                              * (peak_rms / max(1.0, other_peak)),
                              -32768, 32767).astype(np.int16)
            wrong_source_caught += int(
                max_short_rms(matched, rate) <= PROBE_RMS_THRESHOLD)
        by_meeting.append({"meeting_id": spec.meeting_id,
                           "annotated_speech_probes": meeting_healthy,
                           "healthy_false_alerts": meeting_false})
        if next_wrong_probe is not None:
            wrong_probe, wrong_rate = next_wrong_probe, rate
    return {
        "experiment": "guided_speech_probe_synthetic_mute",
        "corpus": "AMI manual 1.6.2; 10 original headset-mix WAVs",
        "injected_fault": "replace each annotated 3-s speech window with zero samples",
        "probe_s": PROBE_S,
        "minimum_annotated_words": PROBE_MIN_WORDS,
        "rms_hop_s": 0.25,
        "rms_threshold_int16": PROBE_RMS_THRESHOLD,
        "healthy_windows": positives,
        "muted_windows": positives,
        "all_contiguous_3s_windows": all_windows,
        "naturally_quiet_3s_windows": quiet_all,
        "zero_word_3s_windows": zero_word_windows,
        "naturally_quiet_zero_word_3s_windows": quiet_zero_word,
        "baseline_device_open_fault_detection_rate": 0.0,
        "treatment_muted_detection_rate": muted_caught / max(1, positives),
        "treatment_healthy_false_alert_rate": false_alerts / max(1, positives),
        "treatment_healthy_accept_rate": (positives - false_alerts) / max(1, positives),
        "healthy_peak_rms_quantiles_int16": {
            str(percentile): float(np.percentile(healthy_peak_rms, percentile))
            for percentile in (0, 1, 5, 25, 50, 75, 95, 99, 100)
        },
        "attenuation_fault_detection_rates": {
            name: count / max(1, positives)
            for name, count in gain_fault_caught.items()
        },
        "level_matched_wrong_source_detection_rate": wrong_source_caught / max(1, positives),
        "wrong_source_fault": "replace each probe with voiced audio from a different AMI meeting, scaled to match the original short-window peak RMS",
        "muted_missed": muted_missed,
        "by_meeting": by_meeting,
        "limitation": "Faults are injected. No device switch, human response, or WER improvement is measured.",
    }


def monitor_real() -> dict:
    """Replay real meetings as callback blocks through the production monitor."""
    from meeting.capture.health import CaptureSignalMonitor
    from meeting.interfaces import CaptureBlock

    probe_s = 5.0
    minimum_words = 6
    selected = failed = all_windows = zero_word_windows = 0
    quiet_replayed = quiet_stalled = 0
    detected_runs = {"one_window": 0, "two_consecutive": 0, "three_consecutive": 0}
    quiet_detected_runs = dict.fromkeys(detected_runs, 0)
    by_meeting = []
    for spec in DEFAULT_MEETINGS:
        audio, rate = load_wav(DATA / "audio" / spec.audio_filename)
        reference = parse_reference_words(DATA / "annotations", spec.meeting_id)
        local_selected = local_failed = 0
        n_windows = int((len(audio) / rate) // probe_s)
        for index in range(n_windows):
            start_s = index * probe_s
            end_s = start_s + probe_s
            words = sum(start_s <= word.start_s < end_s for word in reference)
            all_windows += 1
            zero_word_windows += int(words == 0)
            if 0 < words < minimum_words:
                continue
            if words:
                selected += 1
                local_selected += 1
            else:
                quiet_replayed += 1
            monitor = CaptureSignalMonitor()
            monitor.reset_stream(now=0.0)
            snippet = audio[int(start_s * rate):int(end_s * rate)]
            frame_n = int(round(rate * 0.25))
            trimmed = snippet[:(len(snippet) // frame_n) * frame_n]
            samples = trimmed.reshape(-1, frame_n).astype(np.float64)
            active = np.sqrt(np.mean(samples * samples, axis=1)) >= PROBE_RMS_THRESHOLD
            for width, key in ((1, "one_window"), (2, "two_consecutive"),
                               (3, "three_consecutive")):
                if any(np.all(active[i:i + width])
                       for i in range(len(active) - width + 1)):
                    (detected_runs if words else quiet_detected_runs)[key] += 1
            for offset in range(0, len(snippet), 1024):
                part = snippet[offset:offset + 1024]
                monitor.observe(CaptureBlock(
                    channel="mic", frames=part, sample_rate=rate,
                    t_mono=offset / rate,
                ), now=(offset + len(part)) / rate)
            snapshot = monitor.snapshot(now=probe_s)
            if words and snapshot["signal_windows"] == 0:
                failed += 1
                local_failed += 1
            if not words and snapshot["stalled"]:
                quiet_stalled += 1
        by_meeting.append({"meeting_id": spec.meeting_id,
                           "guided_speech_probes": local_selected,
                           "false_no_signal": local_failed})
    return {
        "experiment": "production_capture_monitor_real_AMI_replay",
        "corpus": "ten complete AMI headset-mix meetings, 8.16 audio hours",
        "probe_s": probe_s,
        "minimum_annotated_words": minimum_words,
        "callback_frames": 1024,
        "all_5s_windows": all_windows,
        "zero_word_5s_windows": zero_word_windows,
        "natural_quiet_callbacks_replayed": quiet_replayed,
        "natural_quiet_false_stalls": quiet_stalled,
        "guided_speech_probes": selected,
        "false_no_signal": failed,
        "false_no_signal_rate": failed / max(1, selected),
        "detected_5s_speech_probes_by_run_length": detected_runs,
        "detected_5s_annotation_quiet_probes_by_run_length": quiet_detected_runs,
        "by_meeting": by_meeting,
        "limitation": "Headset mix is a single, high-quality source. It does not represent a quiet local mic, idle system output, device faults, or a user's actual guided action.",
    }


def speaker_channels() -> dict[str, int]:
    root = ElementTree.parse(DATA / "annotations" / "corpusResources" / "meetings.xml").getroot()
    meeting = next(node for node in root.iter()
                   if node.tag.endswith("meeting") and node.get("observation") == MEETING)
    return {node.attrib["nxt_agent"]: int(node.attrib["channel"])
            for node in meeting if node.tag.endswith("speaker")}


def headset_path(channel: int) -> Path:
    return DATA / "audio" / "individual" / f"{MEETING}.Headset-{channel}.wav"


def download_headsets() -> None:
    def one(channel: int) -> None:
        path = headset_path(channel)
        if path.exists() and path.stat().st_size > 44:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        url = ("https://groups.inf.ed.ac.uk/ami/AMICorpusMirror/amicorpus/"
               f"{MEETING}/audio/{path.name}")
        print(f"Downloading {url}", flush=True)
        request = urllib.request.Request(url, headers={"User-Agent": "OpenWhisper/benchmark"})
        part = path.with_suffix(".wav.part")
        with urllib.request.urlopen(request, timeout=120) as response, part.open("wb") as out:
            while block := response.read(1024 * 1024):
                out.write(block)
        part.replace(path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(one, sorted(speaker_channels().values())))


def _slice_reference(reference, limit_s: float = SLICE_S):
    return [word for word in reference if 0 <= word.start_s < limit_s]


def _core_score(score: dict) -> dict:
    return {key: score[key] for key in
            ("reference_words", "hypothesis_words", "substitutions", "deletions",
             "insertions", "wer")}


def speaker_energy() -> dict:
    """Assign annotated words to the loudest real individual headset."""
    download_headsets()
    mapping = speaker_channels()
    channel_audio = {channel: load_wav(headset_path(channel), SLICE_S)
                     for channel in sorted(mapping.values())}
    rates = {rate for _, rate in channel_audio.values()}
    if len(rates) != 1:
        raise ValueError(f"Headsets do not share a sample rate: {rates}")
    rate = rates.pop()
    reference = _slice_reference(parse_reference_words(DATA / "annotations", MEETING))
    # This prediction grid is derived from audio alone. Manual word times are
    # used only to score each word at its midpoint, never to pick a window.
    bin_s = 0.5
    bin_n = int(round(bin_s * rate))
    bins = int((SLICE_S * rate) // bin_n)
    blind_energy = np.stack([
        np.mean(audio[:bins * bin_n].reshape(bins, bin_n).astype(np.float64) ** 2,
                axis=1)
        for channel, (audio, _) in sorted(channel_audio.items())
    ])
    blind_channels = np.array(sorted(channel_audio))[np.argmax(blind_energy, axis=0)]
    speaker_counts = {speaker: 0 for speaker in mapping}
    speaker_correct = {speaker: 0 for speaker in mapping}
    blind_correct = 0
    blind_speaker_correct = {speaker: 0 for speaker in mapping}
    evaluated = correct = 0
    for word in reference:
        if word.speaker not in mapping:
            continue
        # A short centered window gives single-word labels a stable acoustic
        # interval even when a forced-aligned word has zero duration.
        middle = (word.start_s + word.end_s) / 2
        half = max(0.1, min(0.5, (word.end_s - word.start_s) / 2))
        left = max(0, int((middle - half) * rate))
        right = min(int(SLICE_S * rate), int((middle + half) * rate))
        if right <= left:
            continue
        energies = {}
        for channel, (audio, _) in channel_audio.items():
            part = audio[left:right].astype(np.float64)
            energies[channel] = float(np.mean(part * part)) if part.size else 0.0
        predicted = max(energies, key=energies.get)
        target = mapping[word.speaker]
        bin_index = min(bins - 1, max(0, int(middle / bin_s)))
        blind_hit = int(blind_channels[bin_index] == target)
        blind_correct += blind_hit
        blind_speaker_correct[word.speaker] += blind_hit
        speaker_counts[word.speaker] += 1
        evaluated += 1
        if predicted == target:
            correct += 1
            speaker_correct[word.speaker] += 1
    majority = max(speaker_counts.values()) if speaker_counts else 0
    return {
        "experiment": "IN1009_headset_energy_speaker_attribution_300s",
        "metric": "manual word's channel equals highest mean-square-energy headset",
        "word_windows": evaluated,
        "correct": correct,
        "source_aware_accuracy": correct / max(1, evaluated),
        "fixed_bin_audio_only_accuracy": blind_correct / max(1, evaluated),
        "fixed_bin_audio_only_correct": blind_correct,
        "fixed_bin_s": bin_s,
        "majority_speaker_baseline_accuracy": majority / max(1, evaluated),
        "speaker_mapping": mapping,
        "per_speaker": {speaker: {"words": speaker_counts[speaker],
                                  "correct": speaker_correct[speaker],
                                  "accuracy": speaker_correct[speaker] / max(1, speaker_counts[speaker]),
                                  "fixed_bin_correct": blind_speaker_correct[speaker],
                                  "fixed_bin_accuracy": blind_speaker_correct[speaker] / max(1, speaker_counts[speaker])}
                        for speaker in mapping},
        "limitation": "The fixed-bin selector is reference-blind but evaluated with manual word timestamps. This uses four close-talk headsets, not product mic/loopback capture; majority and word-window scores are upper-bound comparators, not production diarization.",
    }


def fixed_bin_selected_waveform(bin_s: float = 0.5, fade_s: float = 0.02,
                                limit_s: float = ASR_SLICE_S) -> tuple[np.ndarray, int, dict]:
    """Select the loudest headset per fixed audio-only bin with short crossfades."""
    loaded = [load_wav(headset_path(channel), limit_s) for channel in range(4)]
    rates = {rate for _, rate in loaded}
    if len(rates) != 1:
        raise ValueError(f"Headsets do not share a sample rate: {rates}")
    rate = rates.pop()
    n = min(len(audio) for audio, _ in loaded)
    channels = np.stack([audio[:n] for audio, _ in loaded])
    bin_n = int(round(rate * bin_s))
    n_bins = (n + bin_n - 1) // bin_n
    selected = np.empty(n, dtype=np.int16)
    chosen = []
    transitions = 0
    fade_n = int(round(rate * fade_s))
    for index in range(n_bins):
        start = index * bin_n
        end = min(n, start + bin_n)
        chunk = channels[:, start:end].astype(np.float64)
        energy = np.mean(chunk * chunk, axis=1)
        channel = int(np.argmax(energy))
        selected[start:end] = channels[channel, start:end]
        if chosen and channel != chosen[-1]:
            transitions += 1
            fade_end = min(end, start + fade_n)
            alpha = np.linspace(0.0, 1.0, fade_end - start, endpoint=True)
            blend = (channels[chosen[-1], start:fade_end] * (1 - alpha)
                     + channels[channel, start:fade_end] * alpha)
            selected[start:fade_end] = np.clip(blend, -32768, 32767).astype(np.int16)
        chosen.append(channel)
    return selected, rate, {"bin_s": bin_s, "fade_s": fade_s,
                            "bins": n_bins, "transitions": transitions,
                            "chosen_bins_by_channel": {
                                str(channel): chosen.count(channel) for channel in range(4)}}


def sources(model_name: str = "auto", parallel_headsets: bool = False,
            slice_s: float = ASR_SLICE_S) -> dict:
    """Run production chunking/Whisper on mix and blind selected headset."""
    download_headsets()
    # Import here: main.py initializes CUDA DLL paths before CTranslate2.
    from benchmarks.meeting_mode.run import decode_meeting
    from benchmarks.meeting_mode.ami import MeetingSpec
    from meeting.asr.engine import MeetingAsrEngine
    from meeting.persist.repository import SqlMeetingRepository
    from services.database import DatabaseManager

    RESULTS.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(db_path=str(RESULTS / f"source_pilot_{model_name}.db"))
    repo = SqlMeetingRepository(db)
    engine = MeetingAsrEngine(model_name, "source_pilot", repo, language="en")
    if not engine.is_available:
        raise RuntimeError(f"Meeting ASR model is unavailable: {model_name}")
    reference = _slice_reference(parse_reference_words(DATA / "annotations", MEETING), slice_s)
    selected, selected_rate, selector = fixed_bin_selected_waveform(limit_s=slice_s)
    selected_path = RESULTS / "slices" / f"fixed-bin-selected-{int(slice_s)}s.wav"
    write_wav(selected_path, selected, selected_rate)
    cases = [("mix", DATA / "audio" / f"{MEETING}.Mix-Headset.wav"),
             ("fixed-bin-selected", selected_path)]
    if parallel_headsets:
        cases += [(f"headset-{channel}", headset_path(channel))
                  for channel in range(4)]
    decoded = {}
    try:
        for label, source in cases:
            cache = RESULTS / f"{model_name}-{label}-{int(slice_s)}s.json"
            if cache.exists():
                result = json.loads(cache.read_text(encoding="utf-8"))
                print(f"Reusing {label}", flush=True)
            else:
                audio, rate = load_wav(source, slice_s)
                slice_wav = RESULTS / "slices" / f"{label}-{int(slice_s)}s.wav"
                write_wav(slice_wav, audio, rate)
                print(f"Decoding {label} ({len(audio) / rate:.1f}s)", flush=True)
                result = decode_meeting(engine, repo, MeetingSpec(MEETING, label),
                                        slice_wav, RESULTS / "work" / label,
                                        model_name, run_offline=False)
                cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            decoded[label] = result["draft_segments"]
    finally:
        engine.stop()
        db.close()

    mix_score = score_timed_transcript(reference, decoded["mix"])
    selected_score = score_timed_transcript(reference, decoded["fixed-bin-selected"])
    result = {
        "experiment": f"IN1009_fixed_bin_selected_headset_vs_mix_{int(slice_s)}s",
        "model": model_name,
        "language": "en",
        "chunking": "production 5/20s",
        "prompt_tail_words": 50 if model_name == "auto" else 0,
        "reference_words": len(reference),
        "selector": selector,
        "mix": _core_score(mix_score),
        "fixed_bin_selected": _core_score(selected_score),
        "absolute_wer_change": selected_score["wer"] - mix_score["wer"],
        "segment_counts": {key: len(value) for key, value in decoded.items()},
        "limitation": "Source choice is reference-blind, but uses four separate close-talk headsets. Product meetings ordinarily have mic and system audio, so this is a single-meeting proxy and not a production gain. Selection of one channel per bin can drop overlapping speech.",
    }
    if parallel_headsets:
        all_segments = sorted(
            (segment for channel in range(4)
             for segment in decoded[f"headset-{channel}"]),
            key=lambda segment: (segment["start_s"], segment["end_s"]),
        )
        channel_audio = [load_wav(headset_path(channel), slice_s)[0]
                         for channel in range(4)]
        rate = selected_rate
        bin_n = int(round(selector["bin_s"] * rate))
        n_bins = int((slice_s * rate) // bin_n)
        energies = np.stack([
            np.mean(audio[:n_bins * bin_n].reshape(n_bins, bin_n).astype(np.float64) ** 2,
                    axis=1) for audio in channel_audio
        ])
        dominant = np.argmax(energies, axis=0)
        filtered = []
        for channel in range(4):
            for segment in decoded[f"headset-{channel}"]:
                first = max(0, int(segment["start_s"] / selector["bin_s"]))
                last = min(n_bins, max(first + 1,
                                       int(np.ceil(segment["end_s"] / selector["bin_s"]))))
                if first < n_bins and np.mean(dominant[first:last] == channel) >= 0.5:
                    filtered.append(segment)
        filtered.sort(key=lambda segment: (segment["start_s"], segment["end_s"]))
        result["all_headsets_naive_union"] = _core_score(
            score_timed_transcript(reference, all_segments))
        result["all_headsets_audio_dominance_filtered"] = _core_score(
            score_timed_transcript(reference, filtered))
        result["filtered_segment_count"] = len(filtered)
        result["parallel_source_limitation"] = (
            "Independent headset ASR can repeat cross-talk. Filtering keeps a "
            "segment only when its headset has the highest energy for at least "
            "half its duration; no reference words or speaker labels are used."
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", choices=("health", "monitor_real", "speaker_energy", "sources"))
    parser.add_argument("--model", default="auto", help="Meeting ASR backend for sources")
    parser.add_argument("--parallel-headsets", action="store_true")
    parser.add_argument("--slice-s", type=float, default=ASR_SLICE_S)
    args = parser.parse_args()
    started = time.perf_counter()
    result = (health() if args.experiment == "health" else
              monitor_real() if args.experiment == "monitor_real" else
              speaker_energy() if args.experiment == "speaker_energy" else
              sources(args.model, args.parallel_headsets, args.slice_s))
    result["wall_s"] = time.perf_counter() - started
    RESULTS.mkdir(parents=True, exist_ok=True)
    suffix = (f"-{args.model}-{int(args.slice_s)}s"
              f"{'-parallel' if args.parallel_headsets else ''}"
              if args.experiment == "sources" else "")
    path = RESULTS / f"{args.experiment}{suffix}.json"
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"Saved {path}")


if __name__ == "__main__":
    main()
