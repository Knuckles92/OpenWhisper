"""Review round 3, F6: a program's refused run says nothing to the user."""

from __future__ import annotations

import pytest

from services.automation.control import ControlError
from services.dictation_pipeline import DictationJob, JobMode
from tests import test_automation_fix2_pipeline as fix2
from tests import test_automation_foundation_pipeline as foundation

_qapp = foundation._qapp
h = foundation.h

BUSY = "A transcription is already in progress — wait before rewriting text"


# -- F6: a program's refused run says nothing to the user ---------------------------


def test_a_programs_run_while_the_user_records_tells_the_user_nothing(h):
    h.controller.recorder.is_recording = True

    with pytest.raises(ControlError) as busy:
        h.runtime.run_action_text("a1", "hello", source="api")

    assert busy.value.code == "busy"
    # The API's answer is the report; the user's "Recording..." stays.
    assert h.ui.notices == []
    assert h.controller.status_update.calls == []


def test_a_programs_run_while_a_recording_starts_tells_the_user_nothing(h):
    starter = fix2._hold_capture_lock(h, 0.5)
    with pytest.raises(ControlError) as busy:
        h.runtime.run_action_text("a1", "hello", source="mcp")
    starter.join(5)

    assert busy.value.code == "busy"
    assert h.ui.notices == [] and h.controller.status_update.calls == []


def test_the_users_own_rewrite_still_says_why_it_was_refused(h):
    h.controller.recorder.is_recording = True
    transform = DictationJob(mode=JobMode.TRANSFORM, source="hotkey")

    assert not h.runtime.begin_rewrite_job(transform, source_name="Transform · Polish")

    assert h.ui.notices == [BUSY]
    assert h.controller.status_update.calls == [(BUSY,)]
