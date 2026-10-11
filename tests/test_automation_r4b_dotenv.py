"""Loading OpenWhisper's ``.env`` (review round 4).

A line no environment can hold is left out, logged by name, and never stops
the lines after it or the credential lookup that loads the file.

A variable already set to the file's value when the file loads came from the
file (VS Code's Python extension loads the repo-root ``.env``, the one a
source checkout uses, into the processes it starts), so children don't get
it; one set to another value is the user's own and they do.
"""

import logging
import os
import sys

import pytest

from services import credentials, installed_agents, process_env

SECRET = "sk-secret-value-never-logged"


@pytest.fixture
def dotenv(monkeypatch, tmp_path):
    """Write OpenWhisper's ``.env``; whatever loading it sets is undone after the test."""
    env_file = tmp_path / ".env"
    monkeypatch.setattr(credentials, "env_file_path", lambda: str(env_file))
    monkeypatch.setattr(process_env, "_original", dict(process_env._original))
    monkeypatch.setattr(credentials, "_refused_logged", set())

    def write(text, *names):
        env_file.write_text(text, encoding="utf-8", newline="\n")
        for name in names:
            monkeypatch.setenv(name, "")  # remembered as unset, so teardown removes it
            monkeypatch.delenv(name)
        return env_file

    return write


def test_a_line_no_environment_can_hold_is_skipped_and_the_rest_still_load(dotenv, caplog):
    dotenv(f"OW_R4_FIRST=one\n'OW_R4=BAD'={SECRET}\nOW_R4_LATER=later\n",
           "OW_R4_FIRST", "OW_R4_LATER")
    caplog.set_level(logging.WARNING, logger=credentials.__name__)

    assert credentials.resolve_credential("OW_R4_MISSING") is None  # loads .env; never raises
    assert os.environ["OW_R4_FIRST"] == "one"
    assert credentials.resolve_credential("OW_R4_LATER") == "later"
    assert "OW_R4=BAD" not in process_env._original  # nothing was set, so nothing to undo
    assert "'OW_R4=BAD'" in caplog.text
    assert SECRET not in caplog.text
    assert caplog.text.count("OW_R4=BAD") == 1  # once, not on every lookup


@pytest.mark.skipif(sys.platform != "win32", reason="Windows caps a variable at 32,767 characters")
def test_a_value_too_long_for_windows_is_skipped(dotenv):
    dotenv(f"OW_R4_LONG={'x' * 40_000}\nOW_R4_LATER=later\n", "OW_R4_LONG", "OW_R4_LATER")

    assert credentials.resolve_credential("OW_R4_LATER") == "later"
    assert "OW_R4_LONG" not in os.environ
    assert "OW_R4_LONG" not in process_env._original  # nothing was set, so nothing to undo


def _child_names(env):
    return {name.upper() if sys.platform == "win32" else name for name in env}


def test_a_variable_already_holding_the_files_value_came_from_the_file(dotenv, monkeypatch):
    # An editor started OpenWhisper with this .env already loaded.
    monkeypatch.setenv("OW_R4_GROQ_API_KEY", "sk-same")
    dotenv("OW_R4_GROQ_API_KEY=sk-same\n")

    credentials._load_dotenv_into_environ()

    assert os.environ["OW_R4_GROQ_API_KEY"] == "sk-same"  # the app still reads it
    assert "OW_R4_GROQ_API_KEY" not in _child_names(installed_agents.agent_child_env())


def test_a_variable_holding_another_value_is_the_users(dotenv, monkeypatch):
    monkeypatch.setenv("OW_R4_GROQ_API_KEY", "sk-exported")
    dotenv("OW_R4_GROQ_API_KEY=sk-from-dotenv\n")

    credentials._load_dotenv_into_environ()

    assert os.environ["OW_R4_GROQ_API_KEY"] == "sk-exported"
    assert installed_agents.agent_child_env()["OW_R4_GROQ_API_KEY"] == "sk-exported"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ignores case in variable names")
def test_on_windows_a_name_in_dotenv_matches_whatever_its_case(dotenv, monkeypatch):
    monkeypatch.setenv("OW_R4_SAME_KEY", "sk-same")
    monkeypatch.setenv("OW_R4_OWN_KEY", "sk-exported")
    dotenv("ow_r4_same_key=sk-same\nOw_R4_Own_Key=sk-from-dotenv\n")

    credentials._load_dotenv_into_environ()

    env = installed_agents.agent_child_env()
    assert "OW_R4_SAME_KEY" not in _child_names(env)
    assert env["OW_R4_OWN_KEY"] == "sk-exported"
