"""Offline tests for the shared TypeSafe client: validation, failure policy, settings gating."""
import json

import pytest

from services import typesafe
from services.typesafe import (
    ChoiceAnswer,
    TypeSafeError,
    TypeSafeJudge,
    sensitive_content_probability,
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


class TestSensitiveContent:
    def test_sends_text_field_and_truncates(self):
        transport = RecordingTransport(body={"answers": {"q": {"type": "noul", "noul": 0.97}}})
        judge = TypeSafeJudge("k", transport=transport)
        assert sensitive_content_probability(judge, "x" * 20_000) == 0.97
        state = transport.calls[0][0]["state"]
        assert set(state) == {"text"}
        assert len(state["text"]) == typesafe._SENSITIVE_MAX_CHARS

    def test_blank_text_is_not_sent(self):
        transport = RecordingTransport(body={})
        assert sensitive_content_probability(TypeSafeJudge("k", transport=transport), "   ") is None
        assert transport.calls == []
