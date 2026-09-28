"""InstalledAgentCore, the meeting-agent route, its cadence, and agent reports."""
from types import SimpleNamespace

import pytest

from meeting.agent.installed import core as core_module
from meeting.agent.installed.core import InstalledAgentCore, installed_agent
from meeting.agent.installed.drivers import AgentUnavailable, PassOutcome
from meeting.agent.scheduler import INSTALLED_AGENT_CADENCE, CheckpointScheduler
from meeting.interfaces import AgentConfig, CheckpointPayload
from services.installed_agents import InstalledAgent
from services.settings import MeetingAgentCore, SettingsKey
from tests.fakes.agent_core import RecordingAgentTools


class FakeDriver:
    """Plays one pass by calling the pass's tools the way an agent would."""

    tool_prefix = "mcp__openwhisper__"

    def __init__(self, calls=None, outcome=None):
        self.calls = [("patch_state", {"ops": [
            {"op": "set_topic", "text": "Beta", "evidence": ["sg_1"]}]})] if calls is None else calls
        self.outcome = outcome or PassOutcome(ok=True, usage={"total_tokens": 10})
        self.requests = []
        self.personas = None
        self.closed = False

    def start(self, server, personas=None, tools=None):
        self.personas = personas

    def healthy(self):
        return not self.closed

    def run_pass(self, request):
        self.requests.append(request)
        request.emit("start")
        for name, args in self.calls:
            request.tracked_handler()(name, args, {})
        request.emit("settled")
        return self.outcome

    def close(self):
        self.closed = True


@pytest.fixture
def agent(monkeypatch):
    found = InstalledAgent("claude_code", "claude", "2.1.281", account="Claude Team", signed_in=True)
    monkeypatch.setattr(core_module, "resolve_agent", lambda agent_id: found)
    return found


def _core(monkeypatch, driver):
    monkeypatch.setattr(core_module, "make_driver", lambda agent: driver)
    core = InstalledAgentCore("claude_code")
    tools = RecordingAgentTools()
    core.initialize(AgentConfig(meeting_id="m1", provider="claude_code", model="haiku",
                                api_key=None, system_prompt="COPILOT CHARTER"), tools)
    return core, tools


def _payload(**flags):
    segments = [{"id": "sg_1", "start_s": 1.0, "text": "Let's plan the beta.", "channel": "mic"}]
    return CheckpointPayload(request_id="r1", state_snapshot={"cards": {}}, new_segments=segments,
                             **flags)


def test_a_pass_applies_the_agents_tool_calls(monkeypatch, agent):
    driver = FakeDriver()
    core, tools = _core(monkeypatch, driver)
    activity = []
    core.set_activity_callback(activity.append)
    result = core.checkpoint(_payload())
    assert result.ok and [r.ok for r in result.op_results] == [True]
    assert tools.ops == [{"op": "set_topic", "text": "Beta", "evidence": ["sg_1"]}]
    request = driver.requests[0]
    assert request.persona == "build" and request.model == "haiku" and request.effort == "low"
    assert request.system_prompt.startswith("COPILOT CHARTER")
    assert "mcp__openwhisper__patch_state" in request.system_prompt
    assert {a.kind for a in activity} >= {"start", "tool", "settled"}
    core.shutdown()
    assert driver.closed and not core.is_healthy()


def test_a_notes_pass_is_the_note_taker_and_keeps_to_notes(monkeypatch, agent):
    driver = FakeDriver()
    core, tools = _core(monkeypatch, driver)
    result = core.checkpoint(_payload(is_notes=True))
    request = driver.requests[0]
    assert request.persona == "plan"
    assert request.system_prompt == driver.personas["plan"] != driver.personas["build"]
    # set_topic is outside a notes pass's job: refused by the shared policy.
    assert [r.reason for r in result.op_results] == ["notes_only"]
    assert tools.ops == []


def test_the_final_pass_thinks_harder_and_waits_longer(monkeypatch, agent):
    driver = FakeDriver()
    core, _ = _core(monkeypatch, driver)
    core.consolidate(_payload(is_consolidation=True))
    request = driver.requests[0]
    assert request.effort == "medium" and request.timeout_s >= 600


def test_tool_calls_after_the_pass_have_no_authority(monkeypatch, agent):
    driver = FakeDriver(calls=[])
    core, tools = _core(monkeypatch, driver)
    core.checkpoint(_payload())
    late = driver.requests[0].handler("patch_state", {"ops": [{"op": "set_topic", "text": "x",
                                                              "evidence": ["sg_1"]}]}, {})
    assert late[1] is True and tools.ops == []


def test_a_failed_run_reports_its_reason(monkeypatch, agent):
    driver = FakeDriver(calls=[], outcome=PassOutcome(ok=False, error="Codex: model not supported"))
    core, _ = _core(monkeypatch, driver)
    result = core.checkpoint(_payload())
    assert not result.ok and result.error == "Codex: model not supported"


@pytest.mark.parametrize("found, message", [
    (None, "not installed on this computer"),
    (InstalledAgent("claude_code", "c", "1.0", problem="OpenWhisper needs Claude Code 2.0.0"), "needs Claude Code"),
    (InstalledAgent("claude_code", "c", "2.1", signed_in=False), "not signed in"),
])
def test_an_unusable_agent_says_why(monkeypatch, found, message):
    monkeypatch.setattr(core_module, "resolve_agent", lambda agent_id: found)
    with pytest.raises(AgentUnavailable, match=message):
        installed_agent("claude_code")


