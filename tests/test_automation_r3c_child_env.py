"""Keys OpenWhisper loaded from its .env never reach a child, whatever the file says later.

Loading records each variable it adds as the app's own (``process_env``), so
``external_env`` drops exactly those, even after the user rotates or deletes
the key in ``.env`` while OpenWhisper runs.
"""

import os
import sys

from services import credentials, installed_agents, process_env


def load_dotenv(monkeypatch, tmp_path, values):
    """``values`` in OpenWhisper's ``.env``, loaded as the app loads it; undone after the test.

    Returns the ``.env`` path.
    """
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")
    monkeypatch.setattr(credentials, "env_file_path", lambda: str(env_file))
    monkeypatch.setattr(process_env, "_original", dict(process_env._original))
    for name in values:
        monkeypatch.setenv(name, "")  # remembered as unset, so teardown removes what loading sets
        monkeypatch.delenv(name)
    credentials._load_dotenv_into_environ()
    return env_file


def rewrite(env_file, text):
    """Change ``.env`` so the parse cache sees it, even on coarse timestamps."""
    before = env_file.stat()
    env_file.write_text(text, encoding="utf-8")
    os.utime(env_file, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))


def _names(env):
    return {name.upper() if sys.platform == "win32" else name for name in env}


def test_a_key_rotated_in_dotenv_never_reaches_a_child(monkeypatch, tmp_path):
    env_file = load_dotenv(monkeypatch, tmp_path, {"ANTHROPIC_API_KEY": "sk-old-from-dotenv"})
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-old-from-dotenv"

    # The user changes the key in .env while OpenWhisper runs; the old value
    # stays in this process's environment.
    rewrite(env_file, "ANTHROPIC_API_KEY=sk-new-from-dotenv-rotated\n")
    credentials._load_dotenv_into_environ()

    assert "ANTHROPIC_API_KEY" not in _names(installed_agents.agent_child_env())


def test_a_key_deleted_from_dotenv_never_reaches_a_child(monkeypatch, tmp_path):
    env_file = load_dotenv(monkeypatch, tmp_path, {"OPENAI_API_KEY": "sk-from-dotenv"})
    rewrite(env_file, "# no keys any more\n")

    assert "OPENAI_API_KEY" not in _names(installed_agents.agent_child_env())


def test_the_app_still_reads_its_keys_from_dotenv(monkeypatch, tmp_path):
    load_dotenv(monkeypatch, tmp_path, {"OPENWHISPER_TEST_KEY": "file-value"})
    assert credentials.resolve_credential("OPENWHISPER_TEST_KEY") == "file-value"
    assert credentials.credential_source("OPENWHISPER_TEST_KEY") == credentials.CredentialSource.DOTENV


# -- however the file spells their names (review round 2) --

def test_a_dotenv_key_is_dropped_whatever_case_the_file_writes_it_in(monkeypatch, tmp_path):
    # Windows ignores case in variable names and os.environ upper-cases them,
    # so this .env entry is ANTHROPIC_API_KEY there.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("USER_OWN_VAR", "kept")
    load_dotenv(monkeypatch, tmp_path, {"Anthropic_Api_Key": "sk-from-openwhisper-dotenv"})

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
    assert "OPENWHISPER_SIDECAR_TOKEN" not in installed_agents.agent_child_env()
