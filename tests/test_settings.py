import pytest
import tempfile
import os
import json
from unittest.mock import patch

from services.settings import SettingsManager
from config import config


class TestSettingsManager:
    @pytest.fixture(autouse=True)
    def _setup(self):
        self.temp_dir = tempfile.mkdtemp()
        self.test_settings_file = os.path.join(self.temp_dir, "test_settings.json")
        self.settings_manager = SettingsManager(self.test_settings_file)

    @pytest.fixture(autouse=True)
    def _teardown(self):
        yield
        if os.path.exists(self.test_settings_file):
            os.remove(self.test_settings_file)
        os.rmdir(self.temp_dir)

    def test_load_hotkey_settings_default(self):
        hotkeys = self.settings_manager.load_hotkey_settings()
        assert hotkeys == config.DEFAULT_HOTKEYS

    def test_save_and_load_hotkey_settings(self):
        test_hotkeys = {
            'record_toggle': 'f1',
            'cancel': 'f2',
            'enable_disable': 'ctrl+f3'
        }

        self.settings_manager.save_hotkey_settings(test_hotkeys)

        loaded_hotkeys = self.settings_manager.load_hotkey_settings()
        assert loaded_hotkeys == test_hotkeys

    def test_load_hotkey_settings_partial(self):
        partial_settings = {
            'hotkeys': {
                'record_toggle': 'f1'
                # Missing other keys
            }
        }

        with open(self.test_settings_file, 'w') as f:
            json.dump(partial_settings, f)

        # Should return the partial data, not defaults
        loaded_hotkeys = self.settings_manager.load_hotkey_settings()
        assert loaded_hotkeys == {'record_toggle': 'f1'}

    def test_save_hotkey_settings_invalid_file(self):
        invalid_manager = SettingsManager("/invalid/path/settings.json")

        with pytest.raises(Exception):
            invalid_manager.save_hotkey_settings({'test': 'value'})

    def test_load_all_settings(self):
        test_settings = {
            'hotkeys': {'record_toggle': 'f1'},
            'other_setting': 'value'
        }

        with open(self.test_settings_file, 'w') as f:
            json.dump(test_settings, f)

        loaded_settings = self.settings_manager.load_all_settings()
        assert loaded_settings == test_settings

    def test_load_all_settings_empty(self):
        loaded_settings = self.settings_manager.load_all_settings()
        assert loaded_settings == {}

    def test_save_all_settings(self):
        test_settings = {
            'hotkeys': {'record_toggle': 'f1'},
            'window_size': '400x300'
        }

        self.settings_manager.save_all_settings(test_settings)

        with open(self.test_settings_file, 'r') as f:
            saved_data = json.load(f)

        assert saved_data == test_settings

    def test_is_hf_hub_offline_env_set(self):
        """Env helper should reflect the externally supplied HF_HUB_OFFLINE."""
        from services.settings import is_hf_hub_offline_env_set

        previous = os.environ.pop("HF_HUB_OFFLINE", None)
        try:
            assert not is_hf_hub_offline_env_set()
            os.environ["HF_HUB_OFFLINE"] = "1"
            assert is_hf_hub_offline_env_set()
            os.environ["HF_HUB_OFFLINE"] = "0"
            assert not is_hf_hub_offline_env_set()
        finally:
            if previous is None:
                os.environ.pop("HF_HUB_OFFLINE", None)
            else:
                os.environ["HF_HUB_OFFLINE"] = previous

    def test_hf_access_policy_defaults_to_ask(self):
        """A new installation (no settings file) should default to 'ask'."""
        from services.settings import HuggingFaceAccessPolicy

        policy = self.settings_manager.load_hf_access_policy()
        assert policy == HuggingFaceAccessPolicy.ASK
        # No settings file should be created just by reading the default
        assert not os.path.exists(self.test_settings_file)

    def test_hf_access_policy_migrates_legacy_offline_true_to_never(self):
        """Legacy hf_hub_offline=true should migrate to the 'never' policy."""
        from services.settings import HuggingFaceAccessPolicy, SettingsKey

        self.settings_manager.save_all_settings({SettingsKey.HF_HUB_OFFLINE: True})
        policy = self.settings_manager.load_hf_access_policy()
        assert policy == HuggingFaceAccessPolicy.NEVER

        # Migration should be persisted and the legacy key removed
        stored = self.settings_manager.load_all_settings()
        assert stored.get(SettingsKey.HF_ACCESS_POLICY) == HuggingFaceAccessPolicy.NEVER
        assert SettingsKey.HF_HUB_OFFLINE not in stored

    def test_hf_access_policy_migrates_legacy_offline_false_to_ask(self):
        """Legacy hf_hub_offline=false should migrate to 'ask' for existing installs."""
        from services.settings import HuggingFaceAccessPolicy, SettingsKey

        self.settings_manager.save_all_settings({SettingsKey.HF_HUB_OFFLINE: False})
        policy = self.settings_manager.load_hf_access_policy()
        assert policy == HuggingFaceAccessPolicy.ASK

        stored = self.settings_manager.load_all_settings()
        assert stored.get(SettingsKey.HF_ACCESS_POLICY) == HuggingFaceAccessPolicy.ASK
        assert SettingsKey.HF_HUB_OFFLINE not in stored

    def test_hf_access_policy_invalid_value_falls_back_to_ask(self):
        """A corrupted policy value should fall back to 'ask' and be repaired."""
        from services.settings import HuggingFaceAccessPolicy, SettingsKey

        self.settings_manager.save_all_settings(
            {SettingsKey.HF_ACCESS_POLICY: "yolo"}
        )
        policy = self.settings_manager.load_hf_access_policy()
        assert policy == HuggingFaceAccessPolicy.ASK
        stored = self.settings_manager.load_all_settings()
        assert stored.get(SettingsKey.HF_ACCESS_POLICY) == HuggingFaceAccessPolicy.ASK

    def test_save_hf_access_policy_roundtrip_and_legacy_cleanup(self):
        """Saving a policy should persist it and drop the legacy key."""
        from services.settings import HuggingFaceAccessPolicy, SettingsKey

        self.settings_manager.save_all_settings({SettingsKey.HF_HUB_OFFLINE: True})
        self.settings_manager.save_hf_access_policy(HuggingFaceAccessPolicy.ALWAYS)

        assert self.settings_manager.load_hf_access_policy() == HuggingFaceAccessPolicy.ALWAYS
        stored = self.settings_manager.load_all_settings()
        assert SettingsKey.HF_HUB_OFFLINE not in stored

    def test_save_hf_access_policy_rejects_invalid_value(self):
        """Saving an unrecognized policy value should raise ValueError."""
        with pytest.raises(ValueError):
            self.settings_manager.save_hf_access_policy("sometimes")


