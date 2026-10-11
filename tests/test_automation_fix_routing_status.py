"""Route by voice's status names what is missing, including a way to start it
(review: it said ready with no Route by voice shortcut and normal dictations
not listened to), and uses the feature's own name."""

from __future__ import annotations

import pytest

from services import typesafe
from services.actions import routing
from tests import test_automation_routing_status as base
from tests.test_automation_routing_status import ACTION, with_key

no_env_keys = base.no_env_keys

SHORTCUT = {"hotkeys": {"route_record": "ctrl+alt+r"}}


def settings(**overrides):
    values = {"voice_routing_enabled": True, "typesafe_enabled": True, "actions": [ACTION],
              **SHORTCUT}
    values.update(overrides)
    return values


def test_nothing_to_listen_to_is_not_ready():
    with_key()
    for nothing in ({"hotkeys": {}}, {"hotkeys": {"route_record": ""}}, {"hotkeys": None}):
        status = routing.status(settings(voice_routing_on_dictation=False, **nothing))
        assert status == routing.RoutingStatus(
            True, False,
            "Route by voice is on, but has nothing to listen to yet. Set a Route by voice "
            "shortcut below, or turn on Also listen to normal dictations.",
            fix="trigger")
    no_hotkeys = settings()
    del no_hotkeys["hotkeys"]
    assert routing.status(no_hotkeys).fix == "trigger"


def test_ready_says_exactly_what_is_sent():
    with_key()
    assert routing.status(settings()) == routing.RoutingStatus(
        True, True, "Route by voice is on. Everything you record with the Route by voice "
                    "shortcut is sent to TypeSafe to choose the action.")
    assert routing.status(settings(voice_routing_on_dictation=True)).message == (
        "Route by voice is on. Everything you record with the Route by voice shortcut, and "
        "dictations that start with an action's name, are sent to TypeSafe to choose the "
        "action.")
    assert routing.status(settings(voice_routing_on_dictation=True, hotkeys={})) == (
        routing.RoutingStatus(True, True, "Route by voice is on. Dictations that start with "
                                          "an action's name are sent to TypeSafe to choose "
                                          "the action."))


@pytest.mark.parametrize("overrides, has_key, fix", [
    ({"voice_routing_enabled": False}, True, ""),
    ({"typesafe_enabled": False}, True, "fast_judgments"),
    ({}, False, "fast_judgments"),
    ({"actions": []}, True, "actions"),
    ({"actions": [{**ACTION, "enabled": False}]}, True, "actions"),
    ({}, True, ""),
], ids=["off", "fast-judgments-off", "no-key", "no-actions", "actions-off", "ready"])
def test_fix_names_what_is_missing(overrides, has_key, fix):
    if has_key:
        with_key()
    assert routing.status(settings(**overrides)).fix == fix


def test_the_copy_uses_the_features_name_and_points_up_the_page():
    with_key()
    messages = [routing.status(settings(**o)).message for o in (
        {"voice_routing_enabled": False}, {"typesafe_enabled": False}, {"actions": []},
        {"actions": [{**ACTION, "enabled": False}]}, {"hotkeys": {}}, {})]
    assert all(m.startswith("Route by voice ") for m in messages)
    assert not any("Spoken routing" in m or "Settings → Actions" in m for m in messages)
    assert messages[0] == "Route by voice is off. Turn it on above."
    assert messages[2].endswith("Add one above.")
    assert messages[3].endswith("Turn one on above.")


def test_the_missing_key_names_the_chosen_route():
    with_key(typesafe.TYPESAFE_ROUTE)
    status = routing.status(settings(typesafe_provider="openrouter"))
    assert status.fix == "fast_judgments"
    assert status.message == ("Route by voice needs an OpenRouter API key. Add one in "
                              "Settings → Meeting Mode → Fast judgments.")
