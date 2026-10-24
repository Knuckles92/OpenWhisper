"""A source checkout and the installed app keep separate device tokens.

They keep separate pairings in separate settings files, so one shared
credential name let each overwrite the other's token.
"""
from __future__ import annotations

import config
from services import credentials
from services.remote_asr import settings as remote_settings
from services.remote_asr.client import PairingResult


def _pair(token):
    return remote_settings.save_client_pairing("100.64.0.2", 47821, PairingResult(
        token=token, device_id="d", host_name="jed", fingerprint="AB" * 32,
        alternates=(), via="tailscale",
    ))


def test_the_per_user_directory_keeps_the_original_name(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "local_app_dir", lambda: str(tmp_path))

    assert remote_settings.token_credential() == remote_settings.TOKEN_CREDENTIAL


def test_each_other_data_directory_gets_its_own_name(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "local_app_dir", lambda: str(tmp_path / "installed"))
    names = set()
    for checkout in ("one", "two"):
        monkeypatch.setenv(config.DATA_DIR_ENV, str(tmp_path / checkout))
        names.add(remote_settings.token_credential())

    assert len(names) == 2
    assert remote_settings.TOKEN_CREDENTIAL not in names


def test_pairing_a_checkout_leaves_the_installed_apps_token_alone(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "local_app_dir", lambda: str(tmp_path / "installed"))
    credentials.store().set(remote_settings.TOKEN_CREDENTIAL, "installed-token")

    _pair("checkout-token")

    assert credentials.store().get(remote_settings.TOKEN_CREDENTIAL) == "installed-token"
    assert remote_settings.load_client_token() == "checkout-token"

    remote_settings.forget_client_pairing()

    assert credentials.store().get(remote_settings.TOKEN_CREDENTIAL) == "installed-token"
    assert credentials.store().get(remote_settings.token_credential()) is None


def test_a_checkout_paired_before_scoping_still_reads_the_shared_token(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "local_app_dir", lambda: str(tmp_path / "installed"))
    credentials.store().set(remote_settings.TOKEN_CREDENTIAL, "older-token")

    assert remote_settings.load_client_token() == "older-token"
