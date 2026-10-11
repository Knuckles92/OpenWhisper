"""Spoken routing's decision: one Jev request, the threshold, failures and events."""

from __future__ import annotations

import json
import logging

import pytest

from services import credentials, typesafe
from services.actions import routing
from services.automation import catalog, events
from services.automation.events import EventBus
from services.dictation_pipeline import DictationJob, JobMode

CLAUDE = {"id": "c1", "name": "Ask Claude", "kind": "open_link",
          "params": {"template": "claude://claude.ai/new?q={text}"},
          "spoken_names": ["Claude"], "description": "Open a new Claude chat with my words."}
TODO = {"id": "t1", "name": "Todo", "kind": "webhook",
        "params": {"url": "https://example.test/hook?token=secret"},
        "spoken_names": ["to-do list"], "description": "Add an item to my todo list."}
CODEX = {"id": "x1", "name": "Codex", "kind": "agent", "params": {"agent": "codex"},
         "enabled": False}

ASKED = "Claude, what's two plus two?"
AFTER_NAME = "what's two plus two?"


def make_settings(**overrides):
    settings = {
        "actions": [CLAUDE, TODO, CODEX],
        "voice_routing_enabled": True,
        "voice_routing_on_dictation": True,
        "typesafe_enabled": True,
    }
    settings.update(overrides)
    return settings


def make_job(route=routing.ROUTE_AUTO):
    return DictationJob(mode=JobMode.DICTATION, route=route, source="hotkey")


class FakeJudge:
    """TypeSafeJudge-like: records each request and answers with ``answer``."""

    def __init__(self, answer=None, *, raises=None):
        self.answer = answer
        self.raises = raises
        self.calls = []

    def ask(self, state, questions, timeout_s=None):
        self.calls.append({"state": state, "questions": questions, "timeout_s": timeout_s})
        if self.raises is not None:
            raise self.raises
        return self.answer(state, questions) if callable(self.answer) else self.answer


def answering(choice, confidence, *, payload=None):
    """Answers choosing ``choice`` for the route and ``payload`` (a span) for the words."""
    def answer(state, questions):
        options = list(questions["route"]["criteria"])
        rest = (1 - confidence) / max(1, len(options) - 1)
        answers = {"route": {
            "type": "choice", "choice": choice, "confidence": confidence,
            "probabilities": {k: (confidence if k == choice else rest) for k in options},
        }}
        if "payload" in questions:
            spans = list(questions["payload"]["criteria"])
            picked = payload if payload in spans else spans[0]
            answers["payload"] = {
                "type": "choice", "choice": picked, "confidence": 0.9,
                "probabilities": {k: (0.9 if k == picked else 0.1 / (len(spans) - 1))
                                  for k in spans},
            }
        return answers
    return answer


@pytest.fixture
def bus():
    replacement = EventBus()
    previous = events.set_bus(replacement)
    try:
        yield replacement
    finally:
        events.set_bus(previous)
        replacement.close()


def decided(bus):
    return [e.data for e in bus.recent(types=[catalog.ROUTE_DECIDED])]


# -- when the judge is asked ------------------------------------------------------

def test_off_unless_spoken_routing_is_on(bus):
    judge = FakeJudge(answering("c1", 0.99))
    for job in (make_job(), make_job(routing.ROUTE_HOTKEY)):
        assert routing.decide(ASKED, job, make_settings(voice_routing_enabled=False),
                              judge=judge) is None
    assert judge.calls == [] and decided(bus) == []


def test_plain_dictation_routes_only_while_routing_on_dictation_is_on(bus):
    judge = FakeJudge(answering("c1", 0.99))
    assert routing.decide(ASKED, make_job(), make_settings(voice_routing_on_dictation=False),
                          judge=judge) is None
    assert routing.decide(ASKED, make_job(""), make_settings(), judge=judge) is None
    assert judge.calls == []


