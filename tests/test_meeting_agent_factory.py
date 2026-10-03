"""Meeting intelligence only constructs SDK-backed or installed agents."""
from __future__ import annotations

import pytest

from meeting.agent.base import create_agent_core
from meeting.agent.pi_sidecar import PiSidecarAgent
from services.settings import MeetingAgentCore


@pytest.fixture(autouse=True)
def forbid_direct_agent(monkeypatch):
    """A retired loop must never be used, even as a fallback."""
    from meeting.agent import openrouter_direct

    def forbidden(*args, **kwargs):
        pytest.fail("The factory attempted to construct the retired direct API agent")

    monkeypatch.setattr(openrouter_direct, "DirectOpenRouterAgent", forbidden)


@pytest.mark.parametrize("kind", [MeetingAgentCore.PI, MeetingAgentCore.DIRECT])
def test_packaged_and_legacy_selections_construct_pi(tmp_path, kind):
    (tmp_path / "bundle.cjs").write_text("// test bundle", encoding="utf-8")
    assert isinstance(create_agent_core(kind, str(tmp_path)), PiSidecarAgent)


@pytest.mark.parametrize("kind", [MeetingAgentCore.PI, MeetingAgentCore.DIRECT])
@pytest.mark.parametrize("payload", [None, "", "missing_bundle"])
def test_missing_pi_is_actionable_and_never_falls_back(tmp_path, kind, payload):
    directory = str(tmp_path) if payload == "missing_bundle" else payload
    with pytest.raises(RuntimeError, match="Pi is unavailable") as error:
        create_agent_core(kind, directory)
    message = str(error.value)
    assert "Downloads" in message and "installed coding agent" in message
    assert "record without AI insights" in message


@pytest.mark.parametrize("kind", ["", "unknown", "openrouter"])
def test_unknown_cores_are_rejected_instead_of_using_an_api_loop(kind):
    with pytest.raises(ValueError, match="Unsupported meeting agent core"):
        create_agent_core(kind)


def test_direct_is_not_a_supported_core_or_public_export():
    import meeting.agent as agents

    assert MeetingAgentCore.DIRECT not in MeetingAgentCore.ALL
    assert "DirectOpenRouterAgent" not in agents.__all__
    assert not hasattr(agents, "DirectOpenRouterAgent")
