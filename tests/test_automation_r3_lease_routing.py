"""Review round 3, F2: leased before-cleanup filters run once routing has decided.

End to end through the real TranscriptionRuntime, real text filters (a lease
on a local receiver) and the real ActionRuntime; routing's judge is the only
fake. Then the stage's shared time budget, on the filters alone.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from services.automation import filters
from services.automation.channel_bridge import channel_params
from services.automation.models import BEFORE_CLEANUP
from services.dictation_pipeline import DictationJob, JobMode
from services.settings import SettingsKey
from tests import test_automation_hooks_filters as hooks
from tests import test_automation_r2_program_words_pipeline as words
from tests.test_automation_hooks_webhooks import Answer

_qapp = words._qapp
h = words.h
pipe = words.pipe
receiver = words.receiver
no_leases = words.no_leases
failures = hooks.failures


def capitalizes(request):
    """A harmless lease: it only capitalizes a name."""
    body = json.loads(request.body)
    return json.dumps({"text": body["text"].replace("claude", "Claude")}).encode()


def adds_please(request):
    body = json.loads(request.body)
    return json.dumps({"text": body["text"] + " please"}).encode()


def lease(receiver, reply):
    receiver.answer("/lease", Answer(reply=reply))
    filters.register_lease(receiver.url("/lease"), BEFORE_CLEANUP)


@pytest.mark.parametrize("route", ["hotkey", "auto"])
def test_a_lease_never_sees_the_users_words_routed_to_claude(pipe, receiver, monkeypatch, route):
    pipe.controller.current_backend.transcribe = lambda path: "claude, what time is it"
    lease(receiver, capitalizes)
    words.route_when_claude_is_named(monkeypatch)

    ran = words.run(pipe, DictationJob(mode=JobMode.DICTATION, source="hotkey", route=route))

    # Routed to the channel action, which takes no program's words without
    # "Send words to my coding agents": the lease never saw them.
    assert receiver.requests == []
    assert (ran["action_id"], ran["ok"]) == ("ch", True)
    (message,) = words.channel_messages(pipe)
    assert message.data["text"] == "WHAT TIME IS IT"
    assert channel_params(message.to_dict())["meta"]["origin"] == "dictated"
    assert pipe.copied == []


def test_with_the_permission_a_lease_sees_the_job_as_routed(pipe, receiver, monkeypatch):
    pipe.controller.current_backend.transcribe = lambda path: "claude, what time is it"
    pipe.settings.save_setting(SettingsKey.API_AGENT_PROMPTS, True)
    lease(receiver, adds_please)
    words.route_when_claude_is_named(monkeypatch)

    assert words.run(pipe, DictationJob(mode=JobMode.DICTATION, source="hotkey", route="hotkey"))["ok"]

    (request,) = [json.loads(r.body) for r in receiver.requests]
    assert (request["mode"], request["action_id"], request["text"]) == (
        "action", "ch", "what time is it")
    (message,) = words.channel_messages(pipe)
    assert message.data["text"] == "WHAT TIME IS IT PLEASE"
    assert message.data["origin"] == "program"


def test_the_users_saved_filters_still_run_before_routing(pipe, receiver, monkeypatch):
    # A saved filter fixing a mishearing decides where the words go, as before.
    pipe.controller.current_backend.transcribe = lambda path: "cloud, what time is it"
    receiver.answer("/mine", Answer(reply=lambda request: json.dumps(
        {"text": json.loads(request.body)["text"].replace("cloud", "claude")}).encode()))
    pipe.settings.save_setting(SettingsKey.AUTOMATION_FILTERS,
                               [hooks.http_filter("mine", receiver.url("/mine"))])
    lease(receiver, capitalizes)
    words.route_when_claude_is_named(monkeypatch)

    ran = words.run(pipe, DictationJob(mode=JobMode.DICTATION, source="hotkey", route="hotkey"))

    assert [r.path for r in receiver.requests] == ["/mine"]
    assert (ran["action_id"], ran["ok"]) == ("ch", True)
    (message,) = words.channel_messages(pipe)
    assert message.data["origin"] == "dictated"


# -- the two parts of a stage share its budget ---------------------------------------


@pytest.fixture
def stage(monkeypatch):
    """A stage whose filters answer "keep"; the user's saved one takes ``mine_s``."""
    state = SimpleNamespace(ran=[], mine_s=0.0)

    def run_step(step, body, timeout_s, cancel):
        state.ran.append(step.id)
        if not step.leased:
            time.sleep(state.mine_s)
        return filters._Verdict(filters._KEEP)

    monkeypatch.setattr(filters, "STAGE_BUDGET_S", 0.4)
    monkeypatch.setattr(filters, "_run_step", run_step)
    filters.register_lease("http://127.0.0.1:9/lease", BEFORE_CLEANUP)
    state.run = filters.StageRun(BEFORE_CLEANUP, hooks.settings_with(
        hooks.http_filter("mine", "http://127.0.0.1:9/mine"), leases_allowed=True))
    return state


def test_routing_between_the_parts_takes_none_of_the_filters_time(stage, failures):
    stage.run.saved(hooks.TEXT, hooks.job())
    time.sleep(0.5)  # routing
    stage.run.leased(hooks.TEXT, hooks.job())

    assert stage.ran[0] == "mine" and stage.ran[1].startswith("flt_")
    assert failures == []


def test_the_parts_share_the_stages_time(stage, failures):
    stage.mine_s = 0.38
    stage.run.saved(hooks.TEXT, hooks.job())
    stage.run.leased(hooks.TEXT, hooks.job())

    assert stage.ran == ["mine"]
    (failure,) = hooks.wait_for(failures, 1)
    assert failure.data["reason"] == "Skipped: this stage's 0.4 s for filters ran out"