def test_plain_dictation_without_a_name_at_the_start_is_never_sent(bus):
    judge = FakeJudge(answering("c1", 0.99))
    for text in ("I told Claude yesterday that the plan changed.", "Codex, write a test.",
                 "Remind me to call the dentist.", "   "):
        assert routing.decide(text, make_job(), make_settings(), judge=judge) is None
    assert judge.calls == [] and decided(bus) == []


def test_plain_dictation_asks_once_about_the_named_action_with_both_questions(bus):
    judge = FakeJudge(answering("c1", 0.99))
    routing.decide(ASKED, make_job(), make_settings(), judge=judge)
    [call] = judge.calls
    assert call["timeout_s"] == routing.ROUTE_TIMEOUT_S
    assert call["state"] == {"dictation": ASKED}
    assert set(call["questions"]) == {"route", "payload"}
    route = call["questions"]["route"]
    assert route["type"] == "choice"
    assert list(route["criteria"]) == ["c1", routing.NONE]
    assert route["criteria"]["c1"] == {
        "name": "Ask Claude", "spoken_names": ["Claude"],
        "purpose": "Open a new Claude chat with my words.",
        "does": routing._kind_phrase(routing.load_actions(make_settings())[0]),
    }
    payload = call["questions"]["payload"]
    assert payload["type"] == "choice"
    assert list(payload["criteria"]) == [AFTER_NAME, ASKED, routing.NONE]
    assert payload["instructions"]["action_names"] == ["Ask Claude", "Claude"]


def test_route_by_voice_offers_every_enabled_action_named_ones_first(bus):
    judge = FakeJudge(answering("t1", 0.99))
    routing.decide("Buy oat milk, add it to my to-do list.", make_job(routing.ROUTE_HOTKEY),
                   make_settings(), judge=judge)
    [call] = judge.calls
    assert list(call["questions"]["route"]["criteria"]) == ["t1", "c1", routing.NONE]
    assert list(call["questions"]["payload"]["criteria"]) == [
        "Buy oat milk", "Buy oat milk, add it to my to-do list.", routing.NONE]


def test_route_by_voice_without_a_name_asks_only_which_action(bus):
    judge = FakeJudge(answering("t1", 0.9))
    decision = routing.decide("Remind me to call the dentist.", make_job(routing.ROUTE_HOTKEY),
                              make_settings(), judge=judge)
    [call] = judge.calls
    assert set(call["questions"]) == {"route"}
    assert list(call["questions"]["route"]["criteria"]) == ["c1", "t1", routing.NONE]
    assert decision == routing.RouteDecision("t1", "Remind me to call the dentist.", 0.9)


def test_action_settings_never_reach_the_judge(bus):
    judge = FakeJudge(answering("t1", 0.9))
    routing.decide("Remind me to call the dentist.", make_job(routing.ROUTE_HOTKEY),
                   make_settings(), judge=judge)
    sent = json.dumps(judge.calls)
    for private in ("example.test", "token=secret", "claude://", "x1", "Codex"):
        assert private not in sent


# -- the threshold, failures and the payload ---------------------------------------

def test_a_confident_choice_routes_the_words_meant_for_the_action(bus):
    judge = FakeJudge(answering("c1", 0.93, payload=AFTER_NAME))
    job = make_job()
    decision = routing.decide(ASKED, job, make_settings(), judge=judge)
    assert decision == routing.RouteDecision("c1", "What's two plus two?", 0.93)
    assert decided(bus) == [{"job_id": job.id, "action_id": "c1", "confidence": 0.93,
                             "fallback": False}]


def test_the_threshold_is_inclusive(bus):
    judge = FakeJudge(answering("c1", routing.ROUTE_CONFIDENCE))
    assert routing.decide(ASKED, make_job(), make_settings(), judge=judge) is not None
    judge = FakeJudge(answering("c1", routing.ROUTE_CONFIDENCE - 0.01))
    job = make_job()
    assert routing.decide(ASKED, job, make_settings(), judge=judge) is None
    assert decided(bus)[-1] == {"job_id": job.id, "action_id": None,
                                "confidence": round(routing.ROUTE_CONFIDENCE - 0.01, 4),
                                "fallback": True}


