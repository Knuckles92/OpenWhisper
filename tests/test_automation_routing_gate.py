"""Spoken routing's code gate and payload spans (services.actions.routing)."""

from __future__ import annotations

import pytest

from benchmarks.voice_routing.cases import ACTION_SETS, CASES
from services.actions import routing
from services.actions.model import Action

ASK_CLAUDE = Action("c1", "Ask Claude", "open_link", {"template": "claude://claude.ai/new?q={text}"},
                    spoken_names=("Claude", "hey Claude"))
CLAUDE_CODE = Action("cc", "Tell my Claude Code session", "channel", {},
                     spoken_names=("Claude Code",))
TODO = Action("t1", "Todo", "webhook", {"url": "https://example.test/todo"},
              spoken_names=("to-do list",))
SCRIPTS = Action("s1", "Send to my scripts", "scripts", {})
CHATGPT = Action("g1", "ChatGPT app", "open_link", {"template": "https://chatgpt.com/?q={text}"})
AGENT = Action("agent", "Agent hotkey", "agent", {"agent": "claude_code"})
ALL = (ASK_CLAUDE, CLAUDE_CODE, TODO, SCRIPTS, CHATGPT, AGENT)


def named(text, actions=ALL, route=routing.ROUTE_AUTO):
    return [m.action_id for m in routing.gate(text, actions, route=route)]


@pytest.mark.parametrize("text, expected", [
    ("Claude, what's the capital of Peru?", ["c1"]),
    ("Ask Claude what the capital of Peru is", ["c1"]),
    ("Hey Claude, um, what's a mutex?", ["c1"]),
    ("Um, okay so, hey Claude, what's a mutex?", ["c1"]),
    ("All right Claude, draft a reply", ["c1"]),
    ("Can you ask Claude how to center a div", ["c1"]),
    ("Send this to my scripts: rotate the logs", ["s1"]),
    ("Send to my scripts rotate the logs", ["s1"]),
    ("Todo: buy milk", ["t1"]),
    ("To-do list, call the plumber", ["t1"]),
    ("Agent, add a dark mode toggle", ["agent"]),
    ("ChatGPT, translate this", ["g1"]),
])
def test_names_at_the_start_after_fillers_and_lead_ins_pass(text, expected):
    assert named(text) == expected


def test_a_name_may_drop_its_verb_and_generic_words():
    # "Tell my Claude Code session" is named by "Claude Code"; "Ask Claude" by "Claude";
    # "Agent hotkey" by "agent"; "ChatGPT app" by "ChatGPT".
    assert named("Claude Code, run the tests") == ["cc", "c1"]
    claude_only = Action("x", "Claude", "open_link", {"template": "claude://x?q={text}"})
    assert named("Ask Claude to summarize this", [claude_only]) == ["x"]


@pytest.mark.parametrize("text, expected", [
    ("chat GPT, give me a packing list", ["g1"]),   # speech recognition splits a word
    ("To do, email the landlord", ["t1"]),
    ("Cloud, what time zone is Tokyo in?", ["c1"]),  # misheard names
    ("Claud, what's the boiling point?", ["c1"]),
    ("Hey clod, three lunch ideas", ["c1"]),
    ("Clawed code, run the linter", ["cc", "c1"]),
])
def test_speech_recognition_spacing_and_mishearings_pass(text, expected):
    assert named(text) == expected


@pytest.mark.parametrize("text", [
    "I told Claude yesterday that the plan changed.",   # not at the start
    "Today I went to the store.",                       # sounds like "todo", too short to tell
    "Claude's answer was wrong.",                       # a possessive, not an address
    "Agents are the hot topic this year.",
    "Hey team, Claude Code shipped a release.",         # "team" is not a lead-in
    "Please can you ask and tell the Claude team",      # more than four lead-ins
    "",
    "Um, uh.",
])
def test_ordinary_sentences_stop_at_the_gate(text):
    assert named(text) == []


def test_a_name_said_last_counts_only_with_route_by_voice():
    text = "Buy oat milk, add it to my to-do list."
    assert named(text) == []
    matches = routing.gate(text, ALL, route=routing.ROUTE_HOTKEY)
    assert [(m.action_id, m.leading) for m in matches] == [("t1", False)]


def test_longest_name_goes_first():
    matches = routing.gate("Ask Claude Code why the tests fail", ALL, route=routing.ROUTE_AUTO)
    assert [m.action_id for m in matches] == ["cc", "c1"]


def plan_spans(text, actions=ALL, route=routing.ROUTE_AUTO):
    plan = routing.plan(text, route, actions)
    return list(plan.spans.values()) if plan else None


def test_spans_drop_the_name_link_words_and_politeness_and_end_with_everything():
    text = "Hey Claude, um, can you explain recursion simply, please."
    assert plan_spans(text) == [
        "um, can you explain recursion simply, please.",
        "explain recursion simply, please.",
        "um, can you explain recursion simply.",
        "explain recursion simply.",
        text,
    ]


def test_spans_keep_the_words_verbatim():
    assert plan_spans("Send this to my scripts: deploy 'staging' now!") == [
        "deploy 'staging' now!",
        "Send this to my scripts: deploy 'staging' now!",
    ]


def test_a_name_said_last_proposes_the_words_before_it():
    spans = plan_spans("What's the weather in Lisbon? Ask Claude.", route=routing.ROUTE_HOTKEY)
    assert spans == ["What's the weather in Lisbon?", "What's the weather in Lisbon? Ask Claude."]


def test_each_named_action_proposes_its_own_remainder():
    spans = plan_spans("Ask Claude Code why the tests fail")
    assert spans == ["why the tests fail", "Code why the tests fail",
                     "Ask Claude Code why the tests fail"]


def test_nothing_to_strip_means_no_payload_question():
    plan = routing.plan("Remind me to call the dentist", routing.ROUTE_HOTKEY, ALL)
    assert plan.spans == {}
    assert set(plan.questions) == {"route"}


def test_the_name_alone_leaves_only_the_whole_text():
    plan = routing.plan("Claude.", routing.ROUTE_AUTO, ALL)
    assert plan.spans == {}
    assert set(plan.questions) == {"route"}


# -- the benchmark's labels agree with the gate ---------------------------------

def test_benchmark_is_large_enough_and_covers_both_route_modes():
    assert len(CASES) >= 80
    assert len({c.id for c in CASES}) == len(CASES)
    routes = {c.route for c in CASES}
    assert routes == {routing.ROUTE_AUTO, routing.ROUTE_HOTKEY}
    for tag in ("name", "spoken", "filler", "asr", "mention", "ambiguous", "trailing", "purpose"):
        assert any(tag in c.tags for c in CASES), tag
    for case in CASES:
        for alias in case.expect:
            assert alias is None or alias in ACTION_SETS[case.action_set]


@pytest.mark.parametrize("case", [c for c in CASES if c.route == routing.ROUTE_AUTO],
                         ids=lambda c: c.id)
def test_benchmark_plain_dictations_reach_the_judge_only_when_named(case):
    actions = [a for a in ACTION_SETS[case.action_set].values() if a.enabled]
    passed = routing.plan(case.text, case.route, actions) is not None
    meant_for_an_action = None not in case.expect
    if meant_for_an_action and "gate-miss" not in case.tags:
        assert passed, "a plain dictation addressed to an action must reach the judge"
    if not passed:
        assert not meant_for_an_action or "gate-miss" in case.tags
