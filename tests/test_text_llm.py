"""Tests for named OpenAI-compatible text-LLM endpoint profiles."""
import unittest
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
    upsert_custom_profile,
    validate_api_key_env,
    validate_profile_name,
)


class TestProfileValidation(unittest.TestCase):
    """Name, URL, and env-var validation for custom endpoints."""

    def test_normalize_base_url_strips_slash_and_requires_http(self):
        self.assertEqual(
            normalize_base_url("http://127.0.0.1:1234/v1/"),
            "http://127.0.0.1:1234/v1",
        )
        with self.assertRaises(ValueError):
            normalize_base_url("ftp://127.0.0.1/v1")
        with self.assertRaises(ValueError):
            normalize_base_url("not-a-url")
        with self.assertRaises(ValueError):
            normalize_base_url("")

    def test_validate_profile_name(self):
        self.assertEqual(validate_profile_name("  LM Studio  "), "LM Studio")
        with self.assertRaises(ValueError):
            validate_profile_name("   ")
        with self.assertRaises(ValueError):
            validate_profile_name("x" * 81)

    def test_validate_api_key_env_allows_blank(self):
        self.assertEqual(validate_api_key_env(""), "")
        self.assertEqual(validate_api_key_env("  LMSTUDIO_API_KEY "), "LMSTUDIO_API_KEY")
        with self.assertRaises(ValueError):
            validate_api_key_env("api-key")
        with self.assertRaises(ValueError):
            validate_api_key_env("1KEY")


class TestProfileCrud(unittest.TestCase):
    """Built-ins stay immutable; custom profiles persist without secrets."""

    def test_builtins_always_present(self):
        profiles = list_profiles({})
        self.assertEqual([p.id for p in profiles], ["openai", "openrouter"])
        self.assertTrue(all(p.builtin for p in profiles))

    def test_upsert_and_remove_custom_profile(self):
        settings = {}
        profile = upsert_custom_profile(
            settings,
            name="LM Studio",
            base_url="http://127.0.0.1:1234/v1/",
            api_key_env="",
        )
        self.assertTrue(profile.id.startswith("custom_"))
        self.assertEqual(profile.base_url, "http://127.0.0.1:1234/v1")
        self.assertEqual(profile.kind, PROFILE_KIND_CUSTOM)
        self.assertFalse(profile.builtin)
        stored = settings[SettingsKey.TEXT_LLM_PROFILES]
        self.assertEqual(stored[0]["id"], profile.id)
        self.assertNotIn("api_key", stored[0])

        updated = upsert_custom_profile(
            settings,
            name="Home Lab",
            base_url="http://127.0.0.1:8000/v1",
            api_key_env="HOME_LLM_KEY",
            profile_id=profile.id,
        )
        self.assertEqual(updated.id, profile.id)
        self.assertEqual(updated.name, "Home Lab")
        self.assertEqual(
            get_profile(profile.id, settings).api_key_env, "HOME_LLM_KEY"
        )

        self.assertTrue(remove_custom_profile(settings, profile.id))
        self.assertIsNone(get_profile(profile.id, settings))

    def test_cannot_delete_builtin(self):
        with self.assertRaises(ValueError):
            remove_custom_profile({}, OPENAI_PROFILE_ID)


class TestCredentialsAndCatalog(unittest.TestCase):
    """Auth-free dummy keys, catalog filtering, and OpenRouter-only sort."""

    def test_auth_free_profile_uses_dummy_key(self):
        settings = {}
        profile = upsert_custom_profile(
            settings,
            name="Local",
            base_url="http://127.0.0.1:1234/v1",
        )
        self.assertEqual(resolve_api_key(profile), AUTH_FREE_API_KEY)
        with patch("services.text_llm.OpenAI") as mock_openai:
            create_openai_client(profile)
            mock_openai.assert_called_once()
            kwargs = mock_openai.call_args.kwargs
            self.assertEqual(kwargs["api_key"], AUTH_FREE_API_KEY)
            self.assertEqual(kwargs["base_url"], profile.base_url)

    def test_missing_required_key_returns_none(self):
        profile = get_profile(OPENAI_PROFILE_ID)
        with patch.dict("os.environ", {}, clear=True), patch(
            "services.text_llm.lookup_env_value", return_value=None
        ):
            self.assertIsNone(resolve_api_key(profile))

    def test_openai_catalog_filters_non_chat_models(self):
        self.assertEqual(
            filter_openai_chat_models(
                [
                    "gpt-4o-mini",
                    "whisper-1",
                    "gpt-4o-audio-preview",
                    "o4-mini",
                    "dall-e-3",
                ]
            ),
            ["gpt-4o-mini", "o4-mini"],
        )

    def test_list_chat_models_sorts_openrouter_only(self):
        openai = get_profile(OPENAI_PROFILE_ID)
        openrouter = get_profile(OPENROUTER_PROFILE_ID)
        custom = upsert_custom_profile(
            {},
            name="Local",
            base_url="http://127.0.0.1:1234/v1",
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
            self.assertEqual(
                list_chat_models(openai), ["gpt-4o-mini", "o4-mini"]
            )

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
            self.assertEqual(list_chat_models(custom, sort="top-weekly"), ["alpha", "zeta"])
            custom_client.models.list.assert_called_with()

    def test_list_chat_models_propagates_catalog_failure(self):
        profile = upsert_custom_profile(
            {},
            name="Local",
            base_url="http://127.0.0.1:1234/v1",
        )
        with patch(
            "services.text_llm.create_openai_client",
            side_effect=RuntimeError("catalog down"),
        ):
            with self.assertRaises(RuntimeError):
                list_chat_models(profile)


class TestSnapshotsAndConsent(unittest.TestCase):
    """Meeting snapshots, display names, and consent copy."""

    def test_old_meeting_row_reconstructs_builtin_profile(self):
        snapshot = snapshot_from_meeting({"agent_provider": "openai"})
        self.assertEqual(snapshot.profile_id, OPENAI_PROFILE_ID)
        self.assertEqual(snapshot.kind, "openai")
        self.assertIsNone(snapshot.base_url)

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
        self.assertEqual(snapshot.profile_id, "custom_deadbeef")
        self.assertEqual(snapshot.base_url, "http://10.0.0.8:8080/v1")
        profile = snapshot.to_profile()
        self.assertEqual(profile.kind, PROFILE_KIND_CUSTOM)
        self.assertFalse(profile.builtin)

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
        self.assertEqual(profile.id, "custom_abcd1234")
        self.assertTrue(profile.is_local)

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
        self.assertIn("127.0.0.1:1234", consent_destination(local))
        self.assertFalse(destination_is_remote(local))
        self.assertIn("Work gateway", consent_destination(remote))
        self.assertTrue(destination_is_remote(remote))
        self.assertEqual(
            consent_destination(get_profile(OPENROUTER_PROFILE_ID)),
            "OpenRouter (openrouter.ai)",
        )
        self.assertTrue(destination_is_remote(get_profile(OPENAI_PROFILE_ID)))

    def test_profile_display_name_falls_back_to_id(self):
        self.assertEqual(profile_display_name("openai"), "OpenAI")
        self.assertEqual(profile_display_name("missing_profile"), "missing_profile")
        self.assertEqual(SIDECAR_API_KEY_ENV, "OPENWHISPER_LLM_API_KEY")
