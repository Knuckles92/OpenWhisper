"""Spoken routing's status copy (services.actions.routing.status)."""

from __future__ import annotations

import pytest

from services import credentials, typesafe
from services.actions import routing

ACTION = {"id": "c1", "name": "Ask Claude", "kind": "clipboard", "spoken_names": ["Claude"]}


@pytest.fixture(autouse=True)
def no_env_keys(monkeypatch):
    for name in (typesafe.CREDENTIAL_ENV, typesafe.OPENROUTER_ROUTE.credential_env):
        monkeypatch.delenv(name, raising=False)


def settings(**overrides):
    base = {"voice_routing_enabled": True, "typesafe_enabled": True, "actions": [ACTION]}
    base.update(overrides)
    return base


def with_key(route=typesafe.TYPESAFE_ROUTE):
    credentials.store().set(route.credential_env, "test-key")


def test_off():
    status = routing.status(settings(voice_routing_enabled=False))
    assert status == routing.RoutingStatus(
        False, False, "Spoken routing is off. Turn it on in Settings → Actions.")
    assert routing.status({}).enabled is False


def test_on_but_fast_judgments_are_off():
    with_key()
    assert routing.status(settings(typesafe_enabled=False)) == routing.RoutingStatus(
        True, False, "Spoken routing needs Fast judgments. Turn them on in "
                     "Settings → Meeting Mode → Fast judgments.")


@pytest.mark.parametrize("provider, phrase", [
    ("typesafe", "a TypeSafe API key"),
    ("openrouter", "an OpenRouter API key"),
])
def test_on_but_the_chosen_routes_key_is_missing(provider, phrase):
    # The other route's key doesn't count: routing goes where Fast judgments says.
    other = typesafe.OPENROUTER_ROUTE if provider == "typesafe" else typesafe.TYPESAFE_ROUTE
    with_key(other)
    assert routing.status(settings(typesafe_provider=provider)) == routing.RoutingStatus(
        True, False, f"Spoken routing needs {phrase}. Add one in "
                     "Settings → Meeting Mode → Fast judgments.")


def test_on_but_no_actions():
    with_key()
    assert routing.status(settings(actions=[])) == routing.RoutingStatus(
        True, False, "Spoken routing is on, but there are no actions yet. Add one in "
                     "Settings → Actions.")


def test_on_but_every_action_is_off():
    with_key()
    assert routing.status(settings(actions=[{**ACTION, "enabled": False}])) == (
        routing.RoutingStatus(True, False, "Spoken routing is on, but every action is turned "
                                           "off. Turn one on in Settings → Actions."))


def test_ready_says_what_is_sent_and_where():
    with_key()
    assert routing.status(settings()) == routing.RoutingStatus(
        True, True, "Spoken routing is on. What you record with Route by voice is sent to "
                    "TypeSafe to choose the action.")
    with_key(typesafe.OPENROUTER_ROUTE)
    status = routing.status(settings(typesafe_provider="openrouter",
                                     voice_routing_on_dictation=True))
    assert status == routing.RoutingStatus(
        True, True, "Spoken routing is on. What you record with Route by voice, and "
                    "dictations that start with an action's name, are sent to OpenRouter "
                    "to choose the action.")


def test_status_never_raises(monkeypatch):
    def broken(_settings):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(routing, "load_actions", broken)
    with_key()
    assert routing.status(settings()) == routing.RoutingStatus(
        False, False, "Spoken routing isn't available right now.")
    assert routing.status(None).enabled is False
