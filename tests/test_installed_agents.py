"""Finding installed coding agents, their sign-in, their config, and models."""
import json
import os

import pytest

from services import installed_agents as ia


@pytest.mark.parametrize("text, expected", [
    ("2.1.281 (Claude Code)", (2, 1, 281, 1)),
    ("codex-cli 0.158.0-alpha.2.1", (0, 158, 0, 0)),
    ("opencode 2.0", (2, 0, 0, 1)),
    ("no version here", ()),
])
def test_parse_version(text, expected):
    assert ia.parse_version(text)[0] == expected


def test_a_prerelease_sorts_before_its_release_but_after_older_releases():
    alpha = ia.parse_version("0.158.0-alpha.2")[0]
    assert ia.parse_version("0.146.0")[0] < alpha < ia.parse_version("0.158.0")[0]


def test_find_agent_runs_the_newest_install(monkeypatch):
    # The Codex desktop app bundles a newer CLI than the one on PATH.
    versions = {"path/codex": "codex-cli 0.146.0", "app/codex": "codex-cli 0.158.0-alpha.2.1"}
    monkeypatch.setattr(ia, "candidate_paths", lambda agent_id: list(versions))
    monkeypatch.setattr(ia, "_probe_version", lambda path: ia.parse_version(versions[path]))
    agent = ia.find_agent(ia.CODEX)
    assert agent.path == "app/codex"
    assert agent.version == "0.158.0-alpha.2.1"
    assert agent.problem == ""


def test_find_agent_flags_a_version_too_old(monkeypatch):
    monkeypatch.setattr(ia, "candidate_paths", lambda agent_id: ["oc"])
    monkeypatch.setattr(ia, "_probe_version", lambda path: ia.parse_version("1.4.2"))
    agent = ia.find_agent(ia.OPENCODE)
    assert "2.0.0 or newer" in agent.problem
    assert not agent.usable


def test_find_agent_returns_none_when_nothing_answers(monkeypatch):
    monkeypatch.setattr(ia, "candidate_paths", lambda agent_id: ["broken"])
    monkeypatch.setattr(ia, "_probe_version", lambda path: ((), ""))
    assert ia.find_agent(ia.CLAUDE_CODE) is None


@pytest.mark.parametrize("status, expected", [
    ({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "team"}, (True, "Claude Team")),
    ({"loggedIn": True, "apiProvider": "bedrock"}, (True, "Amazon Bedrock")),
    ({"loggedIn": True, "authMethod": "api_key"}, (True, "API key")),
    ({"loggedIn": False}, (False, "")),
])
def test_claude_sign_in(monkeypatch, status, expected):
    monkeypatch.setattr(ia, "_run", lambda cmd, timeout_s: (0, json.dumps(status)))
    assert ia._claude_sign_in("claude") == expected


@pytest.mark.parametrize("output, code, expected", [
    ("Logged in using ChatGPT", 0, (True, "ChatGPT")),
    ("Logged in using an API key", 0, (True, "API key")),
    ("Not logged in", 1, (False, "")),
])
def test_codex_sign_in(monkeypatch, output, code, expected):
    monkeypatch.setattr(ia, "_run", lambda cmd, timeout_s: (code, output))
    assert ia._codex_sign_in("codex") == expected


def test_opencode_sign_in_stays_unknown(monkeypatch):
    # Reading it would start OpenCode's background service.
    monkeypatch.setattr(ia, "_run", lambda *a: pytest.fail("must not run a command"))
    agent = ia.with_sign_in(ia.InstalledAgent(ia.OPENCODE, "oc", "2.0.18"))
    assert agent.signed_in is None and agent.usable


def test_configured_default_models(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    claude.mkdir()
    (claude / "settings.json").write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "config.toml").write_text('model = "gpt-6-sol"\n', encoding="utf-8")
    opencode = tmp_path / "xdg" / "opencode"
    opencode.mkdir(parents=True)
    (opencode / "opencode.jsonc").write_text(
        '{\n  // their favourite\n  "model": "openrouter/deepseek/deepseek-v4.1-flash",\n'
        '  "provider": {"x": {"baseURL": "https://example.com/v1"}}, /* trailing */\n}\n',
        encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert ia.configured_default_model(ia.CLAUDE_CODE) == "opus"
    assert ia.configured_default_model(ia.CODEX) == "gpt-6-sol"
    assert ia.configured_default_model(ia.OPENCODE) == "openrouter/deepseek/deepseek-v4.1-flash"
    assert ia.default_model_label(ia.CODEX) == "Codex default (gpt-6-sol)"


def test_jsonc_keeps_slashes_inside_strings():
    text = '{"url": "https://a//b", /* c */ "x": [1, 2,],}'
    assert json.loads(ia._strip_jsonc(text)) == {"url": "https://a//b", "x": [1, 2]}


def test_codex_models_come_from_its_listed_cache(tmp_path, monkeypatch):
    (tmp_path / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "gpt-6-sol", "display_name": "GPT-6-Sol", "visibility": "list"},
        {"slug": "internal", "display_name": "Hidden", "visibility": "hide"},
    ]}), encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    models = ia.list_models(ia.InstalledAgent(ia.CODEX, "codex", "0.158.0"))
    assert [m.value for m in models] == ["", "gpt-6-sol"]


def test_claude_models_are_aliases_after_its_default(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    models = ia.list_models(ia.InstalledAgent(ia.CLAUDE_CODE, "claude", "2.1.281"))
    assert models[0].value == "" and "haiku" in [m.value for m in models]


def test_describe_choice(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    assert ia.describe_choice(ia.CLAUDE_CODE, "haiku") == "Claude Code · Haiku"
    assert ia.describe_choice(ia.OPENCODE, "openrouter/deepseek/v4") == "OpenCode · v4"
    assert ia.describe_choice(ia.CLAUDE_CODE, "") == "Claude Code · default model"


def test_agent_env_drops_keys_from_openwhispers_own_env_file(monkeypatch):
    import services.credentials as credentials

    monkeypatch.setenv("OPENROUTER_API_KEY", "from-dotenv")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "exported-by-user")
    monkeypatch.setattr(credentials, "_dotenv_values", lambda: {
        "OPENROUTER_API_KEY": "from-dotenv", "ANTHROPIC_API_KEY": "different"})
    env = ia.agent_child_env({"EXTRA": "1"})
    assert "OPENROUTER_API_KEY" not in env
    assert env["ANTHROPIC_API_KEY"] == "exported-by-user"
    assert env["EXTRA"] == "1"


def test_scan_is_cached_until_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(ia, "_scan_result", None)
    monkeypatch.setattr(ia, "find_agent", lambda agent_id: calls.append(agent_id))
    ia.scan_agents()
    ia.scan_agents()
    assert sorted(calls) == sorted(ia.AGENT_ORDER)
    ia.scan_agents(refresh=True)
    assert len(calls) == 2 * len(ia.AGENT_ORDER)
    assert ia.cached_scan() == {agent_id: None for agent_id in ia.AGENT_ORDER}


def test_candidate_paths_find_a_desktop_bundle(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("the versioned bundle folder is the Windows Codex app's layout")
    bundle = tmp_path / "OpenAI" / "Codex" / "bin" / "abc123"
    bundle.mkdir(parents=True)
    (bundle / "codex.exe").write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(ia.shutil, "which", lambda name: None)
    assert str(bundle / "codex.exe") in ia.candidate_paths(ia.CODEX)
