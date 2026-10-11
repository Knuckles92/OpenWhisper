"""Real text filters and webhooks through the real transcription runtime.

The foundation's runtime harness (fake speech engine, uppercasing cleanup,
fake paste) with real HTTP filters and a real webhook on 127.0.0.1.
"""

from __future__ import annotations

import json
import time

import pytest

from services import settings as settings_module
from services.automation import signing, webhooks
from services.dictation_pipeline import DictationJob, JobMode
from services.runtime.transcription import FILTER_CANCELED_MESSAGE
from services.settings import SettingsKey
from tests import test_automation_foundation_pipeline as foundation
from tests.test_automation_hooks_webhooks import Answer, Receiver

# The foundation's harness fixtures, used here by name.
_qapp = foundation._qapp
h = foundation.h
_events = foundation._events


@pytest.fixture
def receiver():
    server = Receiver()
    yield server
    server.close()


def reply_text(transform):
    def reply(request):
        return json.dumps({"text": transform(request.json()["text"])}).encode()
    return reply


def add_filters(h, receiver, *rows):
    h.settings.values[SettingsKey.AUTOMATION_FILTERS] = [
        {"id": stage, "name": stage, "kind": "http", "stage": stage,
         "url": receiver.url(path), **extra}
        for stage, path, extra in rows
    ]


def dictate(h, mode=JobMode.DICTATION):
    job = DictationJob(mode=mode, action_id="a1" if mode == JobMode.ACTION else "")
    h.controller._pending_audio_path = h.audio
    assert h.runtime._claim_job(job)
    h.runtime.transcribe_audio_file(h.audio)
    return job


def test_filters_change_the_text_on_both_sides_of_cleanup(h, receiver):
    receiver.answer("/before", Answer(reply=reply_text(lambda t: t.replace("claude", "Claude Code"))))
    receiver.answer("/after", Answer(reply=reply_text(lambda t: t + " (via filter)")))
    add_filters(h, receiver, ("before_cleanup", "/before", {}), ("after_cleanup", "/after", {}))
    job = dictate(h)

    before, = receiver.at("/before")
    after, = receiver.at("/after")
    assert before.json()["text"] == "ask claude what time it is"
    assert after.json()["text"] == "ASK CLAUDE CODE WHAT TIME IT IS"
    assert before.json()["job_id"] == after.json()["job_id"] == job.id
    assert h.ui.stages == ["ASK CLAUDE CODE WHAT TIME IT IS (via filter)"]
    h.paste.assert_called_once_with()
    completed, = _events(h, "transcription.completed")
    assert completed.data["text"] == "ASK CLAUDE CODE WHAT TIME IT IS (via filter)"


def test_an_action_job_is_filtered_too(h, receiver):
    receiver.answer("/before", Answer(reply=reply_text(str.title)))
    add_filters(h, receiver, ("before_cleanup", "/before", {}))
    dictate(h, JobMode.ACTION)
    assert receiver.at("/before")[0].json()["mode"] == "action"
    assert h.controller.action_runtime.deliver.call_args[0][1] == "ASK CLAUDE WHAT TIME IT IS"


def test_a_filter_cancel_saves_the_words_and_pastes_nothing(h, receiver):
    receiver.answer("/after", Answer(body=b'{"cancel": true}'))
    add_filters(h, receiver, ("after_cleanup", "/after", {}))
    dictate(h)

    h.paste.assert_not_called()
    assert FILTER_CANCELED_MESSAGE in h.ui.statuses
    entry, = h.history.entries
    assert entry["text"] == "ASK CLAUDE WHAT TIME IT IS"


def test_a_stalled_filter_costs_its_time_limit_and_keeps_the_text(h, receiver):
    receiver.answer("/before", Answer(delay=10))
    add_filters(h, receiver, ("before_cleanup", "/before", {"timeout_ms": 400}))
    started = time.monotonic()
    dictate(h)

    assert time.monotonic() - started < 1.5
    assert h.ui.stages == ["ASK CLAUDE WHAT TIME IT IS"]
    failed, = _events(h, "filter.failed")
    assert failed.data == {"filter_id": "before_cleanup", "name": "before_cleanup",
                           "stage": "before_cleanup", "reason": "No answer within 0.4 s"}


def test_a_webhook_receives_the_completed_dictation(h, receiver, monkeypatch):
    monkeypatch.setattr(settings_module, "settings_manager", h.settings)
    h.settings.values[SettingsKey.AUTOMATION_WEBHOOKS] = [{
        "id": "w", "name": "Notes", "url": receiver.url("/hook"),
        "events": ["transcription.completed", "history.saved"], "enabled": True,
    }]
    webhooks.start()
    try:
        job = dictate(h)
        got = receiver.wait_for(2, "/hook")
    finally:
        webhooks.stop()
    secret = webhooks.ensure_secret("w")
    assert [r.json()["type"] for r in got] == ["transcription.completed", "history.saved"]
    assert all(signing.verify(secret, r.headers, r.body) for r in got)
    assert got[0].json()["data"]["text"] == "ASK CLAUDE WHAT TIME IT IS"
    assert got[0].json()["data"]["job_id"] == got[1].json()["data"]["job_id"] == job.id
