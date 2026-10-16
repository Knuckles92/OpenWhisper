"""Fast judgments on the user's own text model, offline against a fake OpenAI-style client."""
import json
import threading
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meeting.agent.typesafe_signals import TOPIC_TIMEOUT_S, topic_shift_probability
from meeting.citation_verifier import CitationVerifier
from meeting.engine import MeetingEngine, MeetingEngineOptions
from meeting.insight_review import CONSENT, JudgeReviewer
from meeting.live_signals import LiveSignals
from meeting.semantic_search import LOCAL_SCOPE, SCOPE, search_history
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore
from meeting.voice_commands import VoiceCommandListener
from services import settings, text_judge, text_llm, typesafe
from services.actions import routing
from services.settings import DEFAULT_VOICE_COMMAND_NAMES, SettingsKey
from services.text_judge import TextModelJudge, confidence, verify_text_model
from services.text_llm import TextLLMProfile
from services.typesafe import JudgeRoute

LOCAL = TextLLMProfile("custom_box", "My server", "custom", "http://127.0.0.1:9000/v1", "")
HOSTED = TextLLMProfile("custom_cloud", "Cloud box", "custom", "https://llm.example.com/v1", "CLOUD_BOX_KEY")
NOUL_Q = {"q": {"type": "noul", "instructions": "Is it?"}}
CHOICE_Q = {"q": {"type": "choice", "instructions": "Which?", "criteria": {"a": "A", "b": None, "c": "C"}}}


def route_for(profile, model="judge-1"):
    return JudgeRoute(id=settings.TypeSafeProvider.TEXT_MODEL, name=profile.name,
                      endpoint=profile.base_url, model=model, credential_env=profile.api_key_env,
                      timeout_s=typesafe.TEXT_MODEL_TIMEOUT_S, profile=profile)


class FakeOpenAI:
    """Stands in for ``openai.OpenAI``: records requests and answers with ``reply(request)``."""

    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests = []
        self.timeouts = []
        self.reply = lambda request: "{}"
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        FakeOpenAI.instances.append(self)

    def with_options(self, *, timeout):
        self.timeouts.append(timeout)
        return self

    def close(self):
        pass

    def _create(self, **request):
        self.requests.append(request)
        content = self.reply(request)
        if isinstance(content, Exception):
            raise content
        message = SimpleNamespace(content=content, refusal=None, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason="stop")],
                               usage=SimpleNamespace(prompt_tokens=123))


@pytest.fixture
def client(monkeypatch):
    FakeOpenAI.instances.clear()
    monkeypatch.setattr(text_llm, "OpenAI", FakeOpenAI)
    monkeypatch.setattr(text_judge, "_CLIENTS", {})
    monkeypatch.setattr(text_judge, "_NO_SCHEMA", set())

    holder = SimpleNamespace(reply=lambda request: "{}")

    def get():
        return FakeOpenAI.instances[-1]

    # Every client built during the test answers with holder.reply.
    original = FakeOpenAI.__init__

    def init(self, **kwargs):
        original(self, **kwargs)
        self.reply = lambda request: holder.reply(request)

    monkeypatch.setattr(FakeOpenAI, "__init__", init)
    holder.get = get
    return holder


def asked(request):
    """The JSON payload the model was given."""
    return json.loads(request["messages"][-1]["content"])


def judge(profile=LOCAL, **kwargs):
    return TextModelJudge(text_llm.AUTH_FREE_API_KEY, route=route_for(profile), **kwargs)


# -- primitives ------------------------------------------------------------------

