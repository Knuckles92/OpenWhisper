"""Tests for named OpenAI-compatible text-LLM endpoint profiles."""
import httpx
import pytest
from unittest.mock import MagicMock, patch

from services.settings import SettingsKey
from services.text_llm import (
    AUTH_FREE_API_KEY,
    OPENAI_PROFILE_ID,
    OPENROUTER_PROFILE_ID,
    PROFILE_KIND_CUSTOM,
    SIDECAR_API_KEY_ENV,
    consent_destination,
    create_openai_client,
    credential_label,
    destination_is_remote,
    filter_openai_chat_models,
    get_profile,
    list_chat_models,
    list_profiles,
    normalize_base_url,
    profile_display_name,
    profile_from_agent_config,
    remove_custom_profile,
    resolve_api_key,
    snapshot_from_meeting,
    snapshot_from_mapping,
    snapshot_from_profile,
    upsert_custom_profile,
    validate_api_key_env,
    validate_profile_name,
)


class TestProfileValidation:
    """Name, URL, and env-var validation for custom endpoints."""

    def test_normalize_base_url_strips_slash_and_requires_http(self):
        assert normalize_base_url("http://127.0.0.1:1234/v1/") == "http://127.0.0.1:1234/v1"
        with pytest.raises(ValueError):
            normalize_base_url("ftp://127.0.0.1/v1")
        with pytest.raises(ValueError):
            normalize_base_url("not-a-url")
        with pytest.raises(ValueError):
            normalize_base_url("")

    def test_validate_profile_name(self):
        assert validate_profile_name("  LM Studio  ") == "LM Studio"
        with pytest.raises(ValueError):
            validate_profile_name("   ")
        with pytest.raises(ValueError):
            validate_profile_name("x" * 81)

    def test_validate_api_key_env_allows_blank(self):
        assert validate_api_key_env("") == ""
        assert validate_api_key_env("  LMSTUDIO_API_KEY ") == "LMSTUDIO_API_KEY"
        with pytest.raises(ValueError):
            validate_api_key_env("api-key")
        with pytest.raises(ValueError):
            validate_api_key_env("1KEY")


