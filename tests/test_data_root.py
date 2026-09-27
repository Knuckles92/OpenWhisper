"""Where settings live: one place per computer, whoever launches the app.

A source tree used as the app (an Arch laptop ran an unpacked checkout
under systemd) wrote its settings relative to the working directory, so its
launcher had to cd into ~/.local/share/OpenWhisper or the GPU settings
never reached the file the packaged build had used. Development checkouts
keep their data to themselves, and data already kept the old way stays put.
"""
import os

import pytest

import config as config_module


@pytest.fixture
def places(tmp_path, monkeypatch):
    per_user = tmp_path / "per-user"
    checkout = tmp_path / "checkout"
    elsewhere = tmp_path / "elsewhere"
    for folder in (checkout, elsewhere):
        folder.mkdir()
    monkeypatch.setattr(config_module, "local_app_dir", lambda: str(per_user))
    monkeypatch.setattr(config_module, "__file__", str(checkout / "config.py"))
    monkeypatch.setattr(config_module, "is_frozen", lambda: False)
    monkeypatch.delenv(config_module.DATA_DIR_ENV, raising=False)
    monkeypatch.chdir(elsewhere)
    return per_user, checkout, elsewhere


def test_a_source_tree_without_git_shares_the_per_user_directory(places):
    per_user, _checkout, _elsewhere = places

    assert config_module.data_root() == str(per_user)
    assert os.path.isdir(per_user)
    assert config_module.user_data_path("openwhisper_settings.json") == str(
        per_user / "openwhisper_settings.json"
    )


def test_it_does_not_matter_where_it_was_started(places, monkeypatch):
    per_user, checkout, _elsewhere = places

    monkeypatch.chdir(checkout)

    assert config_module.data_root() == str(per_user)


def test_a_git_checkout_keeps_its_data_in_the_checkout(places):
    _per_user, checkout, _elsewhere = places
    (checkout / ".git").write_text("gitdir: elsewhere")  # a worktree

    assert config_module.data_root() == str(checkout)


def test_settings_in_the_working_directory_stay_in_use(places):
    _per_user, _checkout, elsewhere = places
    (elsewhere / config_module.SETTINGS_FILENAME).write_text("{}")

    assert config_module.data_root() == str(elsewhere)


def test_settings_in_the_checkout_stay_in_use_from_anywhere(places):
    _per_user, checkout, _elsewhere = places
    (checkout / config_module.SETTINGS_FILENAME).write_text("{}")

    assert config_module.data_root() == str(checkout)


def test_the_environment_variable_wins(places, tmp_path, monkeypatch):
    _per_user, checkout, _elsewhere = places
    (checkout / config_module.SETTINGS_FILENAME).write_text("{}")
    monkeypatch.setenv(config_module.DATA_DIR_ENV, str(tmp_path / "chosen"))

    assert config_module.data_root() == str(tmp_path / "chosen")


def test_frozen_builds_use_the_per_user_directory(places, monkeypatch):
    per_user, _checkout, elsewhere = places
    (elsewhere / config_module.SETTINGS_FILENAME).write_text("{}")
    monkeypatch.setattr(config_module, "is_frozen", lambda: True)

    assert config_module.data_root() == str(per_user)