class TestPrimitives:
    def test_noul_is_the_stated_probability(self, client):
        client.reply = lambda request: json.dumps({"q": 0.83})
        j = judge()
        assert j.noul({"text": "hi"}, "Is it?") == 0.83
        request = client.get().requests[0]
        assert request["temperature"] == 0
        assert request["model"] == "judge-1"
        assert asked(request) == {"state": {"text": "hi"},
                                  "questions": {"q": {"answer": "yes/no", "question": "Is it?"}}}
        schema = request["response_format"]["json_schema"]["schema"]
        assert schema["properties"] == {"q": {"type": "number"}} and schema["required"] == ["q"]
        assert j.usage.requests == 1 and j.usage.failures == 0 and j.usage.input_tokens == 123

    def test_choice_numbers_options_and_scales_confidence_like_jev(self, client):
        client.reply = lambda request: json.dumps({"q": {"option": "2", "probability": 0.9}})
        answer = judge().choice("state", "Which?", CHOICE_Q["q"]["criteria"])
        assert answer.choice == "b"
        # (p - 1/n) / (1 - 1/n) with three options.
        assert answer.confidence == pytest.approx((0.9 - 1 / 3) / (1 - 1 / 3))
        assert answer.probabilities == {}
        shown = asked(client.get().requests[0])["questions"]["q"]
        assert shown["options"] == {"1": {"name": "a", "means": "A"}, "2": {"name": "b"},
                                    "3": {"name": "c", "means": "C"}}
        enum = client.get().requests[0]["response_format"]["json_schema"]["schema"]["properties"]["q"]
        assert enum["properties"]["option"]["enum"] == ["1", "2", "3"]

    def test_confidence_formula(self):
        assert confidence(1.0, 4) == 1.0
        assert confidence(0.25, 4) == 0.0
        assert confidence(0.1, 4) == 0.0  # below an even split
        assert confidence(0.7, 1) == 0.7

    @pytest.mark.parametrize("reply", [
        "not json at all",
        "[0.5]",
        json.dumps({"other": 0.5}),                              # missing the asked id
        json.dumps({"q": 1.5}),                                  # impossible probability
        json.dumps({"q": True}),
        json.dumps({"q": "0.5"}),
    ])
    def test_malformed_noul_replies_are_no_judgment(self, client, reply):
        client.reply = lambda request: reply
        j = judge()
        assert j.noul("x", "Is it?") is None
        assert j.usage.failures == 1

    @pytest.mark.parametrize("value", [
        {"option": "9", "probability": 0.9},                     # unknown option
        {"option": "1", "probability": 2},
        {"option": "1"},
        "1",
    ])
    def test_malformed_choice_replies_are_no_judgment(self, client, value):
        client.reply = lambda request: json.dumps({"q": value})
        assert judge().choice("x", "Which?", CHOICE_Q["q"]["criteria"]) is None

    def test_thinking_and_fences_around_the_object_are_ignored(self, client):
        client.reply = lambda request: '<think>{"q": 0.1} maybe</think>\n```json\n{"q": 0.75}\n```'
        assert judge().noul("x", "Is it?") == 0.75

    def test_score_questions_are_refused_without_a_request(self, client):
        j = judge()
        assert j.ask("x", {"q": {"type": "score", "instructions": "How?", "criteria": ["a", "b"]}}) is None
        assert FakeOpenAI.instances == [] or client.get().requests == []

    def test_oversized_state_is_refused_before_the_request(self, client):
        assert judge().noul("x" * typesafe.MAX_STATE_CHARS, "Is it?") is None
        assert FakeOpenAI.instances == [] or client.get().requests == []

    def test_a_server_that_reports_no_context_gets_no_invented_limit(self, client):
        # LOCAL's server does not answer discovery, so its window is unknown.
        client.reply = lambda request: json.dumps({"q": 0.4})
        assert judge().noul("x" * (typesafe.MAX_STATE_CHARS - 200), "Is it?") == 0.4

    def test_transport_errors_and_cut_off_answers_are_no_judgment(self, client):
        client.reply = lambda request: TimeoutError("slow")
        assert judge().noul("x", "Is it?") is None

    def test_caller_budget_only_shortens_the_routes(self, client):
        client.reply = lambda request: json.dumps({"q": 0.5})
        j = judge()
        j.ask("x", NOUL_Q, timeout_s=2.0)
        j.ask("x", NOUL_Q, timeout_s=500.0)
        j.ask("x", NOUL_Q)
        # What is left of the budget once the request is built.
        assert client.get().timeouts == pytest.approx(
            [2.0, typesafe.TEXT_MODEL_TIMEOUT_S, typesafe.TEXT_MODEL_TIMEOUT_S], abs=0.5)
        assert client.get().kwargs["max_retries"] == 0

    def test_a_server_without_json_schema_gets_plain_json_from_then_on(self, client):
        class Rejected(Exception):
            status_code = 400

        def reply(request):
            if "response_format" in request:
                return Rejected("response_format json_schema is not supported")
            return json.dumps({"q": 0.6})

        client.reply = reply
        j = judge()
        assert j.noul("x", "Is it?") == 0.6
        assert j.noul("x", "Is it?") == 0.6
        assert ["response_format" in r for r in client.get().requests] == [True, False, False]

    def test_judges_share_one_client_per_endpoint_and_key(self, client):
        client.reply = lambda request: json.dumps({"q": 0.5})
        judge().noul("x", "Is it?")
        judge().noul("x", "Is it?")
        assert len(FakeOpenAI.instances) == 1
        assert FakeOpenAI.instances[0].kwargs["base_url"] == LOCAL.base_url

    def test_same_connection_compares_profile_model_and_key(self):
        assert judge().same_connection(judge())
        other = TextModelJudge("dummy", route=route_for(LOCAL, model="judge-2"))
        assert not judge().same_connection(other)
        assert not judge().same_connection(typesafe.TypeSafeJudge("k"))

    def test_locality_follows_the_profile(self):
        assert judge().is_local and route_for(LOCAL).is_local
        assert not judge(HOSTED).is_local
        assert not typesafe.TYPESAFE_ROUTE.is_local and not typesafe.OPENROUTER_ROUTE.is_local