class TestProfileCrud:
    """Built-ins stay immutable; custom profiles persist without secrets."""

    def test_builtins_always_present(self):
        profiles = list_profiles({})
        assert [p.id for p in profiles] == [
            "openai", "openrouter", "ollama", "lm_studio", "vllm",
            "llama_cpp", "localai", "groq", "opencode_go", "opencode_zen",
        ]
        assert all(p.builtin for p in profiles)

    def test_local_server_presets_ship_their_standard_urls(self):
        profiles = {p.id: p for p in list_profiles({})}
        expected = {
            "ollama": "http://localhost:11434/v1",
            "lm_studio": "http://127.0.0.1:1234/v1",
            "vllm": "http://127.0.0.1:8000/v1",
            "llama_cpp": "http://127.0.0.1:9931/v1",
            "localai": "http://127.0.0.1:8080/v1",
        }
        for profile_id, url in expected.items():
            profile = profiles[profile_id]
            assert profile.base_url == url
            assert profile.is_local
            assert not profile.requires_api_key
            assert resolve_api_key(profile) == AUTH_FREE_API_KEY

    def test_local_server_url_override_comes_from_its_own_setting(self):
        profile = get_profile(
            "vllm", {"vllm_base_url": "http://192.168.1.50:8000/v1/"}
        )
        assert profile.base_url == "http://192.168.1.50:8000/v1"
        # One preset's setting never leaks into another preset.
        other = get_profile(
            "lm_studio", {"vllm_base_url": "http://192.168.1.50:8000/v1"}
        )
        assert other.base_url == "http://127.0.0.1:1234/v1"

    def test_save_local_server_normalizes_per_preset(self):
        from services.text_llm import save_local_server

        with patch("services.settings.settings_manager.update_settings") as save:
            save_local_server("lm_studio", "http://localhost:1234/")
            save.assert_called_once_with({
                "lm_studio_base_url": "http://localhost:1234/v1",
                "lm_studio_api_key_env": "",
            })
            for url in ("http://user:secret@localhost", "http://localhost/?key=secret"):
                with pytest.raises(ValueError):
                    save_local_server("lm_studio", url)
            with pytest.raises(ValueError):
                save_local_server("lm_studio", "http://localhost:1234/v1", "not a name")
            with pytest.raises(ValueError):
                save_local_server("openai", "http://localhost:1234/v1")
            assert save.call_count == 1

    def test_local_preset_can_require_an_api_key(self):
        settings = {"llama_cpp_api_key_env": "LLAMA_SERVER_KEY"}
        profile = get_profile("llama_cpp", settings)
        assert profile.requires_api_key
        assert credential_label(profile) == "llama.cpp API key"
        with patch("services.text_llm.resolve_credential", return_value="sk-local"):
            assert resolve_api_key(profile) == "sk-local"
        with patch("services.text_llm.resolve_credential", return_value=None):
            assert resolve_api_key(profile) is None
        snapshot = snapshot_from_profile(profile)
        assert snapshot.to_profile().api_key_env == "LLAMA_SERVER_KEY"
        # A malformed saved variable name falls back to no key.
        assert not get_profile("llama_cpp", {"llama_cpp_api_key_env": "bad name"}).requires_api_key

    def test_upsert_and_remove_custom_profile(self):
        settings = {}
        profile = upsert_custom_profile(
            settings,
            name="LM Studio",
            base_url="http://127.0.0.1:1234/v1/",
            api_key_env="",
        )
        assert profile.id.startswith("custom_")
        assert profile.base_url == "http://127.0.0.1:1234/v1"
        assert profile.kind == PROFILE_KIND_CUSTOM
        assert not profile.builtin
        stored = settings[SettingsKey.TEXT_LLM_PROFILES]
        assert stored[0]["id"] == profile.id
        assert "api_key" not in stored[0]

        updated = upsert_custom_profile(
            settings,
            name="Home Lab",
            base_url="http://127.0.0.1:8000/v1",
            api_key_env="HOME_LLM_KEY",
            profile_id=profile.id,
        )
        assert updated.id == profile.id
        assert updated.name == "Home Lab"
        assert get_profile(profile.id, settings).api_key_env == "HOME_LLM_KEY"

        assert remove_custom_profile(settings, profile.id)
        assert get_profile(profile.id, settings) is None

    def test_cannot_delete_builtin(self):
        with pytest.raises(ValueError):
            remove_custom_profile({}, OPENAI_PROFILE_ID)

    def test_responses_endpoint_persists_and_edits_preserve_protocol(self):
        from services.text_model_catalog import model_spec
        settings = {}
        profile = upsert_custom_profile(settings, name="Gateway", base_url="https://gateway.test/v1",
                                        protocol="responses")
        restored = get_profile(profile.id, settings)
        assert restored.protocol == "responses"
        updated = upsert_custom_profile(settings, profile_id=profile.id, name="Renamed",
                                        base_url=profile.base_url)
        assert updated.protocol == "responses"
        # The endpoint-level choice also survives a snapshot without a model.
        saved = snapshot_from_mapping(snapshot_from_profile(updated).to_dict()).to_profile()
        assert model_spec(saved, "synthetic-model").protocol == "responses"
        with pytest.raises(ValueError):
            upsert_custom_profile(settings, name="Invalid", base_url=profile.base_url, protocol="unknown")

    def test_legacy_custom_endpoints_keep_chat_protocol(self):
        settings = {SettingsKey.TEXT_LLM_PROFILES: [{
            "id": "custom_legacy", "name": "Local", "base_url": "http://localhost:1234/v1",
            "api_key_env": "",
        }]}
        assert get_profile("custom_legacy", settings).protocol == "chat"


class TestCredentialsAndCatalog:
    """Auth-free dummy keys, catalog filtering, and OpenRouter-only sort."""

    def test_auth_free_profile_uses_dummy_key(self):
        settings = {}
        profile = upsert_custom_profile(
            settings,
            name="Local",
            base_url="http://127.0.0.1:1234/v1",
        )
        assert resolve_api_key(profile) == AUTH_FREE_API_KEY
        with patch("services.text_llm.OpenAI") as mock_openai:
            create_openai_client(profile)
            mock_openai.assert_called_once()
            kwargs = mock_openai.call_args.kwargs
            assert kwargs["api_key"] == AUTH_FREE_API_KEY
            assert kwargs["base_url"] == profile.base_url

    def test_missing_required_key_returns_none(self):
        profile = get_profile(OPENAI_PROFILE_ID)
        with patch.dict("os.environ", {}, clear=True), patch(
            "services.text_llm.lookup_env_value", return_value=None
        ):
            assert resolve_api_key(profile) is None

    def test_openai_catalog_filters_non_chat_models(self):
        assert filter_openai_chat_models(
                [
                    "gpt-4o-mini",
                    "whisper-1",
                    "gpt-4o-audio-preview",
                    "o4-mini",
                    "dall-e-3",
                ]
            ) == ["gpt-4o-mini", "o4-mini"]

    def test_list_chat_models_sorts_openrouter_only(self):
        openai = get_profile(OPENAI_PROFILE_ID)
        openrouter = get_profile(OPENROUTER_PROFILE_ID)
        custom = upsert_custom_profile(
            {},
            name="Gateway",
            base_url="https://gateway.test/v1",
        )

        def _client_with(ids):
            client = MagicMock()
            models = []
            for model_id in ids:
                item = MagicMock()
                item.id = model_id
                models.append(item)
            client.models.list.return_value = models
            return client

        with patch(
            "services.text_llm.create_openai_client",
            return_value=_client_with(
                ["gpt-4o-mini", "whisper-1", "o4-mini"]
            ),
        ):
            assert list_chat_models(openai) == ["gpt-4o-mini", "o4-mini"]

        router_client = _client_with(["z-model", "a-model"])
        with patch(
            "services.text_llm.create_openai_client",
            return_value=router_client,
        ):
            list_chat_models(openrouter, sort="top-weekly")
            router_client.models.list.assert_called_with(
                extra_query={"sort": "top-weekly"}
            )

        custom_client = _client_with(["zeta", "alpha"])
        with patch(
            "services.text_llm.create_openai_client",
            return_value=custom_client,
        ):
            assert list_chat_models(custom, sort="top-weekly") == ["alpha", "zeta"]
            custom_client.models.list.assert_called_with()

    def test_list_chat_models_propagates_catalog_failure(self):
        profile = upsert_custom_profile(
            {},
            name="Gateway",
            base_url="https://gateway.test/v1",
        )
        with patch(
            "services.text_llm.create_openai_client",
            side_effect=RuntimeError("catalog down"),
        ):
            with pytest.raises(RuntimeError):
                list_chat_models(profile)
        local = upsert_custom_profile(
            {}, name="Local", base_url="http://127.0.0.1:1234/v1",
        )
        # Nothing answers on loopback in tests.
        with pytest.raises(httpx.ConnectError):
            list_chat_models(local)


