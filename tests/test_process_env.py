"""Programs OpenWhisper starts get the user's environment, not the app's."""

import os
import sys

import pytest

from services import installed_agents, process_env


@pytest.fixture(autouse=True)
def _clean():
    process_env.forget()
    yield
    process_env.forget()


def test_variables_the_app_set_are_restored(monkeypatch):
    monkeypatch.delenv("QT_QPA_PLATFORMTHEME", raising=False)
    monkeypatch.setenv("QT_WAYLAND_DISABLE_WINDOWDECORATION", "0")
    process_env.set_for_app("QT_QPA_PLATFORMTHEME", "")
    process_env.set_for_app("QT_WAYLAND_DISABLE_WINDOWDECORATION", "1")
    assert os.environ["QT_QPA_PLATFORMTHEME"] == ""

    env = process_env.external_env()
    assert "QT_QPA_PLATFORMTHEME" not in env
    assert env["QT_WAYLAND_DISABLE_WINDOWDECORATION"] == "0"


def test_a_user_value_is_kept_when_asked(monkeypatch):
    monkeypatch.setenv("QT_WAYLAND_USE_DATA_CONTROL", "0")
    process_env.set_for_app("QT_WAYLAND_USE_DATA_CONTROL", "1", keep_existing=True)
    assert os.environ["QT_WAYLAND_USE_DATA_CONTROL"] == "0"
    assert process_env.external_env()["QT_WAYLAND_USE_DATA_CONTROL"] == "0"


def test_path_entries_the_app_prepended_are_removed(monkeypatch, tmp_path):
    user = os.pathsep.join([str(tmp_path / "user-bin"), str(tmp_path / "more")])
    monkeypatch.setenv("PATH", user)
    process_env.prepend_for_app("PATH", [str(tmp_path / "cuda"), str(tmp_path / "icu")])
    process_env.prepend_for_app("PATH", [str(tmp_path / "late")])
    assert os.environ["PATH"].startswith(str(tmp_path / "late"))
    assert process_env.external_env()["PATH"] == user


def test_the_frozen_loader_environment_is_undone(monkeypatch, tmp_path):
    bundle = tmp_path / "app" / "_internal"
    bundle.mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", f"{bundle}{os.pathsep}/opt/lib")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/lib")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", str(bundle))
    monkeypatch.setenv("PATH", f"{bundle}{os.pathsep}{tmp_path / 'bin'}")
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", str(bundle))
    monkeypatch.setenv("_MEIPASS2", str(bundle))

    env = process_env.external_env()
    assert env["LD_LIBRARY_PATH"] == "/opt/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in env
    assert "DYLD_LIBRARY_PATH" not in env
    assert env["PATH"] == str(tmp_path / "bin")
    assert "_PYI_APPLICATION_HOME_DIR" not in env and "_MEIPASS2" not in env
    assert env["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_the_apps_own_virtualenv_is_left_out(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    venv = os.path.dirname(sys.prefix) if os.path.basename(sys.prefix) == "Scripts" else sys.prefix
    venv_bin = os.path.join(venv, "Scripts" if sys.platform == "win32" else "bin")
    monkeypatch.setenv("VIRTUAL_ENV", venv)
    monkeypatch.setenv("PATH", os.pathsep.join([venv_bin, str(tmp_path / "user")]))
    monkeypatch.setenv("LD_LIBRARY_PATH", os.path.join(venv, "lib", "nvidia"))

    env = process_env.external_env({"EXTRA": "1"})
    assert "VIRTUAL_ENV" not in env
    assert env["PATH"] == str(tmp_path / "user")
    assert "LD_LIBRARY_PATH" not in env
    assert env["EXTRA"] == "1"


def test_a_users_own_virtualenv_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    other = tmp_path / "their-venv"
    monkeypatch.setenv("VIRTUAL_ENV", str(other))
    assert process_env.external_env()["VIRTUAL_ENV"] == str(other)


def test_agents_hooks_and_actions_start_from_the_users_environment(monkeypatch):
    monkeypatch.delenv("QT_QPA_PLATFORMTHEME", raising=False)
    process_env.set_for_app("QT_QPA_PLATFORMTHEME", "")
    monkeypatch.setenv("OPENWHISPER_LLM_API_KEY", "internal")
    env = installed_agents.agent_child_env({"OPENWHISPER_EVENT_TYPE": "x"})
    assert "QT_QPA_PLATFORMTHEME" not in env
    assert "OPENWHISPER_LLM_API_KEY" not in env
    assert env["OPENWHISPER_EVENT_TYPE"] == "x"