# -- settings and routes ---------------------------------------------------------

def text_settings(**extra):
    return {SettingsKey.TYPESAFE_ENABLED: True, SettingsKey.TYPESAFE_PROVIDER: "text_model",
            SettingsKey.MEETING_LLM_PROVIDER: "ollama", SettingsKey.MEETING_LLM_MODEL: "qwen3:4b",
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter",
            SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "vendor/small-model", **extra}


class TestSettings:
    def test_existing_users_keep_their_route(self):
        assert typesafe.route_from_settings({}) is typesafe.TYPESAFE_ROUTE
        assert typesafe.route_from_settings({SettingsKey.TYPESAFE_PROVIDER: "openrouter"}) is typesafe.OPENROUTER_ROUTE

    def test_text_route_defaults_to_the_ai_insights_model(self):
        route = typesafe.route_from_settings(text_settings())
        assert (route.id, route.name, route.model) == ("text_model", "Ollama", "qwen3:4b")
        assert route.is_local and route.credential_env == "" and route.timeout_s == typesafe.TEXT_MODEL_TIMEOUT_S

    def test_text_route_can_use_the_cleanup_model(self):
        route = typesafe.route_from_settings(text_settings(**{SettingsKey.TYPESAFE_TEXT_MODEL: "cleanup"}))
        assert (route.name, route.model, route.is_local) == ("OpenRouter", "vendor/small-model", False)
        assert route.credential_env == "OPENROUTER_API_KEY"
        assert route.headers == {"X-Title": "OpenWhisper"}

    def test_local_text_route_needs_no_key(self, monkeypatch):
        monkeypatch.setattr(typesafe, "resolve_credential", lambda name: pytest.fail("key lookup"))
        assert typesafe.key_present(text_settings())
        built = typesafe.judge_from_settings(text_settings())
        assert isinstance(built, TextModelJudge) and built.model == "qwen3:4b" and built.is_local

    def test_hosted_text_route_needs_its_profiles_key(self, monkeypatch):
        keys = {}
        monkeypatch.setattr(typesafe, "resolve_credential", keys.get)
        hosted = text_settings(**{SettingsKey.TYPESAFE_TEXT_MODEL: "cleanup"})
        assert typesafe.judge_from_settings(hosted) is None and not typesafe.key_present(hosted)
        keys["OPENROUTER_API_KEY"] = "or-key"
        built = typesafe.judge_from_settings(hosted)
        assert isinstance(built, TextModelJudge) and not built.is_local

    def test_master_switch_still_gates_the_text_route(self):
        assert typesafe.judge_from_settings(text_settings(**{SettingsKey.TYPESAFE_ENABLED: False})) is None

    def test_verify_reports_latency_or_why_not(self, client):
        client.reply = lambda request: json.dumps({"q": 0.99})
        ok, detail = typesafe.verify_key("dummy", route=route_for(LOCAL))
        assert ok and detail.startswith("My server · judge-1 answered in")
        assert client.get().timeouts == pytest.approx([typesafe.TEXT_MODEL_TIMEOUT_S], abs=0.5)
        client.reply = lambda request: "Sure! The answer is yes."
        ok, detail = verify_text_model("dummy", route=route_for(LOCAL), timeout_s=5)
        assert not ok and "not with a usable judgment" in detail

    def test_verify_names_the_status_class_not_the_body(self, client):
        class NotFound(Exception):
            status_code = 404

        client.reply = lambda request: NotFound("secret body")
        ok, detail = verify_text_model("dummy", route=route_for(LOCAL), timeout_s=5)
        assert not ok and "HTTP 404" in detail and "secret" not in detail


