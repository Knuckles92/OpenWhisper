"""Offline tests for the shared TypeSafe client: validation, failure policy, settings gating."""
import json
from unittest.mock import Mock

import pytest

from services import typesafe
from services.typesafe import (
    ChoiceAnswer,
    TypeSafeError,
    TypeSafeJudge,
    validate_answers,
)

NOUL_Q = {"q": {"type": "noul", "instructions": "Is it?"}}
CHOICE_Q = {"q": {"type": "choice", "instructions": "Which?", "criteria": {"a": "A", "b": "B"}}}
SCORE_Q = {"q": {"type": "score", "instructions": "How?", "criteria": ["low", "mid", "high"]}}


class RecordingTransport:
    def __init__(self, status=200, body=None, exc=None):
        self.status = status
        self.body = body
        self.exc = exc
        self.calls = []

    def __call__(self, payload, timeout_s):
        self.calls.append((payload, timeout_s))
        if self.exc is not None:
            raise self.exc
        return self.status, self.body if isinstance(self.body, str) else json.dumps(self.body)


class TestValidateAnswers:
    def test_accepts_each_primitive(self):
        assert validate_answers({"answers": {"q": {"type": "noul", "noul": 0.25}}}, NOUL_Q)["q"]["noul"] == 0.25
        choice = validate_answers(
            {"answers": {"q": {"type": "choice", "choice": "b", "confidence": 0.7,
                                "probabilities": {"a": 0.3, "b": 0.7}}}}, CHOICE_Q)["q"]
        assert choice["choice"] == "b" and choice["probabilities"]["b"] == 0.7
        score = validate_answers({"answers": {"q": {"type": "score", "score": 1.4, "confidence": 0.5}}}, SCORE_Q)["q"]
        assert score["score"] == 1.4

    @pytest.mark.parametrize("body", [
        {"answers": {"other": {"type": "noul", "noul": 0.5}}},          # wrong id
        {"answers": {"q": {"type": "choice", "choice": "a"}}},         # wrong type
        {"answers": {"q": {"type": "noul", "noul": 1.5}}},             # out of range
        {"no_answers": True},
    ])
    def test_rejects_malformed(self, body):
        with pytest.raises(TypeSafeError):
            validate_answers(body, NOUL_Q)

    def test_rejects_unknown_choice_and_bad_score(self):
        with pytest.raises(TypeSafeError):
            validate_answers({"answers": {"q": {"type": "choice", "choice": "zzz", "confidence": 0.9}}}, CHOICE_Q)
        with pytest.raises(TypeSafeError):
            validate_answers({"answers": {"q": {"type": "score", "score": 7}}}, SCORE_Q)


class TestJudge:
    def test_requires_key(self):
        with pytest.raises(ValueError):
            TypeSafeJudge("")

    def test_ask_returns_validated_answers_and_records_usage(self):
        transport = RecordingTransport(body={"answers": {"q": {"type": "noul", "noul": 0.9}},
                                             "usage": {"input_tokens": 321}})
        judge = TypeSafeJudge("k", transport=transport, timeout_s=2.5)
        assert judge.noul({"text": "hi"}, "Is it?") == 0.9
        payload, timeout = transport.calls[0]
        assert payload["model"] == typesafe.MODEL
        assert payload["state"] == {"text": "hi"}
        assert timeout == 2.5
        assert judge.usage.requests == 1 and judge.usage.failures == 0
        assert judge.usage.input_tokens == 321

    def test_choice_convenience(self):
        transport = RecordingTransport(body={"answers": {"q": {
            "type": "choice", "choice": "a", "confidence": 0.8, "probabilities": {"a": 0.9, "b": 0.1}}}})
        judge = TypeSafeJudge("k", transport=transport)
        answer = judge.choice("state", "Which?", {"a": "A", "b": "B"})
        assert answer == ChoiceAnswer("a", 0.8, {"a": 0.9, "b": 0.1})

    @pytest.mark.parametrize("transport", [
        RecordingTransport(status=429, body=""),
        RecordingTransport(status=200, body="not json"),
        RecordingTransport(status=200, body={"answers": {"q": {"type": "noul", "noul": 3}}}),
        RecordingTransport(exc=TimeoutError("slow")),
    ])
    def test_failures_return_none_and_count(self, transport):
        judge = TypeSafeJudge("k", transport=transport)
        assert judge.noul("x", "Is it?") is None
        assert judge.usage.failures == 1

    def test_oversized_state_is_refused_before_transport(self):
        transport = RecordingTransport(body={"answers": {"q": {"type": "noul", "noul": 0.1}}})
        judge = TypeSafeJudge("k", transport=transport)
        assert judge.noul("x" * (typesafe.MAX_STATE_CHARS + 1), "Is it?") is None
        assert transport.calls == []

    def test_empty_questions_short_circuit(self):
        transport = RecordingTransport(body={"answers": {}})
        assert TypeSafeJudge("k", transport=transport).ask("x", {}) is None
        assert transport.calls == []

    def test_warning_rate_limited(self, caplog):
        clock = [0.0]
        transport = RecordingTransport(status=500, body="")
        judge = TypeSafeJudge("k", transport=transport, monotonic=lambda: clock[0])
        with caplog.at_level("WARNING", logger="services.typesafe"):
            judge.noul("x", "a")
            clock[0] = 1.0
            judge.noul("x", "a")
        assert sum(1 for r in caplog.records if r.levelname == "WARNING") == 1


