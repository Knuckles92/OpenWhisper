"""The single table of setting defaults and the generic resolvers."""
import pytest

from config import config
from services import settings as settings_module
from services.settings import (
    SETTING_DEFAULTS,
    SettingsKey,
    resolve_bool_setting,
    resolve_choice_setting,
    setting_value,
)

KEYS = {value for name, value in vars(SettingsKey).items() if name.isupper()}
#: A same-named config value that is deliberately not the setting's default.
CONFIG_NAME_EXCEPTIONS = {
    # The OpenAI profile's default model; cleanup with no saved choice uses
    # OpenRouter's free router (config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL).
    "TRANSCRIPT_CLEANUP_MODEL",
    # The old built-in prompt; with no saved prompt cleanup uses the level's
    # preset, so the setting's default is empty.
    "TRANSCRIPT_CLEANUP_PROMPT",
}


def test_every_default_belongs_to_a_settings_key():
    assert set(SETTING_DEFAULTS) <= KEYS


def test_the_table_is_read_only():
    with pytest.raises(TypeError):
        SETTING_DEFAULTS[SettingsKey.AUTO_PASTE] = False


def test_defaults_match_the_same_named_config_values():
    """config stays the source wherever it defines a setting's default."""
    for name, key in vars(SettingsKey).items():
        if not name.isupper() or name in CONFIG_NAME_EXCEPTIONS:
            continue
        if hasattr(config, name) and key in SETTING_DEFAULTS:
            assert SETTING_DEFAULTS[key] == getattr(config, name), name
    assert (
        SETTING_DEFAULTS[SettingsKey.TRANSCRIPT_CLEANUP_MODEL]
        == config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL
    )


def test_setting_value_returns_the_stored_value_unvalidated():
    assert setting_value(SettingsKey.AUTO_PASTE, {}) is True
    assert setting_value(SettingsKey.AUTO_PASTE, {SettingsKey.AUTO_PASTE: 0}) == 0
    assert setting_value(SettingsKey.WHISPER_DEVICE, {}) == config.FASTER_WHISPER_DEVICE


def test_setting_value_reads_the_file_when_no_mapping_is_given(monkeypatch):
    monkeypatch.setattr(
        settings_module.settings_manager, "load_all_settings",
        lambda: {SettingsKey.LOCAL_ASR_LANGUAGE: "auto"},
    )
    assert setting_value(SettingsKey.LOCAL_ASR_LANGUAGE) == "auto"
    assert setting_value(SettingsKey.COPY_CLIPBOARD) is True


def test_bool_and_choice_resolvers_fall_back_to_the_table():
    corrupt = {
        SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED: "yes",
        SettingsKey.MEETING_SERVER_BIND: "everywhere",
    }
    assert resolve_bool_setting(SettingsKey.TYPESAFE_TOPIC_SHIFT_ENABLED, corrupt) is True
    assert resolve_choice_setting(
        SettingsKey.MEETING_SERVER_BIND, ("localhost", "lan"), corrupt,
    ) == config.MEETING_SERVER_BIND


@pytest.mark.parametrize("name", [
    "resolve_developer_mode",
    "resolve_meeting_cloud_consent",
    "resolve_meeting_end_polish",
    "resolve_typesafe_enabled",
    "resolve_update_check_enabled",
])
def test_generated_bool_resolvers_keep_their_call_shape(name):
    resolver = getattr(settings_module, name)
    assert resolver({}) is resolver(settings={})
    assert isinstance(resolver({}), bool)
