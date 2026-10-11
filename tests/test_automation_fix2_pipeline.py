"""Review round 2 for the transcription runtime: cancel, previews and the job slot."""

from services.dictation_pipeline import DictationJob
from tests import test_automation_foundation_pipeline as foundation

# The foundation's harness fixtures, used here by name.
_qapp = foundation._qapp
h = foundation.h
_events = foundation._events


class Recorder:
    """The recorder's surface a start, stop and cancel use."""

    capture_canceled = False

    def __init__(self, *, recording=False):
        self.is_recording = recording

    def start_recording(self):
        self.is_recording = True
        return True

    def stop_recording(self):
        return True  # post-roll keeps it recording a little longer

    def cancel_recording(self):
        self.is_recording = False

    def get_recording_duration(self):
        return 1.5


# -- finding 1: the transcription runtime says whether it canceled anything ----------


def test_cancel_reports_a_recording_or_job_it_canceled(h):
    h.controller.recorder = Recorder(recording=True)
    h.controller.recording_state_changed = foundation.Signal()
    assert h.runtime.cancel() is True

    assert h.runtime._claim_job(DictationJob())
    h.controller.current_backend.is_transcribing = False
    assert h.runtime.cancel() is True


def test_cancel_with_nothing_going_on_reports_nothing_canceled(h):
    h.controller.current_backend.is_transcribing = False
    assert h.runtime.cancel() is False
    assert h.controller.status_update.calls[-1] == ("Canceled",)