def test_the_factory_builds_installed_cores():
    from meeting.agent.base import create_agent_core

    for kind in MeetingAgentCore.INSTALLED:
        assert isinstance(create_agent_core(kind), InstalledAgentCore)


# ---- cadence ----

def test_installed_agents_get_their_own_cadence():
    engine = SimpleNamespace()
    slow = CheckpointScheduler(engine, InstalledAgentCore("codex"))
    assert slow._base_interval_s == INSTALLED_AGENT_CADENCE.base_interval_s
    assert slow._pace("notes_min_interval_s", 45.0) == INSTALLED_AGENT_CADENCE.notes_min_interval_s
    fast = CheckpointScheduler(engine, SimpleNamespace())
    assert fast._base_interval_s == 15.0 and fast._pace("notes_min_interval_s", 45.0) == 45.0
    explicit = CheckpointScheduler(engine, InstalledAgentCore("codex"), base_interval_s=60.0)
    assert explicit._base_interval_s == 60.0


def test_a_mock_core_is_not_mistaken_for_a_cadence():
    from unittest.mock import MagicMock

    scheduler = CheckpointScheduler(SimpleNamespace(), MagicMock())
    assert scheduler._cadence is None and scheduler._base_interval_s == 15.0


# ---- route ----

@pytest.fixture
def no_pi(monkeypatch):
    import services.components as components

    monkeypatch.setattr(components, "meeting_agent_payload_dir", lambda kind="pi": None)


def test_route_for_an_installed_agent(no_pi):
    from services.meeting_agent_route import resolve_meeting_agent_route

    settings = {SettingsKey.MEETING_AGENT_CORE: "codex",
                SettingsKey.MEETING_AGENT_MODELS: {"codex": "gpt-6-sol", "bogus": "x"}}
    route = resolve_meeting_agent_route(settings)
    assert (route.kind, route.provider, route.model, route.endpoint) == (
        "codex", "codex", "gpt-6-sol", None)
    assert route.installed


def test_a_meeting_recorded_with_an_agent_keeps_it(no_pi):
    from services.meeting_agent_route import resolve_meeting_agent_route

    settings = {SettingsKey.MEETING_AGENT_CORE: "direct"}
    route = resolve_meeting_agent_route(settings, {"agent_provider": "opencode",
                                                   "agent_model": "prov/fast"})
    assert (route.kind, route.model) == ("opencode", "prov/fast")


def test_a_text_endpoint_meeting_rerun_on_a_newly_chosen_agent(no_pi):
    from services.meeting_agent_route import resolve_meeting_agent_route

    settings = {SettingsKey.MEETING_AGENT_CORE: "claude_code"}
    route = resolve_meeting_agent_route(settings, {"agent_provider": "openrouter",
                                                   "agent_model": "deepseek/v4"})
    assert (route.kind, route.provider, route.model) == ("claude_code", "claude_code", "")


def test_a_new_meeting_without_pi_uses_the_direct_core(no_pi):
    from services.meeting_agent_route import resolve_meeting_agent_route

    route = resolve_meeting_agent_route({SettingsKey.MEETING_AGENT_CORE: "pi"})
    assert route.kind == "direct" and route.endpoint is not None


def test_the_archive_ignores_a_recorded_endpoint_for_an_agent(monkeypatch):
    import meeting.web.archive as archive

    monkeypatch.setattr(archive, "open_store", lambda *a, **k: None)
    dashboard = archive.ArchivedMeetingDashboard(
        None, {"id": "m1", "agent_provider": "openrouter", "agent_model": "deepseek/v4"},
        spool_root="", llm_provider="claude_code", llm_model="haiku",
        agent_core_kind="claude_code",
    )
    assert (dashboard.options.llm_provider, dashboard.options.llm_model,
            dashboard.options.llm_endpoint) == ("claude_code", "haiku", None)


# ---- custom reports ----

def test_a_report_through_an_installed_agent(monkeypatch):
    import meeting.custom_report as report

    seen = {}

    def fake_task(agent_id, **kwargs):
        seen.update(kwargs, agent_id=agent_id)
        text, error = kwargs["handler"]("search_transcript", {"query": "beta"})
        assert "beta" in text.lower() and error is False
        return PassOutcome(ok=True, text="# Exec brief\n\nShip October 12.", usage={"total_tokens": 9})

    monkeypatch.setattr("meeting.agent.installed.core.run_agent_task", fake_task)
    segments = [{"id": "sg_1", "start_s": 1.0, "text": "We ship the beta October 12.",
                 "channel": "mic"}]
    result = report.generate_report({"id": "m1", "title": "Sync"}, {"meeting_id": "m1"},
                                    segments, "A brief", provider="claude_code", model="haiku")
    assert result["title"] == "Exec brief"
    assert result["sources"]["provider"] == "claude_code"
    assert result["sources"]["tools_used"] == {"search_transcript": 1}
    assert "finished report" in seen["closing"] and seen["agent_id"] == "claude_code"


def test_a_report_from_a_missing_agent_says_so(monkeypatch):
    import meeting.custom_report as report

    def missing(agent_id, **kwargs):
        raise AgentUnavailable("Codex is not installed on this computer.")

    monkeypatch.setattr("meeting.agent.installed.core.run_agent_task", missing)
    with pytest.raises(report.ReportUnavailable, match="not installed"):
        report.generate_report({"id": "m1"}, {}, [], "A brief", provider="codex", model="")
