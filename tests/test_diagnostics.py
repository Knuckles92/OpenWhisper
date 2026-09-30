"""A support report must remain useful without exporting private user content."""
import json
import logging
import threading
import time

import pytest

from services import diagnostics


def test_export_allowlists_settings_and_failure_fields(tmp_path, monkeypatch):
    from config import SETTINGS_FILENAME
    monkeypatch.setattr("services.gpu_info.nvidia_gpu", lambda: None)
    private = "secret transcript sk-private-key user@example.com C:/Private/recording.wav"
    (tmp_path / SETTINGS_FILENAME).write_text(json.dumps(dict(
        selected_model="local_whisper", whisper_device="cpu", whisper_model=private,
        api_key=private, remote_pairings=[private], microphone=private,
        local_asr_models={"nemo": private},
    )), encoding="utf-8")
    journal = [dict(utc="2026-01-01T00:00:00+00:00", component="recorder", category="OSError",
                    line=123, message=private, traceback=private),
               dict(utc=private, component="recorder", category="OSError", line=123),
               dict(utc="2026-01-01", component=private, category="Error", line=123)]
    (tmp_path / diagnostics.JOURNAL_NAME).write_text(json.dumps(journal), encoding="utf-8")
    report = diagnostics.collect_report(root=tmp_path)
    serialized = json.dumps(report)
    assert private not in serialized
    assert report["engine"]["selected_model"] == "local_whisper"
    assert report["engine"]["whisper_model"] == "default_or_custom"
    assert report["recent_failures"] == [dict(utc="2026-01-01T00:00:00+00:00", component="recorder", category="OSError", line=123)]
    assert "app_version" in report and "numpy" in report["packages"]


def test_failure_handler_never_formats_message_or_exception(tmp_path):
    class PrivateException(ValueError):
        def __str__(self):
            raise AssertionError("Exception string must not be inspected")
    class PrivateMessage:
        def __str__(self):
            raise AssertionError("Log message must not be formatted")
    handler = diagnostics.FailureCapture(tmp_path / "journal.json")
    error = PrivateException()
    record = logging.LogRecord("services.recorder", logging.ERROR, "private/path", 44,
                               PrivateMessage(), (), (PrivateException, error, None))
    for _ in range(60):
        handler.handle(record)
    events = json.loads(handler.path.read_text(encoding="utf-8"))
    assert len(events) == diagnostics.MAX_FAILURES
    assert events[-1]["category"] == "ValueError"
    assert events[-1]["component"] == "recorder"
    assert "private" not in json.dumps(events)


def test_diagnostic_write_failure_does_not_replace_original_failure(tmp_path, monkeypatch):
    handler = diagnostics.FailureCapture(tmp_path / "journal.json")
    monkeypatch.setattr(diagnostics.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    handler.handle(logging.LogRecord("services.recorder", logging.ERROR, "", 1, "private", (), None))
    assert handler.events[-1]["category"] == "LoggedError"


def test_export_never_overwrites_existing_file(tmp_path, monkeypatch):
    output = tmp_path / "keep.json"
    output.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(diagnostics, "collect_report", lambda: {"schema": 1})
    with pytest.raises(FileExistsError):
        diagnostics.main([str(output)])
    assert output.read_text() == "keep"


def test_metric_sample_bounds_and_export_statistics(tmp_path, monkeypatch):
    monkeypatch.setattr("services.gpu_info.nvidia_gpu", lambda: None)
    capture = diagnostics.MetricsCapture(tmp_path / diagnostics.METRICS_NAME)
    for value in range(110):
        capture.record(dict(stop_to_result_s=value, secret="private transcript", cancel_s=float("nan")))
    capture.record(dict(dropped_frames=-1, captured_frames=True))
    assert capture.close(timeout=2)
    report = diagnostics.collect_report(root=tmp_path)
    metric = report["recent_metrics"]["stop_to_result_s"]
    assert metric == dict(count=100, latest=109, maximum=109, p50=59.5, p95=104.05)
    assert "cancel_s" not in report["recent_metrics"]
    assert "private transcript" not in capture.path.read_text()
    assert "dropped_frames" not in report["recent_metrics"]


def test_record_metrics_is_inert_before_app_logging(monkeypatch):
    monkeypatch.setattr(diagnostics, "_metrics", None)
    diagnostics.record_metrics(stop_to_result_s=1)


def test_stalled_metric_storage_does_not_block_qt_or_build_write_queue(tmp_path, monkeypatch):
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QApplication

    capture = diagnostics.MetricsCapture(tmp_path / diagnostics.METRICS_NAME)
    entered, release = threading.Event(), threading.Event()
    snapshots = []
    def stalled_write(snapshot):
        snapshots.append(snapshot)
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(capture, "_write_snapshot", stalled_write)
    try:
        capture.record(dict(stop_to_result_s=1))
        assert entered.wait(2)
        delivered = []
        def qt_callback():
            for value in range(1000):
                capture.record(dict(stop_to_result_s=value))
            delivered.append(True)
        QTimer.singleShot(0, qt_callback)
        started = time.monotonic()
        QApplication.instance().processEvents()
        assert delivered and time.monotonic() - started < .5
        assert len(capture.samples["stop_to_result_s"]) == diagnostics.MAX_METRIC_SAMPLES
        assert len(snapshots) == 1
        started = time.monotonic()
        assert not capture.close(timeout=.02)
        assert time.monotonic() - started < .5
    finally:
        release.set()
        assert capture.close(timeout=2)
    assert len(snapshots) == 2
    assert snapshots[-1]["stop_to_result_s"][-1] == 999


def test_logging_close_flushes_shutdown_metric_with_bounded_wait(tmp_path):
    capture = diagnostics.MetricsCapture(tmp_path / diagnostics.METRICS_NAME)
    handler = diagnostics.FailureCapture(tmp_path / diagnostics.JOURNAL_NAME)
    handler.metrics_capture = capture
    capture.record(dict(shutdown_s=.25))
    handler.close()
    assert capture.flush(timeout=2)
    assert json.loads(capture.path.read_text())["shutdown_s"] == [.25]


@pytest.mark.parametrize("content", ["not JSON", "[]", "null"])
def test_malformed_settings_and_journal_are_safe(tmp_path, monkeypatch, content):
    from config import SETTINGS_FILENAME
    monkeypatch.setattr("services.gpu_info.nvidia_gpu", lambda: None)
    (tmp_path / SETTINGS_FILENAME).write_text(content)
    (tmp_path / diagnostics.JOURNAL_NAME).write_text(content)
    assert diagnostics.collect_report(root=tmp_path)["recent_failures"] == []
