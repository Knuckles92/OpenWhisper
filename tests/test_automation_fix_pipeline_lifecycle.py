"""Every job's events end, failures carry a category, and files publish completions."""

import time

import pytest
from PyQt6.QtTest import QTest

from services.automation import catalog
from services.batch_upload import (
    BatchItem,
    BatchItemResult,
    BatchRelation,
    BatchResult,
    BatchUploadRequest,
)
from services.dictation_pipeline import DictationJob, JobMode
from tests import test_automation_foundation_pipeline as foundation

# The foundation's harness fixtures, used here by name.
_qapp = foundation._qapp
h = foundation.h
_events = foundation._events


def _all(h, type_, count, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(_events(h, type_, 0)) < count:
        time.sleep(0.002)
    time.sleep(0.02)
    return _events(h, type_, 0)


# -- multi-file uploads (pipeline #6) --------------------------------------------

def _batch(h, *texts, combine=False, combined=""):
    items = tuple(
        BatchItem(audio_path=f"{h.audio}{n}", duration_seconds=1.0 + n)
        for n in range(len(texts))
    )
    request = BatchUploadRequest(
        items=items, relation=BatchRelation.SEQUENTIAL if combine else BatchRelation.SEPARATE)
    results = tuple(BatchItemResult(item, text=text, raw_text=text.lower() if text else None,
                                    cleanup_provider="openrouter" if text else None,
                                    cleanup_model="m" if text else None)
                    for item, text in zip(items, texts))
    return BatchResult(request=request, items=results, combined_text=combined or None)


def test_a_multi_file_upload_publishes_a_completion_per_saved_file(h):
    assert h.runtime._claim_job()
    h.runtime._deliver_to_clipboard = False
    h.runtime.on_batch_complete(_batch(h, "One.", "", "Three."))

    completed = _all(h, "transcription.completed", 2)
    assert [(e.data["mode"], e.data["text"], e.data["raw_text"]) for e in completed] == [
        ("file", "One.", "one."), ("file", "Three.", "three."),
    ]
    assert completed[0].data["cleanup"]["provider"] == "openrouter"
    assert completed[1].data["audio_s"] == 3.0
    assert len(_all(h, "history.saved", 2)) == 2


def test_a_combined_upload_publishes_one_completion(h):
    assert h.runtime._claim_job()
    h.runtime._deliver_to_clipboard = False
    h.runtime.on_batch_complete(_batch(h, "One.", "Two.", combine=True, combined="One. Two."))

    completed, = _events(h, "transcription.completed")
    assert completed.data["text"] == "One. Two." and completed.data["mode"] == "file"


def test_an_empty_upload_says_there_was_no_speech(h):
    assert h.runtime._claim_job()
    h.runtime._deliver_to_clipboard = False
    h.runtime.on_batch_complete(_batch(h, "", ""))

    failed, = _events(h, "transcription.failed")
    assert failed.data["error"] == "no_speech"
    assert _events(h, "transcription.completed", timeout=0.05) == []


# -- lifecycles that ended without a completion (pipeline #9) --------------------

def test_an_empty_dictation_publishes_no_speech(h):
    h.controller.current_backend.transcribe = lambda path: "   "
    job = DictationJob(mode=JobMode.DICTATION)
    assert h.runtime._claim_job(job)
    h.runtime.transcribe_audio_file(h.audio)

    failed, = _events(h, "transcription.failed")
    assert failed.data == {"job_id": job.id, "error": "no_speech", "message": None}
    assert not h.runtime.has_active_job


class _Recorder:
    capture_canceled = False

    def __init__(self):
        self.is_recording = True

    def stop_recording(self):
        self.is_recording = False
        return True

    def get_recording_duration(self):
        return 2.0


def test_a_stop_refused_as_busy_publishes_recording_canceled(h):
    h.controller.recorder = _Recorder()
    h.controller._streaming_enabled = False
    h.controller.recording_state_changed = foundation.Signal()
    job = DictationJob(mode=JobMode.DICTATION, source="hotkey")
    h.runtime._job = job
    h.controller._backup_in_progress = True  # the slot can't be claimed
    h.runtime.stop_recording()

    canceled, = _events(h, "recording.canceled")
    assert canceled.data == {"job_id": job.id}
    assert h.submitted == []


@pytest.fixture
def sent():
    """What PreviewEvents hands to TranscriptionRuntime.publish_preview."""
    return []


def _previews(sent, count, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(sent) < count:
        QTest.qWait(5)
    return [(text, final) for _job, text, final in sent]


def test_the_last_throttled_preview_still_goes_out(_qapp, sent):
    from services.application_controller import PreviewEvents

    previews = PreviewEvents(lambda *preview: sent.append(preview))
    job = DictationJob()
    previews.offer(job, "hel", False)
    previews.offer(job, "hello", False)       # within a quarter second: held
    previews.offer(job, "hello wor", False)   # replaces the held one
    assert _previews(sent, 2) == [("hel", False), ("hello wor", False)]
    assert all(preview[0] is job for preview in sent)


def test_a_final_preview_goes_out_at_once_and_drops_the_held_one(_qapp, sent):
    from services.application_controller import PreviewEvents

    previews = PreviewEvents(lambda *preview: sent.append(preview))
    job = DictationJob()
    previews.offer(job, "hel", False)
    previews.offer(job, "hello", False)
    previews.offer(job, "hello world", True)
    QTest.qWait(400)
    assert [(text, final) for _job, text, final in sent] == [
        ("hel", False), ("hello world", True),
    ]


# -- failures carry a category, never raw exception text (security #13) ----------

def test_a_failure_publishes_its_category_not_the_exception(h):
    job = DictationJob(mode=JobMode.DICTATION)
    assert h.runtime._claim_job(job)
    h.runtime.on_transcription_error(
        r"[Errno 2] No such file: 'C:\Users\sam\AppData\Local\OpenWhisper\audio.wav'")

    failed, = _events(h, "transcription.failed")
    assert failed.data == {"job_id": job.id, "error": "failed", "message": None}


def test_a_cancel_publishes_canceled(h):
    assert h.runtime._claim_job(DictationJob())
    h.runtime._cancel_requested.set()
    h.runtime.on_transcription_error("Transcription canceled")
    failed, = _events(h, "transcription.failed")
    assert failed.data["error"] == "canceled" and failed.data["message"] is None


def test_a_refusal_publishes_its_copy(h):
    job = DictationJob(mode=JobMode.COMMAND)
    assert h.runtime._claim_job(job)
    h.runtime._refuse("Select text first")
    failed, = _events(h, "transcription.failed")
    assert failed.data == {"job_id": job.id, "error": "refused", "message": "Select text first"}


def test_the_builder_never_passes_free_text_as_the_error():
    data = catalog.transcription_failed(None, "Traceback: C:\\secret\\path")
    assert data == {"job_id": "", "error": "failed", "message": None}
    assert catalog.transcription_failed(None, "refused", message="Select text first")[
        "message"] == "Select text first"
    assert catalog.transcription_failed(None, "failed", message="C:\\x")["message"] is None