# -- the gate: local vs hosted × AI insights consent ---------------------------

@pytest.fixture
def engine(monkeypatch, client):
    values = text_settings(**{SettingsKey.TYPESAFE_HIGHLIGHTS_ENABLED: True})
    monkeypatch.setattr(settings.settings_manager, "load_all_settings", lambda: dict(values))
    monkeypatch.setattr(typesafe, "resolve_credential", {"OPENROUTER_API_KEY": "or-key"}.get)
    repo = Mock()
    engine = MeetingEngine(MeetingEngineOptions(), repository=repo)
    engine.meeting_id = "m_gate"
    engine.store = MeetingStateStore(MeetingState("m_gate", cloud_enabled=False))
    engine._active = True
    yield SimpleNamespace(engine=engine, values=values)
    engine._shutdown_fast_features()


@pytest.mark.parametrize(("source", "consent", "allowed", "status"), [
    ("meeting", False, True, "on"),              # Ollama on this computer
    ("meeting", True, True, "on"),
    ("cleanup", False, False, "needs_insights"),  # OpenRouter-hosted model
    ("cleanup", True, True, "on"),
])
def test_gate_matrix(engine, source, consent, allowed, status):
    engine.values[SettingsKey.TYPESAFE_TEXT_MODEL] = source
    engine.engine.store.update_runtime_fields(cloud_enabled=consent)
    assert engine.engine._fast_features_allowed() is allowed
    assert (engine.engine._consented_judge() is not None) is allowed
    engine.engine._refresh_live_highlights_status()
    assert engine.engine.store.snapshot()["live_highlights_status"] == status


def test_jev_routes_still_wait_for_ai_insights(engine, monkeypatch):
    engine.values[SettingsKey.TYPESAFE_PROVIDER] = "typesafe"
    monkeypatch.setattr(typesafe, "resolve_credential", lambda name: "ts-key")
    assert not engine.engine._fast_features_allowed()
    assert engine.engine._consented_judge() is None
    engine.engine.store.update_runtime_fields(cloud_enabled=True)
    assert engine.engine._fast_features_allowed()


def test_local_workers_start_without_ai_insights_and_stop_sending_after_a_switch(engine, client):
    engine.engine._start_fast_features()
    worker = engine.engine._live_signals
    assert worker is not None
    client.reply = lambda request: json.dumps({"q": 0.4})
    assert worker.judge.noul("x", "Is it?") == 0.4
    # Switching to a hosted model mid-meeting must not carry text off this computer.
    engine.values[SettingsKey.TYPESAFE_TEXT_MODEL] = "cleanup"
    sent = len(client.get().requests)
    assert worker.judge.noul("x", "Is it?") is None
    assert len(client.get().requests) == sent


def test_local_voice_commands_apply_without_ai_insights(engine):
    engine.values[SettingsKey.TYPESAFE_VOICE_COMMANDS_ENABLED] = True
    assert engine.engine._voice_commands_allowed()
    engine.values[SettingsKey.TYPESAFE_TEXT_MODEL] = "cleanup"
    assert not engine.engine._voice_commands_allowed()


# -- one consumer per feature family -------------------------------------------

class Repo:
    def __init__(self, rows):
        self.rows = rows

    def get_segment(self, mid, sid):
        return next((dict(r) for r in self.rows if r["id"] == sid), None)

    def get_segments(self, mid, **kwargs):
        return self.rows


class ImmediateExecutor:
    def submit(self, fn, *args):
        future = Future()
        future.set_result(fn(*args))
        return future

    def shutdown(self, **kwargs):
        pass


