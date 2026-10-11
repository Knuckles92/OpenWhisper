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


def test_a_key_the_user_exported_reaches_children_even_when_dotenv_has_it(monkeypatch, tmp_path):
    # Set before OpenWhisper started, to a value of its own: the user's, so
    # loading leaves it alone and children get it. (One set to the file's
    # value came from the file: test_automation_r4b_dotenv.)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-exported")
    monkeypatch.setattr(credentials, "env_file_path", lambda: str(tmp_path / ".env"))
    monkeypatch.setattr(process_env, "_original", dict(process_env._original))
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-from-dotenv\n", encoding="utf-8")

    credentials._load_dotenv_into_environ()

    assert installed_agents.agent_child_env()["OPENROUTER_API_KEY"] == "sk-exported"


def test_the_app_still_reads_its_keys_from_dotenv(monkeypatch, tmp_path):
    load_dotenv(monkeypatch, tmp_path, {"OPENWHISPER_TEST_KEY": "file-value"})
    assert credentials.resolve_credential("OPENWHISPER_TEST_KEY") == "file-value"
    assert credentials.credential_source("OPENWHISPER_TEST_KEY") == credentials.CredentialSource.DOTENV
