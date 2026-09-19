"""Runtime changes must reach existing meetings without bypassing consent."""
import json
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from meeting.engine import MeetingEngine, MeetingEngineOptions
from meeting.state.schema import MeetingState
from meeting.state.store import MeetingStateStore
from services import settings, typesafe


class ManualExecutor:
    def __init__(self):
        self.jobs = []

    def submit(self, fn, *args):
        future = Future()
        self.jobs.append((fn, args, future))
        return future

    def run(self):
        while self.jobs:
            fn, args, future = self.jobs.pop(0)
            future.set_result(fn(*args))

    def shutdown(self, **kwargs):
        self.jobs.clear()


@pytest.fixture
def runtime(monkeypatch):
    values = {"typesafe_enabled": False}
    credential = {"key": None}
    monkeypatch.setattr(settings.settings_manager, "load_all_settings", lambda: dict(values))
    monkeypatch.setattr(typesafe, "resolve_credential", lambda _: credential["key"])
    post = Mock(return_value=(200, json.dumps({
        "answers": {"q": {"type": "noul", "noul": .9}}})))
    monkeypatch.setattr(typesafe, "_http_post", post)
    for module in ("meeting.voice_commands", "meeting.live_signals", "meeting.citation_verifier"):
        monkeypatch.setattr(module + ".ThreadPoolExecutor", lambda **_: ManualExecutor())
    repo = Mock()
    repo.get_segments.return_value = [{
        "id": "sg_one", "text": "Who owns the launch?", "start_s": 10, "end_s": 20}]
    repo.get_segment.return_value = repo.get_segments.return_value[0]
    engine = MeetingEngine(MeetingEngineOptions(cloud_enabled=True), repository=repo)
    engine.meeting_id = "m_runtime"
    engine.store = MeetingStateStore(MeetingState("m_runtime", cloud_enabled=True))
    engine._active = True
    yield SimpleNamespace(engine=engine, values=values, credential=credential, post=post, repo=repo)
    engine._shutdown_voice_commands()
    engine._shutdown_fast_features()


def test_judge_retries_disabled_and_missing_key_then_reuses_success(runtime):
    engine = runtime.engine
    assert engine._typesafe_judge() is None
    runtime.values["typesafe_enabled"] = True
    assert engine._typesafe_judge() is None
    runtime.credential["key"] = "synthetic-key"
    judge = engine._typesafe_judge()
    assert isinstance(judge, typesafe.TypeSafeJudge)
    assert engine._typesafe_judge() is judge
    runtime.values["typesafe_enabled"] = False
    assert engine._typesafe_judge() is None
    runtime.values["typesafe_enabled"] = True
    assert engine._typesafe_judge() is judge
    runtime.post.assert_not_called()


def test_judge_retries_initialization_failure(runtime, monkeypatch):
    runtime.values["typesafe_enabled"] = True
    judge = Mock()
    factory = Mock(side_effect=[RuntimeError("temporary failure"), judge])
    monkeypatch.setattr(typesafe, "judge_from_settings", factory)
    assert runtime.engine._typesafe_judge() is None
    assert runtime.engine._typesafe_judge() is judge


def test_attached_topic_callback_follows_switches_key_and_cloud(runtime):
    engine = runtime.engine
    callback = engine._typesafe_topic_judge()
    assert callable(callback)
    assert callback("budget", "hiring") is None
    runtime.values.update(typesafe_enabled=True, typesafe_topic_shift_enabled=False)
    assert callback("budget", "hiring") is None
    runtime.values["typesafe_topic_shift_enabled"] = True
    assert callback("budget", "hiring") is None
    runtime.post.assert_not_called()
    runtime.credential["key"] = "synthetic-key"
    assert callback("budget", "hiring") == .9
    for flag in ("typesafe_topic_shift_enabled", "typesafe_enabled"):
        runtime.values[flag] = False
        assert callback("budget", "hiring") is None
        runtime.values[flag] = True
    engine.store.update_runtime_fields(cloud_enabled=False)
    assert callback("budget", "hiring") is None
    assert runtime.post.call_count == 1
    engine.store.update_runtime_fields(cloud_enabled=True)
    assert callback("budget", "hiring") == .9
    assert runtime.post.call_count == 2


def test_topic_result_is_discarded_when_disabled_in_flight(runtime):
    runtime.values["typesafe_enabled"] = True
    runtime.credential["key"] = "synthetic-key"
    def post(*args, **kwargs):
        runtime.values["typesafe_topic_shift_enabled"] = False
        return 200, json.dumps({"answers": {"q": {"type": "noul", "noul": .99}}})
    runtime.post.side_effect = post
    assert runtime.engine._typesafe_topic_judge()("budget", "hiring") is None
    runtime.post.assert_called_once()


def test_voice_listener_can_start_after_first_chunk_and_reenable(runtime):
    engine = runtime.engine
    assert engine._voice_command_listener() is None
    runtime.values["typesafe_enabled"] = True
    assert engine._voice_command_listener() is None
    runtime.values["typesafe_voice_commands_enabled"] = True
    listener = engine._voice_command_listener()
    assert listener is not None
    runtime.values["typesafe_voice_commands_enabled"] = False
    assert engine._voice_command_listener() is None
    runtime.values["typesafe_voice_commands_enabled"] = True
    assert engine._voice_command_listener() is listener
    engine._shutdown_voice_commands()
    assert engine._voice_command_listener() is None