class TestTranscriptCleanupRules:
    """Learned-rules resolution and prompt composition."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from services.settings import (
            SettingsKey,
            compose_transcript_cleanup_prompt,
            resolve_transcript_cleanup_rules,
        )
        self.key = SettingsKey.TRANSCRIPT_CLEANUP_RULES
        self.resolve = resolve_transcript_cleanup_rules
        self.compose = compose_transcript_cleanup_prompt

    def test_missing_key_returns_empty_list(self):
        assert self.resolve({}) == []

    def test_non_list_value_returns_empty_list(self):
        assert self.resolve({self.key: "not a list"}) == []
        assert self.resolve({self.key: {"a": 1}}) == []
        assert self.resolve({self.key: None}) == []

    def test_entries_are_filtered_and_stripped(self):
        rules = self.resolve(
            {self.key: ["  Rule one  ", "", "   ", 42, None, "Rule two"]}
        )
        assert rules == ["Rule one", "Rule two"]

    def test_rules_capped_at_config_limit(self):
        rules = self.resolve(
            {self.key: [f"Rule {i}" for i in range(config.MAX_TRANSCRIPT_CLEANUP_RULES + 10)]}
        )
        assert len(rules) == config.MAX_TRANSCRIPT_CLEANUP_RULES

    def test_compose_without_rules_returns_base_unchanged(self):
        assert self.compose("Base prompt.", []) == "Base prompt."

    def test_compose_appends_numbered_rules(self):
        result = self.compose(
            "Base prompt.", ['Spell "Alex Rivera".', "Expand SCWA."]
        )
        assert result.startswith("Base prompt.\n\n")
        assert "Additional user-taught rules (always apply):" in result
        assert '1. Spell "Alex Rivera".' in result
        assert "2. Expand SCWA." in result

    def test_rules_round_trip_through_settings_file(self):
        temp_dir = tempfile.mkdtemp()
        path = os.path.join(temp_dir, "settings.json")
        try:
            manager = SettingsManager(path)
            rules = ['Always spell my name "Alex Rivera".', "Use bullet lists."]
            manager.save_setting(self.key, rules)
            assert self.resolve(manager.load_all_settings()) == rules
        finally:
            if os.path.exists(path):
                os.remove(path)
            os.rmdir(temp_dir)


class TestMeetingSettings:
    """Meeting Mode resolvers backing the Settings dialog's Meeting tab."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        from services.settings import (
            MeetingAgentCore,
            MeetingLanguage,
            MeetingServerBind,
            MeetingSpeakerIdBackend,
            SettingsKey,
            resolve_meeting_agent_core,
            resolve_meeting_audio_upload_consent,
            resolve_meeting_unsupported_platform_ack,
            resolve_meeting_context_folder_enabled,
            resolve_meeting_context_folder_path,
            resolve_meeting_past_recall_enabled,
            resolve_meeting_end_polish,
            resolve_meeting_end_redecode,
            resolve_meeting_end_report,
            resolve_meeting_report_brief,
            resolve_meeting_report_ribbon,
            resolve_meeting_report_signal,
            resolve_meeting_report_views,
            resolve_developer_mode,
            resolve_meeting_llm_model,
            resolve_meeting_llm_provider,
            resolve_meeting_language,
            resolve_meeting_server_bind,
            resolve_meeting_server_port,
            resolve_meeting_speaker_id_backend,
            resolve_meeting_whisper_model,
        )
        self.keys = SettingsKey
        self.agent_cores = MeetingAgentCore
        self.speaker_backends = MeetingSpeakerIdBackend
        self.languages = MeetingLanguage
        self.binds = MeetingServerBind
        self.resolve_whisper_model = resolve_meeting_whisper_model
        self.resolve_provider = resolve_meeting_llm_provider
        self.resolve_language = resolve_meeting_language
        self.resolve_llm_model = resolve_meeting_llm_model
        self.resolve_agent_core = resolve_meeting_agent_core
        self.resolve_speaker_id = resolve_meeting_speaker_id_backend
        self.resolve_audio_consent = resolve_meeting_audio_upload_consent
        self.resolve_platform_ack = resolve_meeting_unsupported_platform_ack
        self.resolve_past_recall = resolve_meeting_past_recall_enabled
        self.resolve_context_folder = resolve_meeting_context_folder_enabled
        self.resolve_context_folder_path = resolve_meeting_context_folder_path
        self.resolve_end_redecode = resolve_meeting_end_redecode
        self.resolve_end_polish = resolve_meeting_end_polish
        self.resolve_end_report = resolve_meeting_end_report
        self.resolve_report_ribbon = resolve_meeting_report_ribbon
        self.resolve_report_brief = resolve_meeting_report_brief
        self.resolve_report_signal = resolve_meeting_report_signal
        self.resolve_report_views = resolve_meeting_report_views
        self.resolve_developer_mode = resolve_developer_mode
        self.resolve_bind = resolve_meeting_server_bind
        self.resolve_port = resolve_meeting_server_port

    def test_defaults_come_from_config(self):
        assert self.resolve_whisper_model({}) == config.MEETING_WHISPER_MODEL
        assert self.resolve_language({}) == config.MEETING_LANGUAGE
        assert self.resolve_provider({}) == config.MEETING_LLM_PROVIDER
        assert self.resolve_llm_model({}) == config.MEETING_LLM_MODEL
        assert self.resolve_agent_core({}) == config.MEETING_AGENT_CORE
        assert config.MEETING_AGENT_CORE == self.agent_cores.PI
        assert self.resolve_speaker_id({}) == config.MEETING_SPEAKER_ID_BACKEND
        assert config.MEETING_SPEAKER_ID_BACKEND == self.speaker_backends.LOCAL
        assert not self.resolve_audio_consent({})
        assert not self.resolve_platform_ack({})
        assert not self.resolve_past_recall({})
        assert not self.resolve_context_folder({})
        assert self.resolve_context_folder_path({}) == ""
        assert self.resolve_bind({}) == config.MEETING_SERVER_BIND
        assert self.resolve_port({}) == config.MEETING_SERVER_PORT
        assert self.resolve_end_redecode({}) == config.MEETING_END_REDECODE
        assert self.resolve_end_polish({})
        assert self.resolve_end_report({})
        assert self.resolve_report_ribbon({})
        assert self.resolve_report_brief({})
        assert self.resolve_report_signal({})
        assert self.resolve_report_views({}) == ("ribbon", "brief", "signal")
        assert not self.resolve_developer_mode({})

    def test_saved_choices_round_trip(self):
        saved = {
            self.keys.MEETING_WHISPER_MODEL: "small.en",
            self.keys.MEETING_LANGUAGE: "en",
            self.keys.MEETING_LLM_PROVIDER: "openai",
            self.keys.MEETING_LLM_MODEL: "  gpt-4o-mini  ",
            self.keys.MEETING_AGENT_CORE: self.agent_cores.DIRECT,
            self.keys.MEETING_SPEAKER_ID_BACKEND: self.speaker_backends.OPENAI,
            self.keys.MEETING_AUDIO_UPLOAD_CONSENT_GIVEN: True,
            self.keys.MEETING_UNSUPPORTED_PLATFORM_ACK: True,
            self.keys.MEETING_PAST_RECALL_ENABLED: True,
            self.keys.MEETING_CONTEXT_FOLDER_ENABLED: True,
            self.keys.MEETING_CONTEXT_FOLDER_PATH: "  ~/Notes  ",
            self.keys.MEETING_SERVER_BIND: self.binds.LAN,
            self.keys.MEETING_SERVER_PORT: 8099,
            self.keys.MEETING_END_REDECODE: True,
            self.keys.MEETING_END_POLISH: False,
            self.keys.MEETING_END_REPORT: False,
            self.keys.MEETING_REPORT_RIBBON: True,
            self.keys.MEETING_REPORT_BRIEF: False,
            self.keys.MEETING_REPORT_SIGNAL: False,
        }
        assert self.resolve_whisper_model(saved) == "small.en"
        assert self.resolve_language(saved) == "en"
        assert self.resolve_provider(saved) == "openai"
        assert self.resolve_llm_model(saved) == "gpt-4o-mini"
        assert self.resolve_agent_core(saved) == self.agent_cores.DIRECT
        assert self.resolve_speaker_id(saved) == self.speaker_backends.OPENAI
        assert self.resolve_audio_consent(saved)
        assert self.resolve_platform_ack(saved)
        assert self.resolve_past_recall(saved)
        assert self.resolve_context_folder(saved)
        assert self.resolve_context_folder_path(saved) == os.path.normpath(os.path.expanduser("~/Notes"))
        assert self.resolve_bind(saved) == self.binds.LAN
        assert self.resolve_port(saved) == 8099
        assert self.resolve_end_redecode(saved)
        assert not self.resolve_end_polish(saved)
        assert not self.resolve_end_report(saved)
        assert self.resolve_report_ribbon(saved)
        assert not self.resolve_report_brief(saved)
        assert not self.resolve_report_signal(saved)
        assert self.resolve_report_views(saved) == ("ribbon",)
        assert self.resolve_report_views({
                self.keys.MEETING_REPORT_RIBBON: False,
                self.keys.MEETING_REPORT_BRIEF: False,
                self.keys.MEETING_REPORT_SIGNAL: False,
            }) == ("ribbon",)
        assert self.resolve_report_views({
                self.keys.MEETING_REPORT_RIBBON: "yes",
            }) == ("ribbon", "brief", "signal")
        assert self.resolve_developer_mode(
            {self.keys.DEVELOPER_MODE: True}
        )
        assert not self.resolve_developer_mode(
            {self.keys.DEVELOPER_MODE: "yes"}
        )

    def test_unknown_values_fall_back_to_defaults(self):
        saved = {
            self.keys.MEETING_WHISPER_MODEL: "not-a-model",
            self.keys.MEETING_LANGUAGE: "klingon",
            self.keys.MEETING_LLM_PROVIDER: "not-a-provider",
            self.keys.MEETING_LLM_MODEL: "   ",
            self.keys.MEETING_AGENT_CORE: "not-a-core",
            self.keys.MEETING_SPEAKER_ID_BACKEND: "not-a-backend",
            self.keys.MEETING_SERVER_BIND: "everywhere",
        }
        assert self.resolve_whisper_model(saved) == config.MEETING_WHISPER_MODEL
        assert self.resolve_language(saved) == config.MEETING_LANGUAGE
        assert self.resolve_provider(saved) == config.MEETING_LLM_PROVIDER
        assert self.resolve_llm_model(saved) == config.MEETING_LLM_MODEL
        assert self.resolve_agent_core(saved) == config.MEETING_AGENT_CORE
        assert self.resolve_speaker_id(saved) == config.MEETING_SPEAKER_ID_BACKEND
        assert self.resolve_bind(saved) == config.MEETING_SERVER_BIND
        assert not self.resolve_context_folder(
            {self.keys.MEETING_CONTEXT_FOLDER_ENABLED: "yes"}
        )
        assert not self.resolve_platform_ack(
            {self.keys.MEETING_UNSUPPORTED_PLATFORM_ACK: "yes"}
        )
        assert self.resolve_context_folder_path(
                {self.keys.MEETING_CONTEXT_FOLDER_PATH: 12}
            ) == ""

    def test_port_is_clamped_and_tolerates_junk(self):
        assert self.resolve_port({self.keys.MEETING_SERVER_PORT: -5}) == 0
        assert self.resolve_port({self.keys.MEETING_SERVER_PORT: 99999}) == 65535
        assert self.resolve_port({self.keys.MEETING_SERVER_PORT: "nope"}) == config.MEETING_SERVER_PORT

    def test_custom_profile_id_is_accepted(self):
        from services.settings import resolve_transcript_cleanup_provider

        settings = {
            self.keys.TEXT_LLM_PROFILES: [
                {
                    "id": "custom_abcd1234",
                    "name": "LM Studio",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "api_key_env": "",
                }
            ],
            self.keys.MEETING_LLM_PROVIDER: "custom_abcd1234",
            self.keys.MEETING_LLM_MODEL: "local-qwen",
            self.keys.TRANSCRIPT_CLEANUP_PROVIDER: "custom_abcd1234",
            self.keys.TRANSCRIPT_CLEANUP_MODEL: "local-qwen",
        }
        assert self.resolve_provider(settings) == "custom_abcd1234"
        assert resolve_transcript_cleanup_provider(settings) == "custom_abcd1234"

    def test_unknown_custom_profile_falls_back(self):
        from services.settings import resolve_transcript_cleanup_provider

        settings = {
            self.keys.MEETING_LLM_PROVIDER: "custom_missing",
            self.keys.TRANSCRIPT_CLEANUP_PROVIDER: "custom_missing",
        }
        assert self.resolve_provider(settings) == config.MEETING_LLM_PROVIDER
        assert resolve_transcript_cleanup_provider(settings) == config.TRANSCRIPT_CLEANUP_PROVIDER

    def test_text_llm_assignment_is_a_pair(self):
        """Last chosen is kept only when the provider is still known."""
        from services.settings import (
            resolve_meeting_llm_model,
            resolve_meeting_llm_provider,
            resolve_transcript_cleanup_model,
            resolve_transcript_cleanup_provider,
        )

        leftover = {
            self.keys.TRANSCRIPT_CLEANUP_PROVIDER: "custom_abcd1234",
            self.keys.TRANSCRIPT_CLEANUP_MODEL: "other-local",
            self.keys.MEETING_LLM_PROVIDER: "custom_abcd1234",
            self.keys.MEETING_LLM_MODEL: "other-local",
        }
        assert resolve_transcript_cleanup_provider(leftover) == "openrouter"
        assert resolve_transcript_cleanup_model(leftover) == "openrouter/free"
        assert resolve_meeting_llm_provider(leftover) == "openrouter"
        assert resolve_meeting_llm_model(leftover) == "openrouter/free"

        custom = {
            self.keys.TEXT_LLM_PROFILES: [
                {
                    "id": "custom_abcd1234",
                    "name": "LM Studio",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "api_key_env": "",
                }
            ],
            self.keys.TRANSCRIPT_CLEANUP_PROVIDER: "custom_abcd1234",
            self.keys.TRANSCRIPT_CLEANUP_MODEL: "local-qwen",
            self.keys.MEETING_LLM_PROVIDER: "custom_abcd1234",
            self.keys.MEETING_LLM_MODEL: "local-qwen",
        }
        assert resolve_transcript_cleanup_provider(custom) == "custom_abcd1234"
        assert resolve_transcript_cleanup_model(custom) == "local-qwen"
        assert resolve_meeting_llm_provider(custom) == "custom_abcd1234"
        assert resolve_meeting_llm_model(custom) == "local-qwen"

        openai = {
            self.keys.TRANSCRIPT_CLEANUP_PROVIDER: "openai",
            self.keys.TRANSCRIPT_CLEANUP_MODEL: "gpt-4o-mini",
            self.keys.MEETING_LLM_PROVIDER: "openai",
            self.keys.MEETING_LLM_MODEL: "gpt-4o-mini",
        }
        assert resolve_transcript_cleanup_provider(openai) == "openai"
        assert resolve_transcript_cleanup_model(openai) == "gpt-4o-mini"
        assert resolve_meeting_llm_provider(openai) == "openai"
        assert resolve_meeting_llm_model(openai) == "gpt-4o-mini"


