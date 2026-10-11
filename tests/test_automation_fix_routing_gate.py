"""The routing gate doesn't hear ordinary words as action names (review: "could",
"called", "cold" passed as "Claude", "so to do that" as Todo, "Send the scripts
to Bob" and "Use the webhook from" as action names), and still hears names
said to an action, misheard or not (review round 2: "chat gpt what's …" and
"To do buy milk" without a pause, and "Tell Claude to …" for an action typed
"ask claude", had stopped passing)."""

from __future__ import annotations

import pytest

from benchmarks.voice_routing.cases import ACTION_SETS, CASES
from services.actions import routing
from services.actions.model import PRESETS, Action

#: The gallery presets as they come (names only), plus two common actions.
PRESET_ACTIONS = [Action(id=p.id, name=p.name, kind=p.kind, params=dict(p.params))
                  for p in PRESETS] + [
    Action(id="todo", name="Todo", kind="webhook", params={"url": "http://127.0.0.1/x"}),
    Action(id="notes", name="Notes", kind="webhook", params={"url": "http://127.0.0.1/x"}),
]
#: The same actions with their names typed in lowercase ("ask claude").
LOWERCASE_ACTIONS = [Action(id=a.id, name=a.name.lower(), kind=a.kind, params=dict(a.params))
                     for a in PRESET_ACTIONS]
BOTH_ROUTES = (routing.ROUTE_AUTO, routing.ROUTE_HOTKEY)


def named(text, actions=PRESET_ACTIONS, route=routing.ROUTE_AUTO):
    return [m.action_id for m in routing.gate(text, actions, route=route)]


@pytest.mark.parametrize("actions", [PRESET_ACTIONS, LOWERCASE_ACTIONS],
                         ids=["as-typed", "lowercase"])
@pytest.mark.parametrize("text", [
    "Could you send me the report by Friday?",
    "Could we move the meeting to three?",
    "Called the bank this morning, they said it's fine.",
    "Cold brew is cheaper if you make it at home.",
    "Child care pickup is at five today.",
    "Cloudy skies all week, bring a jacket.",
    "Clued in the team about the outage.",
    "Killed the stale jobs on the build box.",
    "Okay so could you take a look at the PR?",
    "So to do that we need a new index.",
    "Now to do this properly we should refactor first.",
    "To do that later we'd need a second server.",
    "To do it right we need more time.",
    "Chat about it with the team tomorrow.",
    "Send the scripts to Bob by Friday.",
    "Send my scripts to Bob by Friday.",
    "Use the webhook from the staging config.",
    "Use my webhook settings from staging.",
    "Open the app and click settings.",
])
def test_ordinary_sentences_from_the_review_stop_at_the_gate(text, actions):
    assert named(text, actions) == []
    assert routing.plan(text, routing.ROUTE_AUTO, actions) is None


@pytest.mark.parametrize("case", [c for c in CASES if "gate-stop" in c.tags], ids=lambda c: c.id)
def test_benchmark_sentences_that_must_be_pasted_never_reach_the_judge(case):
    actions = [a for a in ACTION_SETS[case.action_set].values() if a.enabled]
    assert routing.plan(case.text, case.route, actions) is None


@pytest.mark.parametrize("text, expected", [
    ("Cloud, what time zone is Tokyo in?", ["claude-chat"]),     # misheard at the start
    ("Ask cloud to translate good morning.", ["claude-chat"]),
    ("Hey clod, three lunch ideas", ["claude-chat"]),
    ("Claud, what's the boiling point?", ["claude-chat"]),
    ("Clawed code, run the linter", ["claude-code-session", "claude-chat"]),
    ("Tell Claude to summarize this thread.", ["claude-chat"]),   # a proper noun after lead-ins
    ("Can you ask ChatGPT for a packing list?", ["chatgpt-app"]),
    ("Send this to my scripts: rotate the logs.", ["scripts"]),   # the name's own words
    ("Send it to my scripts, rotate the logs.", ["scripts"]),
    ("Webhook, the build finished.", ["webhook"]),                # a generic name, said first
    ("Agent, add a dark mode toggle.", ["agent"]),
    ("To do, email the landlord.", ["todo"]),                     # a split name before a pause
    ("To-do: call the plumber.", ["todo"]),
    ("Todo buy milk", ["todo"]),
])
def test_names_said_to_an_action_still_pass(text, expected):
    assert named(text) == expected


@pytest.mark.parametrize("heard, name", [
    ("cloud", "claude"), ("clod", "claude"), ("claud", "claude"), ("clawed", "claude"),
    ("clawd", "claude"), ("codecs", "codex"),
])
def test_mishearings_sound_alike(heard, name):
    assert routing._alike(heard, name)


