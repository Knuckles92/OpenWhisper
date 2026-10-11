"""Children never get OpenWhisper's .env keys, however the file spells their names."""

import sys

from services import credentials, installed_agents, process_env


def test_a_dotenv_key_is_dropped_whatever_case_the_file_writes_it_in(monkeypatch):
    # Windows ignores case in variable names and os.environ upper-cases them,
    # so this .env entry is ANTHROPIC_API_KEY there.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("Anthropic_Api_Key", "sk-from-openwhisper-dotenv")
    monkeypatch.setenv("USER_OWN_VAR", "kept")
    monkeypatch.setattr(credentials, "_dotenv_values",
                        lambda: {"Anthropic_Api_Key": "sk-from-openwhisper-dotenv"})

    env = installed_agents.agent_child_env()

    assert not [name for name in env if name.upper() == "ANTHROPIC_API_KEY"]
    assert env["USER_OWN_VAR"] == "kept"


def test_names_are_matched_the_way_the_system_matches_them():
    env = {"ANTHROPIC_API_KEY": "a", "Anthropic_Api_Key": "b", "KEEP": "1"}
    process_env.drop(env, ["anthropic_api_key"])
    if sys.platform == "win32":
        assert env == {"KEEP": "1"}
    else:  # distinct variables elsewhere
        assert env == {"ANTHROPIC_API_KEY": "a", "Anthropic_Api_Key": "b", "KEEP": "1"}


def test_openwhispers_internal_names_are_dropped_too(monkeypatch):
    monkeypatch.setenv("OPENWHISPER_SIDECAR_TOKEN", "internal")
    monkeypatch.setattr(credentials, "_dotenv_values", lambda: {})
    assert "OPENWHISPER_SIDECAR_TOKEN" not in installed_agents.agent_child_env()