class TestUpdatePreferences:
    """Resolvers for automatic update check / notify preferences."""

    def test_defaults_are_on(self):
        from services.settings import (
            resolve_update_check_enabled,
            resolve_update_notify_enabled,
            resolve_update_skipped_version,
        )

        assert config.UPDATE_CHECK_ENABLED is True
        assert config.UPDATE_NOTIFY_ENABLED is True
        assert resolve_update_check_enabled({})
        assert resolve_update_notify_enabled({})
        assert resolve_update_skipped_version({}) == ""

    def test_saved_false_round_trips(self):
        from services.settings import (
            SettingsKey,
            resolve_update_check_enabled,
            resolve_update_notify_enabled,
        )

        saved = {
            SettingsKey.UPDATE_CHECK_ENABLED: False,
            SettingsKey.UPDATE_NOTIFY_ENABLED: False,
        }
        assert not resolve_update_check_enabled(saved)
        assert not resolve_update_notify_enabled(saved)

    def test_notify_off_with_check_on(self):
        from services.settings import (
            SettingsKey,
            resolve_update_check_enabled,
            resolve_update_notify_enabled,
        )

        saved = {
            SettingsKey.UPDATE_CHECK_ENABLED: True,
            SettingsKey.UPDATE_NOTIFY_ENABLED: False,
        }
        assert resolve_update_check_enabled(saved)
        assert not resolve_update_notify_enabled(saved)

    def test_non_bool_falls_back_to_default(self):
        from services.settings import (
            SettingsKey,
            resolve_update_check_enabled,
            resolve_update_notify_enabled,
            resolve_update_skipped_version,
        )

        saved = {
            SettingsKey.UPDATE_CHECK_ENABLED: "yes",
            SettingsKey.UPDATE_NOTIFY_ENABLED: 0,
            SettingsKey.UPDATE_SKIPPED_VERSION: 2,
        }
        assert resolve_update_check_enabled(saved)
        assert resolve_update_notify_enabled(saved)
        assert resolve_update_skipped_version(saved) == ""
        assert resolve_update_skipped_version(
            {SettingsKey.UPDATE_SKIPPED_VERSION: "  2.2.0  "}
        ) == "2.2.0"


if __name__ == '__main__':
    unittest.main()