class TestSettingsGating:
    def test_disabled_setting_yields_no_judge(self, monkeypatch):
        monkeypatch.setattr(typesafe, "resolve_credential", lambda name: "key")
        assert typesafe.judge_from_settings({"typesafe_enabled": False}) is None
        assert typesafe.is_configured({"typesafe_enabled": False}) is False

    def test_enabled_without_key_yields_no_judge(self, monkeypatch):
        monkeypatch.setattr(typesafe, "resolve_credential", lambda name: None)
        assert typesafe.judge_from_settings({"typesafe_enabled": True}) is None
        assert typesafe.is_configured({"typesafe_enabled": True}) is False

    def test_enabled_with_key_builds_judge(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(typesafe, "resolve_credential", lambda name: seen.setdefault("name", name) and "key")
        judge = typesafe.judge_from_settings({"typesafe_enabled": True}, timeout_s=1.0)
        assert isinstance(judge, TypeSafeJudge)
        assert seen["name"] == typesafe.CREDENTIAL_ENV
        assert judge.timeout_s == 1.0


GOOD_VERIFY_BODY = {"answers": {"q": {"type": "noul", "noul": 1.0}}}


class TestKeyPresence:
    def test_reports_whether_a_key_resolves(self, monkeypatch):
        monkeypatch.setattr(typesafe, "resolve_credential", lambda name: None)
        assert typesafe.key_present() is False
        monkeypatch.setattr(typesafe, "resolve_credential", lambda name: "key")
        assert typesafe.key_present() is True

    def test_is_configured_still_needs_the_master_switch(self, monkeypatch):
        monkeypatch.setattr(typesafe, "resolve_credential", lambda name: "key")
        assert typesafe.key_present() is True
        assert typesafe.is_configured({"typesafe_enabled": False}) is False


class TestVerifyKey:
    """Verification must report failures that ``ask`` deliberately swallows."""

    def test_accepts_a_working_key(self):
        transport = RecordingTransport(body=GOOD_VERIFY_BODY)
        ok, detail = typesafe.verify_key("k", transport=transport)
        assert ok is True
        assert typesafe.MODEL in detail
        payload = transport.calls[0][0]
        assert payload["model"] == typesafe.MODEL
        assert payload["state"] == {"text": "ok"}
        assert set(payload["questions"]) == {"q"}

    def test_blank_key_is_not_sent(self):
        transport = RecordingTransport(body=GOOD_VERIFY_BODY)
        ok, detail = typesafe.verify_key("   ", transport=transport)
        assert ok is False
        assert detail == "Paste a key first."
        assert transport.calls == []

    @pytest.mark.parametrize("status,fragment", [
        (401, "rejected the key"),
        (403, "denied access"),
        (500, "HTTP 500"),
    ])
    def test_reports_the_status_class(self, status, fragment):
        transport = RecordingTransport(status=status, body={})
        ok, detail = typesafe.verify_key("k", transport=transport)
        assert ok is False
        assert fragment in detail

    def test_rate_limiting_still_proves_the_key(self):
        # 429 is only reachable after authentication, so "failed" would mislead.
        ok, detail = typesafe.verify_key("k", transport=RecordingTransport(status=429, body={}))
        assert ok is True
        assert "429" in detail

    def test_rejects_a_200_that_does_not_answer(self):
        transport = RecordingTransport(body={"answers": {"other": {"type": "noul", "noul": 1.0}}})
        ok, detail = typesafe.verify_key("k", transport=transport)
        assert ok is False
        assert "answered oddly" in detail

    def test_network_failure_names_the_host_not_the_key(self):
        transport = RecordingTransport(exc=OSError("unreachable"))
        ok, detail = typesafe.verify_key("secret-key", transport=transport)
        assert ok is False
        assert typesafe.VERIFY_HOST in detail
        assert "secret-key" not in detail

    def test_uses_a_longer_timeout_than_a_live_judgment(self):
        transport = RecordingTransport(body=GOOD_VERIFY_BODY)
        typesafe.verify_key("k", transport=transport)
        assert transport.calls[0][1] == typesafe.VERIFY_TIMEOUT_S
        assert typesafe.VERIFY_TIMEOUT_S > typesafe.DEFAULT_TIMEOUT_S


class TestOpenRouterRoute:
    """The same model through OpenRouter: only endpoint, key, model id and a header differ."""

    def test_route_reuses_the_openrouter_text_profile(self):
        from services.text_llm import OPENROUTER_PROFILE_ID, builtin_profile

        profile = builtin_profile(OPENROUTER_PROFILE_ID)
        route = typesafe.OPENROUTER_ROUTE
        assert route.endpoint == profile.base_url.rstrip("/") + "/systemone"
        assert route.credential_env == profile.api_key_env == "OPENROUTER_API_KEY"
        assert route.headers == {"X-Title": "OpenWhisper"}
        assert route.host == "openrouter.ai"

    def test_model_is_openrouters_id_for_the_pinned_release(self):
        # Prefixing the pinned id would ask for typesafe/jev-1.13.0, which
        # OpenRouter answers with HTTP 400 (checked live, October 6, 2026).
        assert typesafe.MODEL == "jev-1.13.0"
        assert typesafe.OPENROUTER_ROUTE.model == "typesafe/jev-1.13"

    def test_judge_posts_to_openrouter_with_its_key_model_and_header(self, monkeypatch):
        post = Mock(return_value=(200, json.dumps({
            "id": "gen-dec-1", "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
            "answers": {"q": {"type": "noul", "noul": 0.98}},
            "usage": {"input_tokens": 275, "output_tokens": 20, "cost": 0.00003}})))
        monkeypatch.setattr(typesafe, "_http_post", post)
        judge = TypeSafeJudge("or-key", route=typesafe.OPENROUTER_ROUTE)
        assert judge.noul("I was charged twice.", "Is a refund requested?") == 0.98
        payload, _timeout = post.call_args.args
        assert payload["model"] == "typesafe/jev-1.13"
        assert post.call_args.kwargs == {
            "api_key": "or-key", "endpoint": typesafe.OPENROUTER_ROUTE.endpoint,
            "extra_headers": {"X-Title": "OpenWhisper"},
        }
        assert judge.usage.input_tokens == 275

    def test_extra_headers_never_replace_authorization(self, monkeypatch):
        sent = {}

        def urlopen_post(endpoint, body, headers, timeout_s):
            sent.update(headers)
            return 200, json.dumps({"answers": {"q": {"type": "noul", "noul": 0.5}}})

        monkeypatch.setattr(typesafe, "_proxied", lambda parts: True)
        monkeypatch.setattr(typesafe, "_urlopen_post", urlopen_post)
        typesafe._http_post({"model": "m"}, 1.0, api_key="real", endpoint="https://example.test/x",
                            extra_headers={"Authorization": "Bearer spoofed", "X-Title": "OpenWhisper"})
        assert sent["Authorization"] == "Bearer real"
        assert sent["X-Title"] == "OpenWhisper"


class TestRouteFromSettings:
    def test_defaults_to_typesafe_and_ignores_unknown_values(self):
        assert typesafe.route_from_settings({}) is typesafe.TYPESAFE_ROUTE
        assert typesafe.route_from_settings({"typesafe_provider": "bogus"}) is typesafe.TYPESAFE_ROUTE
        assert typesafe.route_from_settings(
            {"typesafe_provider": "openrouter"}) is typesafe.OPENROUTER_ROUTE

    def test_judge_uses_the_chosen_routes_credential(self, monkeypatch):
        keys = {"OPENROUTER_API_KEY": "or-key"}
        monkeypatch.setattr(typesafe, "resolve_credential", keys.get)
        settings = {"typesafe_enabled": True, "typesafe_provider": "openrouter"}
        judge = typesafe.judge_from_settings(settings)
        assert judge.route is typesafe.OPENROUTER_ROUTE
        assert judge.endpoint == typesafe.OPENROUTER_ROUTE.endpoint
        # The TypeSafe route has no key here, so nothing would run there.
        assert typesafe.judge_from_settings({"typesafe_enabled": True}) is None

    def test_key_presence_follows_the_route(self, monkeypatch):
        monkeypatch.setattr(typesafe, "resolve_credential", {"OPENROUTER_API_KEY": "k"}.get)
        assert typesafe.key_present({"typesafe_provider": "openrouter"}) is True
        assert typesafe.key_present({"typesafe_provider": "typesafe"}) is False
        assert typesafe.key_present({}, route=typesafe.OPENROUTER_ROUTE) is True
        assert typesafe.is_configured({"typesafe_enabled": True, "typesafe_provider": "openrouter"})

    def test_same_connection_compares_destination_and_key(self):
        direct = TypeSafeJudge("k")
        assert direct.same_connection(TypeSafeJudge("k"))
        assert not direct.same_connection(TypeSafeJudge("k", route=typesafe.OPENROUTER_ROUTE))
        assert not direct.same_connection(TypeSafeJudge("other"))
        assert not direct.same_connection(object())


class TestFollowingJudge:
    def test_each_question_goes_to_the_judge_provided_at_that_moment(self):
        first = TypeSafeJudge("k", transport=RecordingTransport(body=GOOD_VERIFY_BODY))
        second = TypeSafeJudge("k", route=typesafe.OPENROUTER_ROUTE,
                               transport=RecordingTransport(body=GOOD_VERIFY_BODY))
        current = [first]
        judge = typesafe.FollowingJudge(lambda: current[0])
        assert judge.noul("x", "Is it?") == 1.0
        current[0] = second
        assert judge.noul("x", "Is it?") == 1.0
        current[0] = None
        assert judge.noul("x", "Is it?") is None
        assert len(first._transport.calls) == 1 and len(second._transport.calls) == 1
        assert second._transport.calls[0][0]["model"] == typesafe.OPENROUTER_ROUTE.model

    def test_timeout_is_passed_only_when_given_and_by_keyword(self):
        seen = []

        class Duck:
            def ask(self, state, questions, *, timeout_s=None):
                seen.append(timeout_s)
                return {}

        judge = typesafe.FollowingJudge(Duck)
        judge.ask("x", NOUL_Q)
        judge.ask("x", NOUL_Q, timeout_s=2.0)
        assert seen == [None, 2.0]


class TestVerifyOverOpenRouter:
    def test_reports_the_openrouter_host_and_model(self):
        transport = RecordingTransport(body=GOOD_VERIFY_BODY)
        ok, detail = typesafe.verify_key("k", route=typesafe.OPENROUTER_ROUTE, transport=transport)
        assert ok is True
        assert "openrouter.ai" in detail and "typesafe/jev-1.13" in detail
        assert transport.calls[0][0]["model"] == "typesafe/jev-1.13"

    def test_sends_the_route_header_and_endpoint(self, monkeypatch):
        post = Mock(return_value=(200, json.dumps(GOOD_VERIFY_BODY)))
        monkeypatch.setattr(typesafe, "_http_post", post)
        typesafe.verify_key("k", route=typesafe.OPENROUTER_ROUTE)
        assert post.call_args.kwargs["endpoint"] == typesafe.OPENROUTER_ROUTE.endpoint
        assert post.call_args.kwargs["extra_headers"] == {"X-Title": "OpenWhisper"}

    def test_out_of_credits_is_not_reported_as_a_bad_key(self):
        ok, detail = typesafe.verify_key(
            "k", route=typesafe.OPENROUTER_ROUTE, transport=RecordingTransport(status=402, body={}))
        assert ok is False
        assert "out of credits" in detail and "rejected" not in detail

    def test_no_route_points_at_the_account_settings(self):
        ok, detail = typesafe.verify_key(
            "k", route=typesafe.OPENROUTER_ROUTE, transport=RecordingTransport(status=404, body={}))
        assert ok is False
        assert "privacy settings" in detail
        # TypeSafe's own 404 keeps the plain status wording.
        ok, detail = typesafe.verify_key("k", transport=RecordingTransport(status=404, body={}))
        assert detail == "api.typesafe.ai answered HTTP 404."
