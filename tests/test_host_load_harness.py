"""The optional load harness must count overload rather than hide arrivals."""
import random

import pytest

pytest.importorskip("psutil")

from benchmarks.host_load.runner import Measurements, default_args, make_schedule, summary
from benchmarks.host_load.profiles import PROFILES


def test_profile_calibration_and_explicit_whisper_uncertainty():
    gpu = PROFILES["gpu-parakeet"]
    assert abs(gpu.intercept_s + gpu.seconds_per_audio_second * 70.7 - .462) < .01
    cpu = PROFILES["cpu-parakeet"]
    assert abs(cpu.intercept_s + cpu.seconds_per_audio_second * 8.8 - 2.2) < .01
    assert "Hypothetical" in PROFILES["gpu-whisper-turbo"].evidence


def test_poisson_schedule_is_reproducible_and_contains_no_execution_feedback():
    args = default_args(duration=100., rate=.2)
    first = make_schedule(args, 3)
    assert first == make_schedule(args, 3)
    assert first != make_schedule(args, 4)
    assert all(0 <= row["at"] < 100 for row in first)
    assert all(2 <= row["seconds"] <= 30 for row in first)
    assert first[0]["at"] == random.Random(args.seed + 3 * 7919).expovariate(args.rate)


def test_short_poisson_cell_can_have_no_arrivals():
    assert make_schedule(default_args(duration=.001, rate=.05), 0) == []
    assert not summary(Measurements(), .001)["acceptance_observed"]


def test_window_preview_uses_transcribe_and_native_stream_is_not_windowed():
    args = default_args(duration=20., workload="preview", preview="window")
    window = make_schedule(args, 1)
    assert all(row["op"] == "transcribe" for row in window)
    preview = [row for row in window if row["kind"] == "preview"]
    assert preview[0]["seconds"] == pytest.approx(3.0186)
    assert preview[1]["seconds"] == pytest.approx(3.7686)
    args.preview = "native"
    native = make_schedule(args, 1)
    assert all(row["seconds"] == pytest.approx(.7663) for row in native if row["kind"] == "preview")
    assert any(row["kind"] == "preview_finish" for row in native)


def test_censored_offered_work_cannot_pass_acceptance():
    data = Measurements()
    data.add(op="connect", status="ok", client=0)
    data.add(op="transcribe", kind="final", role="remote", status="ok", client=0,
             audio_s=3., latency_s=.1, schedule_lag_s=0.)
    assert summary(data, 10.)["acceptance_observed"]
    data.add(op="transcribe", kind="final", role="remote", status="not_started", client=0, audio_s=3.)
    result = summary(data, 10.)
    assert not result["acceptance_observed"]
    assert result["not_started"] == 1
    assert result["requests"] == 2
    assert result["audio_seconds_per_wall_second"] == .3


def test_full_featured_capacity_counts_history_refusal():
    data = Measurements()
    data.add(op="connect", status="ok", client=0)
    data.add(op="history_connect", status="refused", client=0)
    data.add(op="transcribe", kind="final", role="remote", status="ok", client=0,
             audio_s=3., latency_s=.1, schedule_lag_s=0.)
    assert not summary(data, 10.)["acceptance_observed"]
    assert summary(data, 10.)["full_featured_connected"] == 0


def test_lifecycle_rows_are_not_audio_requests_and_refusals_fail_acceptance():
    data = Measurements()
    data.add(op="transcribe", kind="final", role="remote", status="ok", client=0,
             audio_s=3., latency_s=.1, schedule_lag_s=0.)
    data.add(op="cycle_connect", status="ok", client=0)
    data.add(op="cycle_history_connect", status="ok", client=0)
    result = summary(data, 10.)
    assert result["requests"] == 1
    assert result["audio_seconds_per_wall_second"] == .3
    assert result["lifecycle_reconnects"] == 1
    assert result["acceptance_observed"]
    data.add(op="cycle_history_connect", status="refused", client=0)
    data.add(op="cycle_connect", status="refused", client=0)
    result = summary(data, 10.)
    assert result["requests"] == 1
    assert result["connection_refusals"] == result["history_refusals"] == 1
    assert not result["acceptance_observed"]
