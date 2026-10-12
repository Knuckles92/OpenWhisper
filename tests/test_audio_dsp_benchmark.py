"""Benchmark validity checks, independent of native denoisers and ASR models."""

from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from benchmarks.audio_dsp import audioforge_worker
from benchmarks.audio_dsp import run as runner
from benchmarks.audio_dsp.audio import FRAME, RATE, rms, signal_metrics, write_audio
from benchmarks.audio_dsp.prepare import mix_at_snr, prepare
from benchmarks.audio_dsp.prepare import parser as prepare_parser
from benchmarks.audio_dsp.processors import make_processor, render
from benchmarks.audio_dsp.run import frame_summary, load_clips, parser, run, summarize
from benchmarks.meeting_mode.metrics import score_text


def test_delayed_processor_keeps_last_sample_and_resets_state():
    instances = []

    class Delay:
        latency_samples = FRAME

        def __init__(self):
            self.previous = np.zeros(FRAME, dtype=np.float32)
            self.closed = False
            instances.append(self)

        def process(self, frame):
            output, self.previous = self.previous, frame.copy()
            return output

        def close(self):
            self.closed = True

    audio = np.linspace(-0.3, 0.3, FRAME + 17, dtype=np.float32)
    for _ in range(2):
        output, timing = render(audio, Delay)
        np.testing.assert_array_equal(output, audio)
        assert len(timing["frame_ms"]) == 3
        assert timing["algorithmic_latency_ms"] == 10
    assert all(instance.closed for instance in instances)
    assert len(instances) == 2


def test_raw_exact_and_highpass_rejects_rumble():
    time = np.arange(RATE) / RATE
    low = np.sin(2 * np.pi * 20 * time).astype(np.float32) * 0.1
    raw, _ = render(low, lambda: make_processor("raw"))
    filtered, _ = render(low, lambda: make_processor("highpass"))
    np.testing.assert_array_equal(raw, low)
    assert rms(filtered[RATE // 2 :]) < rms(low[RATE // 2 :]) / 10


def test_snr_is_measured_on_speech_and_pair_does_not_clip():
    clean = np.array([0, 0.9, -0.9, 0], dtype=np.float32)
    mask = np.array([False, True, True, False])
    noisy, reference = mix_at_snr(clean, np.ones(4, dtype=np.float32), 10, mask)
    measured = 20 * np.log10(rms(reference[mask]) / rms((noisy - reference)[mask]))
    assert measured == pytest.approx(10, abs=1e-5)
    assert np.max(np.abs(noisy)) <= 0.950001


def test_silence_and_invalid_output_are_not_scored_as_speech():
    audio = np.zeros(FRAME, dtype=np.float32)
    signal = signal_metrics(audio, audio, audio, [])
    assert signal["output_si_sdr_db"] is None
    assert signal["speech_level_delta_db"] is None
    assert signal["energy_above_minus40_dbfs_fraction"] == 0
    for output in (audio[:-1], np.full(FRAME, np.nan)):
        with pytest.raises(ValueError):
            signal_metrics(audio, output, audio, [])


def test_offline_simulation_never_passes_frame_latency_budget():
    result = frame_summary(
        [{"frame_ms": None, "algorithmic_latency_ms": 20, "simulation_ms": 5}], 5
    )
    assert result["frame_p95_ms"] is None
    assert result["p95_within_budget"] is None
    result = frame_summary([{"frame_ms": [1, 6, 11], "algorithmic_latency_ms": 10}], 5)
    assert result["frames_over_budget"] == 2
    assert result["frames_over_deadline"] == 1
    assert result["p95_within_budget"] is False


def test_aggregation_is_word_weighted_and_failure_is_incomplete():
    rows = []
    for profile in ("raw", "highpass"):
        for index, (reference, transcript) in enumerate(
            (("one", "two"), ("one two three", "one two three"), ("", "ghost word"))
        ):
            rows.append(
                {
                    "id": str(index),
                    "profile": profile,
                    "group": "noise" if not reference else "clean",
                    "duration_s": 1,
                    "asr": {
                        "base": {
                            "score": score_text(reference, transcript),
                            "decode_s": 0.1,
                        }
                    },
                }
            )
    summary = summarize(rows, ["raw", "highpass"], ["base"], 3)
    groups = summary["highpass"]["models"]["base"]["groups"]
    assert groups["clean"]["wer"] == 0.25
    assert groups["noise"]["wer"] is None
    assert groups["noise"]["hallucinated_words"] == 2
    assert groups["clean"]["wer_delta_pp"] == 0
    rows[-1]["asr"]["base"] = {"error": "failed"}
    summary = summarize(rows, ["raw", "highpass"], ["base"], 3)
    assert not summary["highpass"]["models"]["base"]["complete"]
    assert summary["highpass"]["models"]["base"]["paired_clips"] == 2


def test_partial_report_waits_for_raw_baseline():
    row = {
        "id": "later",
        "profile": "highpass",
        "group": "clean",
        "duration_s": 1,
        "asr": {"base": {"score": score_text("one", "one"), "decode_s": 0.1}},
    }
    summary = summarize([row], ["raw", "highpass"], ["base"], 1)
    assert summary["highpass"]["models"]["base"]["paired_clips"] == 0


def test_asr_uses_isolated_offline_environment_and_closes_backend(
    corpus, tmp_path, monkeypatch
):
    calls = []

    class Backend:
        def transcribe(self, path):
            assert os.environ["HF_HUB_OFFLINE"] == "1"
            assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
            assert os.environ["OPENWHISPER_DATA_DIR"] == str(
                (tmp_path / "asr" / "settings").resolve()
            )
            calls.append(path)
            return "hello world"

        def cleanup(self):
            calls.append("cleanup")

    monkeypatch.setattr(runner, "make_backend", lambda *_: Backend())
    monkeypatch.setattr(runner, "model_identity", lambda *_: {"fixture": True})
    args = parser().parse_args(
        [
            str(corpus),
            "--output",
            str(tmp_path / "asr"),
            "--profiles",
            "highpass",
            "--models",
            "base",
            "--repeats",
            "1",
        ]
    )
    assert run(args) == 0
    report = json.loads((args.output / "results.json").read_text())
    assert report["accuracy_measured"] is True
    assert report["summary"]["highpass"]["models"]["base"]["complete"]
    assert len(calls) == 4  # Warm-up, raw, filtered, cleanup.
    assert calls[-1] == "cleanup"


def test_audioforge_worker_uses_native_aligned_output_without_oracle_labels(
    tmp_path, monkeypatch
):
    source, output, metadata = [
        tmp_path / name for name in ("input.npy", "output.npy", "metadata.json")
    ]
    audio = np.arange(FRAME, dtype=np.float32) / FRAME
    np.save(source, audio)
    calls = []

    def simulate(samples, rate, bands, settings):
        calls.append(settings)
        assert rate == RATE and len(bands) == 10
        assert "vad_probabilities" not in settings
        assert not settings["vad_available"]
        return {
            "output_audio": samples,
            "non_finite_output": False,
            "chain_latency_samples": 800,
        }

    core = SimpleNamespace(__file__=__file__, simulate_auto_eq_chain=simulate)
    monkeypatch.setattr(audioforge_worker.importlib, "import_module", lambda *_: core)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            str(source),
            str(output),
            str(metadata),
            "--root",
            str(tmp_path),
            "--profile",
            "audioforge_full",
        ],
    )
    # The worker is normally a subprocess; restore its source-path insertion here.
    monkeypatch.setattr(sys, "path", sys.path.copy())
    audioforge_worker.main()
    np.testing.assert_array_equal(np.load(output), audio)
    info = json.loads(metadata.read_text())
    assert info["algorithmic_latency_ms"] == pytest.approx(800 / 48)
    assert info["frame_ms"] is None
    assert calls[0]["compressor_auto_makeup_enabled"]


