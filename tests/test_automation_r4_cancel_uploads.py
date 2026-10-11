"""Review round 4, F3: a Cancel that returned True wins for uploads and batches.

Also when it lands between the worker's result and its delivery on the Qt
thread. An upload is then canceled like a dictation; a multi-file upload
keeps what was done, as when the Cancel lands between its files, and says
it was canceled.
"""

from __future__ import annotations

from services.automation import catalog
from services.batch_upload import (
    BatchItem,
    BatchItemResult,
    BatchRelation,
    BatchResult,
    BatchUploadRequest,
)
from services.runtime.transcription import EMPTY_ASR_MESSAGE
from services.transcript_cleanup import CANCELED_REASON
from tests import test_automation_foundation_pipeline as foundation

_qapp = foundation._qapp
h = foundation.h

CANCELED = [catalog.RECORDING_CANCELED, f"{catalog.TRANSCRIPTION_FAILED}(canceled)"]


def kinds(h):
    """What was published, in order, with a failure's category."""
    return [e.type + (f"({e.data['error']})" if e.type == catalog.TRANSCRIPTION_FAILED else "")
            for e in h.bus.recent(500)]


def test_an_upload_canceled_after_its_worker_finished_is_canceled(h):
    h.controller.current_backend.is_transcribing = False
    h.runtime.upload_audio_file(h.audio)
    fn, args = h.submitted.pop(0)
    emitted = []
    h.controller.transcription_completed.handler = lambda *result: emitted.append(result)
    fn(*args)  # the worker's result is on its way to the Qt thread

    assert h.runtime.cancel() is True
    h.runtime.on_transcription_complete(*emitted[0])

    assert kinds(h) == CANCELED
    assert h.history.entries == []
    assert h.ui.statuses[-1] == "Error: Transcription canceled"
    assert not h.runtime.has_active_job


def _batch(h, *texts, combine=False, combined="", cleanup_error=None):
    items = tuple(BatchItem(audio_path=f"{h.audio}{n}", duration_seconds=1.0)
                  for n in range(len(texts)))
    request = BatchUploadRequest(
        items=items, relation=BatchRelation.SEQUENTIAL if combine else BatchRelation.SEPARATE)
    results = tuple(BatchItemResult(item, text=text) for item, text in zip(items, texts))
    return BatchResult(request=request, items=results, combined_text=combined or None,
                       combined_raw_text=combined or None, cleanup_error=cleanup_error)


def _batch_under_way(h):
    h.controller.current_backend.is_transcribing = False  # between files, or done
    assert h.runtime._claim_job()
    h.runtime._deliver_to_clipboard = False


def test_a_batch_canceled_after_its_files_finished_says_canceled(h):
    _batch_under_way(h)

    assert h.runtime.cancel() is True
    h.runtime.on_batch_complete(_batch(h, "One.", "Two."))

    # Files already done are kept, as when the Cancel lands between files.
    assert h.ui.statuses[-1] == "Canceled — kept 2 of 2 files"
    assert [entry["text"] for entry in h.history.entries] == ["One.", "Two."]
    assert kinds(h).count(catalog.HISTORY_SAVED) == 2


def test_a_combined_batch_canceled_after_it_finished_says_canceled(h):
    _batch_under_way(h)

    assert h.runtime.cancel() is True
    h.runtime.on_batch_complete(_batch(h, "One.", "Two.", combine=True, combined="One. Two."))

    assert h.ui.statuses[-1] == "Canceled — kept the transcript"


def test_a_combined_batch_canceled_during_its_cleanup_keeps_the_raw_transcript(h):
    _batch_under_way(h)

    assert h.runtime.cancel() is True
    h.runtime.on_batch_complete(_batch(h, "one", "two", combine=True, combined="one two",
                                       cleanup_error=CANCELED_REASON))

    assert h.ui.statuses[-1] == "Canceled — kept the transcript without AI cleanup"


def test_a_batch_canceled_with_nothing_to_keep_ends_canceled(h):
    _batch_under_way(h)

    assert h.runtime.cancel() is True
    h.runtime.on_batch_complete(_batch(h, "", ""))

    assert kinds(h) == CANCELED
    assert h.ui.statuses[-1] == "Error: Transcription canceled"
    assert EMPTY_ASR_MESSAGE not in h.ui.statuses
