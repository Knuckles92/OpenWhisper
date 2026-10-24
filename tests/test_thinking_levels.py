"""Which thinking levels each runner and model offer, and what runs."""
from types import SimpleNamespace

from services import thinking_levels
from services.text_model_catalog import TextModelSpec


def _profile(kind):
    return SimpleNamespace(kind=kind)


def test_claude_code_offers_its_cli_levels():
    assert thinking_levels.agent_levels("claude_code", "haiku") == (
        "low", "medium", "high", "xhigh", "max")


def test_codex_levels_come_from_the_model_and_unknown_models_get_the_shared_ones(monkeypatch):
    monkeypatch.setattr("services.installed_agents.codex_model_efforts", lambda: {
        "sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
        "luna": ("medium", "low", "high", "xhigh", "max"),
    })
    monkeypatch.setattr("services.installed_agents.configured_default_model", lambda _id: "")
    assert thinking_levels.agent_levels("codex", "sol")[-1] == "ultra"
    assert thinking_levels.agent_levels("codex", "luna") == ("low", "medium", "high", "xhigh", "max")
    assert thinking_levels.agent_levels("codex", "") == ("low", "medium", "high", "xhigh", "max")


def test_endpoint_catalog_adds_and_removes_levels(monkeypatch):
    specs = {
        "plain": TextModelSpec(reasoning=True, reasoning_format="openai"),
        "deep": TextModelSpec(reasoning=True, reasoning_format="openai",
                              thinking_levels={"off": None, "xhigh": "xhigh", "max": "max"}),
        "chat": TextModelSpec(),
    }
    monkeypatch.setattr("services.text_model_catalog.model_spec", lambda _p, model: specs[model])
    custom = _profile("custom")
    assert thinking_levels.endpoint_levels(custom, "plain") == ("off", "low", "medium", "high")
    assert thinking_levels.endpoint_levels(custom, "deep") == ("low", "medium", "high", "xhigh", "max")
    assert thinking_levels.endpoint_levels(custom, "chat") == ()


def test_openai_offers_levels_only_for_reasoning_models():
    openai = _profile("openai")
    assert thinking_levels.endpoint_levels(openai, "gpt-5-mini") == ("off", "low", "medium", "high")
    assert thinking_levels.endpoint_levels(openai, "gpt-4.1") == ()


def test_saved_level_runs_at_the_nearest_offered_one():
    agent = ("low", "medium", "high", "xhigh", "max")
    assert thinking_levels.effective_level("off", agent) == "low"
    assert thinking_levels.effective_level("ultra", agent) == "max"
    assert thinking_levels.effective_level("high", agent) == "high"
    assert thinking_levels.effective_level("max", ("off", "low", "medium", "high")) == "high"
    assert thinking_levels.effective_level("high", ()) == "off"
