"""Bounded, explicit admission telemetry, separate from engine timing."""
from services.remote_asr.activity import HostActivity, MAX_REQUEST_SAMPLES, empty_snapshot


def test_request_samples_are_bounded_and_totals_are_not():
    activity = HostActivity()
    for index in range(MAX_REQUEST_SAMPLES + 100):
        activity.request_finished("device", "laptop", index / 1000, (index + 10) / 1000, "ok")
    snapshot = activity.snapshot()
    load = snapshot["load"]
    assert load["requests"] == MAX_REQUEST_SAMPLES + 100
    assert load["sample_count"] == MAX_REQUEST_SAMPLES
    assert len(activity._request_samples) == MAX_REQUEST_SAMPLES
    assert load["queue_p95_ms"] == 343
    assert load["server_p95_ms"] == 353
    assert snapshot["transcriptions"] == 0
    assert snapshot["host_s"] == 0


def test_capacity_and_preview_outcomes_do_not_inflate_engine_errors():
    activity = HostActivity()
    activity.request_finished("device", "laptop", .2, .4, "ok")
    activity.request_finished("device", "laptop", 0, .001, "busy")
    activity.request_finished("device", "laptop", 0, .001, "preview_skipped")
    activity.connection_rejected("authenticated_capacity")
    activity.connection_rejected("anonymous_handshake_capacity")
    activity.connection_rejected("arbitrary peer identity")
    snapshot = activity.snapshot()
    assert snapshot["load"]["work_rejections"] == 1
    assert snapshot["load"]["preview_skips"] == 1
    assert snapshot["load"]["connection_rejections"] == {
        "authenticated_capacity": 1, "anonymous_handshake_capacity": 1, "other": 1}
    assert snapshot["errors"] == 0
    assert snapshot["events"] == []
    assert snapshot["devices"]["device"]["queue_s"] == .2
    assert snapshot["devices"]["device"]["server_s"] == .402


def test_request_metrics_reset_with_host_session():
    activity = HostActivity()
    activity.request_finished("device", "laptop", .2, .4, "busy")
    activity.connection_rejected("authenticated_capacity")
    activity.reset()
    assert activity.snapshot()["load"] == empty_snapshot()["load"]
    assert activity.snapshot()["devices"] == {}