class TestSnapshotsAndConsent:
    """Meeting snapshots, display names, and consent copy."""

    def test_old_meeting_row_reconstructs_builtin_profile(self):
        snapshot = snapshot_from_meeting({"agent_provider": "openai"})
        assert snapshot.profile_id == OPENAI_PROFILE_ID
        assert snapshot.kind == "openai"
        assert snapshot.base_url is None

    def test_deleted_endpoint_uses_the_chosen_meeting_endpoint_not_a_cloud(self):
        settings = {
            "meeting_llm_provider": "ollama",
            "meeting_llm_model": "llama3.2",
        }
        snapshot = snapshot_from_meeting(
            {"agent_provider": "custom_deleted"}, settings,
            fallback_provider="custom_deleted",
        )
        assert snapshot.profile_id == "ollama"
        profile = profile_from_agent_config("custom_deleted", None, settings)
        assert profile.id == "ollama"
        assert profile.is_local

    def test_stored_snapshot_wins_over_current_settings(self):
        snapshot = snapshot_from_meeting(
            {
                "agent_provider": "openai",
                "agent_endpoint_json": {
                    "profile_id": "custom_deadbeef",
                    "name": "Deleted Lab",
                    "kind": "custom",
                    "base_url": "http://10.0.0.8:8080/v1",
                    "api_key_env": "LAB_KEY",
                },
            }
        )
        assert snapshot.profile_id == "custom_deadbeef"
        assert snapshot.base_url == "http://10.0.0.8:8080/v1"
        profile = snapshot.to_profile()
        assert profile.kind == PROFILE_KIND_CUSTOM
        assert not profile.builtin

    def test_profile_from_agent_config_prefers_endpoint_snapshot(self):
        profile = profile_from_agent_config(
            "openai",
            {
                "profile_id": "custom_abcd1234",
                "name": "LM Studio",
                "kind": "custom",
                "base_url": "http://127.0.0.1:1234/v1",
                "api_key_env": "",
            },
        )
        assert profile.id == "custom_abcd1234"
        assert profile.is_local

    def test_consent_destination_distinguishes_local_and_remote(self):
        local = upsert_custom_profile(
            {},
            name="LM Studio",
            base_url="http://127.0.0.1:1234/v1",
        )
        remote = upsert_custom_profile(
            {},
            name="Work gateway",
            base_url="https://llm.example.com/v1",
            api_key_env="WORK_LLM_KEY",
        )
        assert "127.0.0.1:1234" in consent_destination(local)
        assert not destination_is_remote(local)
        assert "Work gateway" in consent_destination(remote)
        assert destination_is_remote(remote)
        assert consent_destination(get_profile(OPENROUTER_PROFILE_ID)) == "OpenRouter (openrouter.ai)"
        assert destination_is_remote(get_profile(OPENAI_PROFILE_ID))

    def test_profile_display_name_falls_back_to_id(self):
        assert profile_display_name("openai") == "OpenAI"
        assert profile_display_name("missing_profile") == "missing_profile"
        assert SIDECAR_API_KEY_ENV == "OPENWHISPER_LLM_API_KEY"
