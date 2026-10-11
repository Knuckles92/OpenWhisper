"""An action's answer may paste while its own job is still saving its history."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services.dictation_pipeline import DictationJob, JobMode
from services.runtime import transcription
from services.runtime.transcription import TranscriptionRuntime
from services.settings import SettingsKey
from tests.fakes.settings import InMemorySettings


class FakeUI:
    def __init__(self):
        self.stages, self.notices, self.statuses = [], [], []

    def set_status(self, text):
        self.statuses.append(text)

    def show_notice(self, text, done=False):
        self.notices.append(text)

    def discard_clipboard_prefetch(self):
        pass

    def stage_transcript_for_paste(self, text, **kwargs):
        self.stages.append(text)
        return SimpleNamespace(written=True, lease=None, restore_unavailable=False)

    def schedule_clipboard_restore(self, stage):
        return True


class Signal:
    def emit(self, *args):
        pass


@pytest.fixture
def h(monkeypatch):
    ui = FakeUI()
    paste = Mock()
    monkeypatch.setattr(transcription, "settings_manager", InMemorySettings(
        {SettingsKey.AUTO_PASTE: False, SettingsKey.COPY_CLIPBOARD: True}))
    monkeypatch.setattr(transcription, "send_paste", paste)
    monkeypatch.setattr(transcription, "is_accessibility_trusted", lambda: True)
    controller = SimpleNamespace(
        ui_controller=ui,
        recorder=SimpleNamespace(is_recording=False),
        status_update=Signal(),
        current_backend=None,
    )
    return SimpleNamespace(ui=ui, paste=paste, controller=controller,
                           runtime=TranscriptionRuntime(controller))


def test_the_answering_job_may_paste_while_it_still_holds_the_slot(h):
    job = DictationJob(mode=JobMode.ACTION, action_id="a1")
    assert h.runtime._claim_job(job)

    assert h.runtime.paste_text_now("the answer", job=job)
    assert h.ui.stages == ["the answer"]
    assert h.paste.call_count == 1


def test_a_routed_copy_of_the_job_counts_as_the_same_job(h):
    job = DictationJob(mode=JobMode.DICTATION)
    assert h.runtime._claim_job(job)
    routed = DictationJob(mode=JobMode.ACTION, action_id="a1", id=job.id, routed=True)

    assert h.runtime.paste_text_now("the answer", job=routed)


def test_another_job_or_a_recording_still_refuses_the_paste(h):
    assert h.runtime._claim_job(DictationJob(mode=JobMode.DICTATION))

    assert not h.runtime.paste_text_now("the answer", job=DictationJob(mode=JobMode.ACTION))
    assert not h.runtime.paste_text_now("the answer")
    assert h.ui.stages == []
    assert h.ui.notices[-1].startswith("A transcription is already in progress")

    h.runtime._finish_job()
    h.controller.recorder.is_recording = True
    job = DictationJob(mode=JobMode.ACTION)
    assert not h.runtime.paste_text_now("the answer", job=job)
    assert h.paste.call_count == 0


def test_without_an_active_job_any_caller_pastes(h):
    assert h.runtime.paste_text_now("words")
    assert h.runtime.paste_text_now("words", job=DictationJob(mode=JobMode.ACTION))
    assert h.paste.call_count == 2