def test_choosing_none_pastes(bus):
    judge = FakeJudge(answering(routing.NONE, 0.99))
    job = make_job()
    assert routing.decide("Claude said the deadline moved.", job, make_settings(),
                          judge=judge) is None
    assert decided(bus) == [{"job_id": job.id, "action_id": None, "confidence": 0.99,
                             "fallback": True}]


@pytest.mark.parametrize("judge", [
    FakeJudge(None),                                   # TypeSafeJudge's failure (timeout, HTTP)
    FakeJudge(raises=TimeoutError()),                  # a judge that raises instead
    FakeJudge({"route": {"choice": "gone", "confidence": 1.0}}),   # not a candidate
    FakeJudge({"payload": {"choice": routing.NONE}}),  # no route answer
], ids=["no-answer", "raises", "unknown-choice", "no-route"])
def test_a_failed_judgment_pastes_and_says_so(bus, judge):
    job = make_job()
    decision = routing.decide(ASKED, job, make_settings(), judge=judge)
    assert decision == routing.RouteDecision("", "", 0.0, failure="unreachable")
    assert len(judge.calls) == 1
    assert decided(bus) == [{"job_id": job.id, "action_id": None, "confidence": None,
                             "fallback": True}]


def test_none_of_these_spans_counts_for_the_whole_text(bus):
    def answer(state, questions):
        spans = list(questions["payload"]["criteria"])
        return {
            "route": {"choice": "c1", "confidence": 0.9, "probabilities": {"c1": 0.95}},
            "payload": {"choice": spans[0], "confidence": 0.2,
                        "probabilities": {spans[0]: 0.4, spans[1]: 0.3, routing.NONE: 0.3}},
        }
    decision = routing.decide(ASKED, make_job(), make_settings(), judge=FakeJudge(answer))
    assert decision.payload == ASKED


def test_without_a_payload_answer_the_whole_text_goes(bus):
    judge = FakeJudge({"route": {"choice": "c1", "confidence": 0.9}})
    decision = routing.decide(ASKED, make_job(), make_settings(), judge=judge)
    assert decision == routing.RouteDecision("c1", ASKED, 0.9)


def test_payload_without_probabilities_uses_the_choice(bus):
    judge = FakeJudge({"route": {"choice": "c1", "confidence": 0.9},
                       "payload": {"choice": AFTER_NAME}})
    assert routing.decide(ASKED, make_job(), make_settings(), judge=judge).payload == (
        "What's two plus two?")


def test_decide_never_raises(bus):
    judge = FakeJudge(answering("c1", 0.9))
    assert routing.decide(ASKED, None, make_settings(), judge=judge) is None
    assert routing.decide(ASKED, make_job(), {"actions": "garbage",
                                              "voice_routing_enabled": True,
                                              "voice_routing_on_dictation": True},
                          judge=judge) is None
    assert routing.decide(None, make_job(), make_settings(), judge=judge) is None
    assert routing.decide(ASKED, make_job(), None, judge=judge) is None


def test_the_words_are_never_logged(bus, caplog):
    caplog.set_level(logging.DEBUG, logger="services.actions.routing")
    routing.decide(ASKED, make_job(), make_settings(), judge=FakeJudge(answering("c1", 0.9)))
    routing.decide(ASKED, make_job(), make_settings(), judge=FakeJudge(raises=RuntimeError(ASKED)))
    assert caplog.records
    assert "two plus two" not in caplog.text


# -- request size --------------------------------------------------------------