class ManualExecutor:
    """Runs queued work when told, outside the store's subscriber callbacks."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        self.jobs.append((fn, args))

    def run(self):
        while self.jobs:
            fn, args = self.jobs.pop(0)
            fn(*args)

    def shutdown(self, **kwargs):
        self.jobs.clear()


def answer_all(noul=0.0, choice=None):
    """A reply that answers every asked question: ``choice(question id, options)`` → option number."""
    def reply(request):
        questions = asked(request)["questions"]
        out = {}
        for name, question in questions.items():
            if question["answer"] == "yes/no":
                out[name] = noul(name) if callable(noul) else noul
            else:
                out[name] = {"option": choice(name, question["options"]), "probability": 0.95}
        return json.dumps(out)
    return reply


def number_of(options, name):
    return next(label for label, option in options.items() if option["name"] == name)


def test_live_highlights_from_a_local_model(client, monkeypatch):
    monkeypatch.setattr("meeting.live_signals.resolve_typesafe_feature_enabled", lambda _: True)
    target = MeetingStateStore(MeetingState("m_live", cloud_enabled=False))
    row = {"id": "sg_one", "text": "We decided to ship on Friday.", "start_s": 10, "end_s": 15}
    client.reply = answer_all(
        noul=lambda name: 0.95 if name == "decision" else 0.05,
        choice=lambda name, options: number_of(options, "sg_one" if name == "decision_anchor" else "none"))
    worker = LiveSignals(target, Repo([row]), judge(), lambda: True, executor=ImmediateExecutor())
    worker._run(0)
    pulses = target.snapshot()["live_highlights"]
    assert [(p["kind"], p["segment_id"]) for p in pulses] == [("decision", "sg_one")]
    # No full distribution on this route, so no source ranking is claimed.
    assert "source_rank" not in pulses[0]["assessment"]


def test_citation_check_from_a_local_model(client, monkeypatch):
    monkeypatch.setattr("meeting.citation_verifier.resolve_typesafe_feature_enabled", lambda _: True)
    target = MeetingStateStore(MeetingState("m_cite", cloud_enabled=False))
    row = {"id": "sg_one", "text": "We agree to ship the beta on Friday.", "start_s": 10, "end_s": 15}
    client.reply = answer_all(choice=lambda name, options: number_of(options, "supported"))
    pool = ManualExecutor()
    verifier = CitationVerifier(target, Repo([row]), judge(), lambda: True, executor=pool)
    target.apply("agent", "notes", [{"op": "add_item", "card": "decisions",
                                     "text": "The beta ships on Friday.", "evidence": ["sg_one"]}])
    pool.run()
    check = target.snapshot()["cards"]["decisions"][0]["citation_check"]
    assert check["status"] == "supported" and check["model"] == "judge-1"
    assert check["confidence"] == pytest.approx(confidence(0.95, 3))
    verifier.shutdown()


def test_semantic_search_ranks_every_meeting_only_when_the_judge_is_local(client, monkeypatch):
    monkeypatch.setattr("meeting.semantic_search.resolve_typesafe_feature_enabled", lambda _: True)
    seen = {}

    def candidates(query, **kwargs):
        seen.update(kwargs)
        return [{"segment_id": "sg_a", "meeting_id": "m_private", "text": "Launch postponed"}]

    repo = SimpleNamespace(search_transcripts=lambda *a, **k: [], search_candidates=candidates,
                           get_meeting=lambda mid: {"cloud_enabled": False})
    client.reply = answer_all(noul=0.9)
    result = search_history(repo, "shipping delay", semantic=True, judge=judge())
    assert seen["shared_only"] is False
    assert result["mode"] == "semantic" and result["message"] == LOCAL_SCOPE
    assert [r["meeting_id"] for r in result["results"]] == ["m_private"]

    hosted = TextModelJudge("cloud-key", route=route_for(HOSTED))
    result = search_history(repo, "shipping delay", semantic=True, judge=hosted)
    assert seen["shared_only"] is True
    # The meeting had AI insights off, so nothing of it is sent.
    assert result["results"] == [] and result["message"] == SCOPE


def test_voice_command_from_a_local_model(client):
    store = MeetingStateStore(MeetingState("m_voice", cloud_enabled=False))
    client.reply = answer_all(choice=lambda name, options: number_of(options, "mark_decision"))
    listener = VoiceCommandListener(store, judge(), DEFAULT_VOICE_COMMAND_NAMES,
                                    allowed=lambda: True, executor=ImmediateExecutor())
    previous = {"id": "sg_prev", "text": "Let's ship on Friday.", "start_s": 1.0, "end_s": 3.0, "channel": "mic"}
    command = {"id": "sg_cmd", "text": "Note taker, mark that as a decision.", "start_s": 4.0, "end_s": 6.0, "channel": "mic"}
    listener.observe([previous])
    assert listener.observe([command]) == 1
    decisions = store.snapshot()["cards"]["decisions"]
    assert [d["text"] for d in decisions] == ["Let's ship on Friday."]


def test_route_by_voice_on_a_local_model_gets_its_own_budget(client, monkeypatch):
    from services.actions.model import Action, ActionKind

    action = Action(id="a_todo", name="Todo", kind=ActionKind.CLIPBOARD, enabled=True)
    monkeypatch.setattr(routing, "load_actions", lambda settings: [action])
    monkeypatch.setattr(routing.events, "publish", lambda *a, **k: None)
    values = text_settings(**{SettingsKey.VOICE_ROUTING_ENABLED: True,
                              SettingsKey.VOICE_ROUTING_ON_DICTATION: True})
    assert routing.route_budget(values) == routing.TEXT_MODEL_ROUTE_TIMEOUT_S
    assert routing.route_budget({}) == routing.ROUTE_TIMEOUT_S

    def choose(name, options):
        if name == "route":
            return number_of(options, "a_todo")
        return number_of(options, "buy milk")

    client.reply = answer_all(choice=choose)
    decision = routing.decide("Todo, buy milk", SimpleNamespace(route=routing.ROUTE_AUTO), values, judge=judge())
    assert decision.action_id == "a_todo" and decision.payload == "Buy milk"
    assert client.get().timeouts == pytest.approx([routing.TEXT_MODEL_ROUTE_TIMEOUT_S], abs=0.5)


def test_routing_status_says_the_words_stay_here(monkeypatch):
    from services.actions.model import Action, ActionKind

    monkeypatch.setattr(routing, "load_actions", lambda settings: [
        Action(id="a_todo", name="Todo", kind=ActionKind.CLIPBOARD, enabled=True)])
    status = routing.status(text_settings(**{SettingsKey.VOICE_ROUTING_ENABLED: True,
                                             SettingsKey.VOICE_ROUTING_ON_DICTATION: True}))
    assert status.ready
    assert "checked on this computer by Ollama to choose the action" in status.message


def test_insight_review_on_a_local_model(client, monkeypatch):
    monkeypatch.setattr("meeting.insight_review.resolve_typesafe_enabled", lambda: True)
    client.reply = answer_all(noul=0.97)
    reviewer = JudgeReviewer(judge())
    assert reviewer.model == "judge-1"
    questions = {"support": {"type": "noul", "instructions": "Supported?"}}
    assert reviewer.evaluate({"insight": "x"}, questions, consent=CONSENT) == {"support": 0.97}


def test_topic_shift_caps_a_slow_model(client):
    client.reply = answer_all(noul=0.7)
    assert topic_shift_probability(judge(), "budget talk", "hiring plans") == 0.7
    assert client.get().timeouts == pytest.approx([TOPIC_TIMEOUT_S], abs=0.5)


def test_final_highlight_pass_goes_newest_first_with_the_judges_budget(monkeypatch):
    monkeypatch.setattr("meeting.live_signals.resolve_typesafe_feature_enabled", lambda _: True)
    target = MeetingStateStore(MeetingState("m_final", status="ended", cloud_enabled=False))
    rows = [{"id": f"sg_{m}", "text": f"Minute {m} words here.", "start_s": m * 60 + 5, "end_s": m * 60 + 9}
            for m in range(3)]
    seen = []
    lock = threading.Lock()

    def ask(state, questions, timeout_s=None):
        with lock:
            seen.append((next(iter(state["passages"])), timeout_s))
        return None

    LiveSignals(target, Repo(rows), SimpleNamespace(ask=ask), lambda: True,
                executor=ImmediateExecutor()).finalize(timeout_s=30.0)
    assert [sid for sid, _ in seen] == ["sg_2", "sg_1", "sg_0"]
    assert all(20.0 < budget <= 30.0 for _, budget in seen)