@pytest.mark.parametrize("heard, name", [
    ("could", "claude"), ("called", "claude"), ("cold", "claude"), ("child", "claude"),
    ("killed", "claude"), ("cloudy", "claude"), ("clued", "claude"), ("claudes", "claude"),
    ("today", "todo"), ("agents", "agent"), ("urgent", "agent"),
])
def test_other_words_do_not_sound_alike(heard, name):
    assert not routing._alike(heard, name)


@pytest.mark.parametrize("route", BOTH_ROUTES)
@pytest.mark.parametrize("text, expected", [
    ("chat gpt what's 15 percent of 80", ["chatgpt-app"]),
    ("Chat GPT what's 15% of 80", ["chatgpt-app"]),
    ("chat gpt can you translate this", ["chatgpt-app"]),
    ("To do buy milk", ["todo"]),
    ("To do email the landlord", ["todo"]),
    ("To do, email the landlord", ["todo"]),
    ("To do list, call the plumber", ["todo"]),
])
def test_a_name_heard_as_several_words_names_it_when_a_request_follows(text, expected, route):
    assert named(text, route=route) == expected
    assert named(text, LOWERCASE_ACTIONS, route) == expected


@pytest.mark.parametrize("text", [
    "So to do that we need a new index.",
    "To do that later we'd need a second server.",
    "To do it right we need more time.",
    "Now to do this properly we should refactor first.",
    "To do with the budget, ask finance.",
])
def test_a_name_heard_as_several_words_is_ordinary_speech_when_the_phrase_goes_on(text):
    assert named(text) == []
    assert named(text, route=routing.ROUTE_HOTKEY) == []


def test_a_pause_still_sets_off_a_name_heard_as_several_words():
    assert named("To do: that thing for the landlord") == ["todo"]
    hotkey = routing.ROUTE_HOTKEY
    assert named("Buy milk, add it to my to do", route=hotkey) == ["todo"]
    assert named("Buy milk, add it to my to do please", route=hotkey) == ["todo"]


# -- names typed in lowercase -----------------------------------------------------

@pytest.mark.parametrize("route", BOTH_ROUTES)
@pytest.mark.parametrize("text, expected", [
    ("Tell Claude to write a haiku.", ["claude-chat"]),
    ("Can you tell Claude what time it is?", ["claude-chat"]),
    ("Tell Claude Code to fix the failing test.", ["claude-code-session", "claude-chat"]),
    ("Ask my Claude Code session to run the tests.", ["claude-code-session", "claude-chat"]),
    ("Open ChatGPT and ask for a lasagna recipe.", ["chatgpt-app"]),
    ("Ask claude to summarize this.", ["claude-chat"]),       # the name as typed
    ("Send this to my scripts: rotate the logs.", ["scripts"]),
])
def test_a_name_typed_in_lowercase_is_heard_as_a_name(text, expected, route):
    """Whether a shortened name may follow lead-ins ("tell" isn't the name's
    "ask") comes from how it was heard, not only from how it was typed."""
    assert named(text, LOWERCASE_ACTIONS, route) == expected
    assert named(text, PRESET_ACTIONS, route) == expected


def test_a_common_noun_heard_in_lowercase_after_lead_ins_still_names_nothing():
    for actions in (PRESET_ACTIONS, LOWERCASE_ACTIONS):
        assert named("Send my scripts to Bob by Friday.", actions) == []
        assert named("Use agent mode for this one.", actions) == []
        assert named("Use my Webhook settings from staging.", actions) == []  # only generic


def test_words_are_written_as_names_by_their_capitals_not_a_sentence_start():
    words = routing._words("Tell Claude. Claude, ChatGPT and okay? Scripts")
    assert [(w.norm, w.name) for w in words] == [
        ("tell", False), ("claude", True), ("claude", False), ("chatgpt", True),
        ("and", False), ("okay", False), ("scripts", False),
    ]
    assert named("Use it. Scripts are in the shared drive.", LOWERCASE_ACTIONS) == []


def test_a_generic_name_counts_after_lead_ins_only_when_said_in_full():
    assert named("Use my webhook to post this.") == []
    hook = Action(id="h", name="Post to my webhook", kind="webhook",
                  params={"url": "http://127.0.0.1/x"})
    assert named("Can you post to my webhook: the build is green", [hook]) == ["h"]


def test_a_name_said_last_needs_lead_ins_or_a_pause_before_it():
    home = Action(id="home", name="Home Assistant", kind="webhook",
                  params={"url": "http://127.0.0.1/x"}, spoken_names=("home",))
    hotkey = routing.ROUTE_HOTKEY
    assert named("Lock the front door, home.", [home], hotkey) == ["home"]
    assert named("Lock the front door and head home.", [home], hotkey) == []
    assert named("Restart the webhook.", route=hotkey) == []