@pytest.fixture
def corpus(tmp_path):
    audio = (
        np.sin(2 * np.pi * 200 * np.arange(RATE // 2) / RATE).astype(np.float32) * 0.1
    )
    path = tmp_path / "source.wav"
    write_audio(path, audio)
    manifest = tmp_path / "source.json"
    manifest.write_text(
        json.dumps(
            {
                "clips": [
                    {
                        "id": "speech",
                        "audio_path": "source.wav",
                        "reference": "hello world",
                        "group": "clean",
                    }
                ]
            }
        )
    )
    return manifest


def test_preparation_is_deterministic_and_checks_generated_hashes(corpus, tmp_path):
    outputs = [tmp_path / "a", tmp_path / "b"]
    for output in outputs:
        prepare(prepare_parser().parse_args([str(corpus), "--output", str(output)]))
    left, right = [
        json.loads((output / "manifest.json").read_text()) for output in outputs
    ]
    assert left["generated_sha256"] == right["generated_sha256"]
    assert len(left["clips"]) == 8
    assert left["clips"][-1]["reference"] == ""
    load_clips(outputs[0] / "manifest.json")
    (outputs[0] / "0000-clean.wav").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="Corpus hash mismatch"):
        load_clips(outputs[0] / "manifest.json")


def test_missing_requested_denoiser_fails_and_preserves_environment(corpus, tmp_path):
    before = {
        key: os.environ.get(key)
        for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "OPENWHISPER_DATA_DIR")
    }
    args = parser().parse_args(
        [
            str(corpus),
            "--output",
            str(tmp_path / "run"),
            "--profiles",
            "rnnoise",
            "--repeats",
            "1",
        ]
    )
    assert run(args) == 1
    report = json.loads((args.output / "results.json").read_text())
    assert report["accuracy_measured"] is False
    assert report["complete"] is False
    assert report["summary"]["raw"]["completed_dsp_clips"] == 1
    assert report["summary"]["rnnoise"]["completed_dsp_clips"] == 0
    assert "--rnnoise-library" in report["results"][1]["error"]
    assert before == {key: os.environ.get(key) for key in before}
    with pytest.raises(ValueError, match="not be overwritten"):
        run(args)