def test_a_long_dictation_is_shown_by_its_start_and_end(bus):
    text = "Claude, " + "lorem ipsum dolor sit amet " * 800 + "the end."
    judge = FakeJudge(answering("c1", 0.9))
    decision = routing.decide(text, make_job(), make_settings(), judge=judge)
    [call] = judge.calls
    encoded = json.dumps({"state": call["state"], "questions": call["questions"]})
    assert len(encoded) < typesafe.MAX_STATE_CHARS
    assert call["state"]["dictation"].startswith("Claude, lorem")
    assert call["state"]["dictation"].endswith("the end.")
    assert decision.payload.endswith("the end.") and len(decision.payload) > 20_000


def test_fifty_wordy_actions_still_fit_one_request(bus):
    actions = [
        {"id": f"a{i}", "name": f"Action number {i}", "kind": "scripts",
         "spoken_names": [f"spoken {i} {w}" for w in "abcdefgh"],
         "description": "A long description of what this does. " * 7}
        for i in range(50)
    ]
    judge = FakeJudge(answering("a7", 0.9))
    routing.decide("Do the thing for me please.", make_job(routing.ROUTE_HOTKEY),
                   make_settings(actions=actions), judge=judge)
    [call] = judge.calls
    assert len(call["questions"]["route"]["criteria"]) == 51
    encoded = json.dumps({"state": call["state"], "questions": call["questions"]})
    assert len(encoded) <= routing._REQUEST_CHARS


# -- through the real client ---------------------------------------------------------

def test_the_request_passes_the_real_clients_validation(bus):
    """A TypeSafeJudge over a fake transport: the request and answers have the real shapes."""
    sent = []

    def transport(payload, timeout_s):
        sent.append((payload, timeout_s))
        questions = payload["questions"]
        return 200, json.dumps({"answers": answering("c1", 0.8, payload=AFTER_NAME)(
            payload["state"], questions), "usage": {"input_tokens": 700}})

    judge = typesafe.TypeSafeJudge("test-key", transport=transport)
    decision = routing.decide(ASKED, make_job(), make_settings(), judge=judge)
    assert decision == routing.RouteDecision("c1", "What's two plus two?", 0.8)
    [(payload, timeout_s)] = sent
    assert timeout_s == routing.ROUTE_TIMEOUT_S
    assert payload["model"] == typesafe.MODEL


@pytest.fixture
def no_env_keys(monkeypatch):
    for name in (typesafe.CREDENTIAL_ENV, typesafe.OPENROUTER_ROUTE.credential_env):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("provider", ["typesafe", "openrouter"])
def test_the_judge_follows_the_fast_judgments_route(bus, monkeypatch, no_env_keys, provider):
    route = typesafe.ROUTES[provider]
    credentials.store().set(route.credential_env, "test-key")
    posted = []

    def fake_post(payload, timeout_s, *, api_key, endpoint, extra_headers=None):
        posted.append((endpoint, payload["model"], timeout_s))
        return 200, json.dumps({"answers": answering("c1", 0.9)(
            payload["state"], payload["questions"])})

    monkeypatch.setattr(typesafe, "_http_post", fake_post)
    decision = routing.decide(ASKED, make_job(), make_settings(typesafe_provider=provider))
    assert decision is not None and decision.action_id == "c1"
    assert posted == [(route.endpoint, route.model, routing.ROUTE_TIMEOUT_S)]


def test_without_fast_judgments_nothing_is_sent(bus, monkeypatch, no_env_keys):
    credentials.store().set(typesafe.CREDENTIAL_ENV, "test-key")
    monkeypatch.setattr(typesafe, "_http_post", lambda *a, **k: pytest.fail("sent"))
    # Nothing is sent, and Route by voice can say the judge couldn't be asked.
    failed = routing.RouteDecision("", "", 0.0, failure="unreachable")
    assert routing.decide(ASKED, make_job(), make_settings(typesafe_enabled=False)) == failed
    credentials.store().delete(typesafe.CREDENTIAL_ENV)
    assert routing.decide(ASKED, make_job(), make_settings()) == failed
    assert [e["fallback"] for e in decided(bus)] == [True, True]