@pytest.mark.parametrize("preview", [False, True])
def test_existing_unkeyed_voice_listener_uses_key_added_later(runtime, preview):
    runtime.values.update(typesafe_enabled=True, typesafe_voice_commands_enabled=True)
    engine = runtime.engine
    listener = engine._voice_command_listener()
    feedback = []
    engine.add_listener(lambda kind, payload: feedback.append(payload) if kind == "voice_feedback" else None)
    runtime.post.return_value = (200, json.dumps({"answers": {
        "q": {"type": "choice", "choice": "set_topic", "confidence": .99}}}))

    def speak(index):
        row = {"id": f"sg_{index}", "start_s": index * 10, "end_s": index * 10 + 2,
               "channel": "mic", "text": f"Note taker, new topic: budget {index}."}
        if preview:
            listener.observe_preview(row)
        else:
            listener.observe([row])
        listener._executor.run()

    speak(1)
    runtime.post.assert_not_called()
    assert feedback[-1]["phase"] == "unavailable"
    runtime.credential["key"] = "synthetic-key"
    speak(2)
    runtime.post.assert_called_once()
    assert feedback[-1]["phase"] == ("recognized" if preview else "saved")
    runtime.values["typesafe_voice_commands_enabled"] = False
    speak(3)
    assert runtime.post.call_count == 1


@pytest.mark.parametrize("initial", ["disabled", "unkeyed"])
def test_later_transcript_starts_pulses_radar_and_citations(runtime, initial, monkeypatch):
    engine = runtime.engine
    runtime.values.update(typesafe_enabled=initial == "unkeyed", typesafe_highlights_enabled=True,
                          typesafe_question_radar_enabled=True, typesafe_citations_enabled=True)
    engine._start_fast_features()
    assert getattr(engine, "_live_signals", None) is None
    runtime.values["typesafe_enabled"] = True
    runtime.credential["key"] = "synthetic-key"
    rows = runtime.repo.get_segments.return_value
    rows[0]["end_s"] = 60
    runtime.repo.commit_chunk_transcription.return_value = (rows, True)
    monkeypatch.setattr(engine, "_assign_speakers", Mock())
    monkeypatch.setattr(engine, "_maybe_revise_transcript", Mock())
    chunk = SimpleNamespace(chunk_id=1, start_s=0, duration_s=60, channel="mic")
    engine._on_chunk_result(chunk, [])
    signals, verifier = engine._live_signals, engine._citation_verifier
    assert signals.judge is verifier.judge
    ask = Mock(return_value={})
    monkeypatch.setattr(signals.judge, "ask", ask)
    signals.executor.run()
    asked = ask.call_args.args[1]
    assert "decision" in asked and "candidate_0" in asked
    engine._start_fast_features()
    assert engine._live_signals is signals and engine._citation_verifier is verifier

    choice = Mock(return_value=typesafe.ChoiceAnswer("supported", .99))
    monkeypatch.setattr(verifier.judge, "choice", choice)
    engine.store.apply("agent", "notes", [{"op": "add_item", "card": "decisions",
        "text": "The launch has an owner.", "evidence": ["sg_one"]}])
    verifier.executor.run()
    choice.assert_called_once()
    item = engine.store.snapshot()["cards"]["decisions"][0]
    assert item["citation_check"]["status"] == "supported"


def test_heartbeat_retries_workers_without_new_speech(runtime):
    engine = runtime.engine
    engine._start_fast_features()
    runtime.values["typesafe_enabled"] = True
    runtime.credential["key"] = "synthetic-key"
    engine._hb_stop = Mock()
    engine._hb_stop.wait.side_effect = [False, True]
    engine._heartbeat_loop()
    assert engine._live_signals is not None
    assert engine._citation_verifier is not None
    runtime.post.assert_not_called()


@pytest.mark.parametrize("signals_only", [False, True])
def test_worker_retry_does_not_restart_after_teardown(runtime, signals_only):
    engine = runtime.engine
    runtime.values["typesafe_enabled"] = True
    runtime.credential["key"] = "synthetic-key"
    engine._start_fast_features()
    signals, verifier = engine._live_signals, engine._citation_verifier
    engine._shutdown_fast_features(signals_only=signals_only)
    engine._start_fast_features()
    assert signals.closed and engine._live_signals is None
    assert engine._citation_verifier is (verifier if signals_only else None)
    assert verifier.closed is not signals_only


def test_worker_start_waits_for_meeting_cloud_consent(runtime):
    engine = runtime.engine
    runtime.values["typesafe_enabled"] = True
    runtime.credential["key"] = "synthetic-key"
    engine.store.update_runtime_fields(cloud_enabled=False)
    engine._start_fast_features()
    assert getattr(engine, "_live_signals", None) is None
    engine.store.update_runtime_fields(cloud_enabled=True)
    engine._start_fast_features()
    assert engine._live_signals is not None


def test_intelligence_attaches_topic_callback_before_typesafe_is_enabled(runtime, monkeypatch):
    engine = runtime.engine
    engine._agent_core = Mock()
    engine._agent_core.is_healthy.return_value = True
    scheduler = Mock()
    factory = Mock(return_value=scheduler)
    monkeypatch.setattr("meeting.agent.scheduler.CheckpointScheduler", factory)
    engine._maybe_start_intelligence()
    scheduler.start.assert_called_once()
    callback = factory.call_args.kwargs["topic_judge"]
    assert callback("budget", "hiring") is None
    runtime.values["typesafe_enabled"] = True
    runtime.credential["key"] = "synthetic-key"
    assert callback("budget", "hiring") == .9
