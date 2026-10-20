"""A real-TLS reply distinguishes admission wait from engine execution."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest

from services.remote_asr.admission import WorkAdmission
from services.remote_asr.client import _host_seconds
from tests.test_remote_engine import (
    _connect, _pair, _tone, _wait_for, engine, host, identity, no_real_tailscale, store,
)


@pytest.mark.parametrize("reply, expected", [
    ({"host_ms": 100}, .1),
    ({"server_total_ms": 300, "host_ms": 100}, .3),
    ({"server_total_ms": 0, "host_ms": 100}, 0),
    ({"server_total_ms": float("nan"), "host_ms": 100}, .1),
    ({"server_total_ms": float("inf"), "host_ms": 100}, .1),
    ({"server_total_ms": -1, "host_ms": 100}, .1),
    ({"server_total_ms": True, "host_ms": 100}, .1),
    ({"server_total_ms": "300", "host_ms": 100}, .1),
    ({"host_ms": float("nan")}, None),
    ({"host_ms": float("inf")}, None),
    ({"host_ms": True}, None),
    ({}, None),
])
def test_client_prefers_total_and_rejects_invalid_timing(reply, expected):
    assert _host_seconds(reply) == expected


def test_queue_time_is_reported_separately_over_real_tls(host, engine, monkeypatch):
    host._work_admission = WorkAdmission(1, 4, 3)
    first = _connect(host, _pair(host))
    second = _connect(host, _pair(host))
    entered = threading.Event()
    release = threading.Event()
    original = engine.transcribe
    count = 0
    replies = []
    await_reply = second._await_reply

    def capture_reply(*args):
        reply = await_reply(*args)
        replies.append(reply)
        return reply

    def blocked_once(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            entered.set()
            assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "transcribe", blocked_once)
    monkeypatch.setattr(second, "_await_reply", capture_reply)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            blocked = pool.submit(first.request, "transcribe", audio=_tone(1600))
            assert entered.wait(3)
            waiting = pool.submit(second.request_timed, "transcribe", audio=_tone(1600))
            assert _wait_for(lambda: host.admission_snapshot()["queued"] == 1)
            time.sleep(.06)
            release.set()
            blocked.result(timeout=3)
            _result, server_s = waiting.result(timeout=3)
        reply = replies[0]
        assert reply["queue_ms"] >= 50
        assert reply["server_total_ms"] >= reply["queue_ms"]
        assert reply["server_total_ms"] >= reply["host_ms"] + 50
        assert server_s == reply["server_total_ms"] / 1000
        load = host.activity.snapshot()["load"]
        assert load["requests"] == 2
        assert load["queue_p95_ms"] >= 50
    finally:
        release.set()
        first.close()
        second.close()


def test_disposable_preview_does_not_count_as_final_audio(host):
    observed = []
    host._on_event = lambda kind, detail: observed.append(host.activity.snapshot()["load"].copy()) if kind == "activity" and detail.get("request_finished") else None
    client = _connect(host, _pair(host))
    try:
        client.request("transcribe", audio=_tone(1600), preview=True)
        snapshot = host.activity.snapshot()
        assert snapshot["previews"] == 1
        assert snapshot["transcriptions"] == 0
        assert snapshot["audio_s"] == 0
        client.request("transcribe", audio=_tone(1600))
        snapshot = host.activity.snapshot()
        assert snapshot["previews"] == 1
        assert snapshot["transcriptions"] == 1
        assert snapshot["audio_s"] == .1
        assert snapshot["load"]["requests"] == 2
        assert [item["requests"] for item in observed] == [1, 2]
    finally:
        client.close()


@pytest.mark.parametrize("preview", [False, True])
def test_rejected_work_has_timing_and_separate_counter(host, monkeypatch, preview):
    from services.remote_asr import protocol

    observed = []
    host._on_event = lambda kind, detail: observed.append(host.activity.snapshot()["load"].copy()) if kind == "activity" else None
    monkeypatch.setattr(host._work_admission, "acquire", lambda *a, **kw: False)
    frame = protocol.pack_request({"id": "rejected", "op": "transcribe", "preview": preview}, payload=b"")
    reply = host._dispatch(frame, None, (), "connection", set(), "Device", "device")
    assert reply["server_total_ms"] >= reply["queue_ms"] >= 0
    assert "host_ms" not in reply
    load = host.activity.snapshot()["load"]
    assert load["requests"] == 1
    assert load["preview_skips"] == int(preview)
    assert load["work_rejections"] == int(not preview)
    assert host.activity.snapshot()["transcriptions"] == 0
    assert observed == [load]
