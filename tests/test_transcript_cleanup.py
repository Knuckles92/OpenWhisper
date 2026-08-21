"""Unit tests for post-ASR transcript cleanup."""
import pytest
import os
from unittest.mock import MagicMock, patch

from services.transcript_cleanup import (
    TranscriptCleanup,
    _filter_openai_chat_models,
)


class TestTranscriptCleanup:
    """Tests for TranscriptCleanup behavior and fallbacks."""

    def test_empty_text_returns_unchanged(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        cleaner.client = MagicMock()
        assert cleaner.cleanup("") == ""
        assert cleaner.cleanup("   ") == "   "
        cleaner.client.chat.completions.create.assert_not_called()

    def test_unavailable_returns_raw(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        cleaner.client = None
        cleaner.api_key = None
        assert not cleaner.is_available()
        assert cleaner.cleanup("hello um world") == "hello um world"

    def test_success_returns_cleaned_text(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Hello world."))]
        )
        cleaner.client = mock_client

        result = cleaner.cleanup("hello um world")
        assert result == "Hello world."
        mock_client.chat.completions.create.assert_called_once()

    def test_custom_system_prompt_is_sent(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Done."))]
        )
        cleaner.client = mock_client

        custom = "Rewrite as bullet points only."
        cleaner.cleanup("raw text", system_prompt=custom)

        kwargs = mock_client.chat.completions.create.call_args.kwargs
        assert kwargs["messages"][0]["content"] == custom

    def test_api_error_falls_back_to_raw(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = TimeoutError("timed out")
        cleaner.client = mock_client

        result = cleaner.cleanup("raw transcript")
        assert result == "raw transcript"

    def test_empty_model_response_falls_back_to_raw(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="  "))]
        )
        cleaner.client = mock_client

        assert cleaner.cleanup("keep me") == "keep me"

    def test_last_error_tracks_run_outcome(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        assert cleaner.last_error is not None  # no run yet

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Cleaned."))]
        )
        cleaner.client = mock_client
        cleaner.cleanup("raw text")
        assert cleaner.last_error is None

        mock_client.chat.completions.create.side_effect = TimeoutError("timed out")
        cleaner.cleanup("raw text")
        assert cleaner.last_error is not None

    def test_last_error_set_when_unavailable_or_empty(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        cleaner.client = None
        cleaner.api_key = None
        cleaner.cleanup("hello")
        assert cleaner.last_error is not None

        cleaner.cleanup("")
        assert cleaner.last_error is not None


class TestTranscriptCleanupProviders:
    """Provider, model, and reasoning configuration."""

    @staticmethod
    def _mock_client(content="Cleaned."):
        client = MagicMock()
        client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=content))]
        )
        return client

    def test_configured_model_is_sent(self):
        cleaner = TranscriptCleanup(api_key="test-key", model="my-model")
        cleaner.client = self._mock_client()

        cleaner.cleanup("raw text")
        kwargs = cleaner.client.chat.completions.create.call_args.kwargs
        assert kwargs["model"] == "my-model"

    def test_default_models_per_provider(self):
        from config import config

        openai_cleaner = TranscriptCleanup(api_key="k")
        assert openai_cleaner.model == config.TRANSCRIPT_CLEANUP_MODEL

        router_cleaner = TranscriptCleanup(provider="openrouter", api_key="k")
        assert router_cleaner.model == config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL

    def test_reasoning_off_sends_temperature_zero(self):
        cleaner = TranscriptCleanup(api_key="k")
        cleaner.client = self._mock_client()

        cleaner.cleanup("raw text")
        kwargs = cleaner.client.chat.completions.create.call_args.kwargs
        assert kwargs["temperature"] == 0
        assert "reasoning_effort" not in kwargs

    def test_reasoning_openai_sends_reasoning_effort(self):
        cleaner = TranscriptCleanup(api_key="k", reasoning="high")
        cleaner.client = self._mock_client()

        cleaner.cleanup("raw text")
        kwargs = cleaner.client.chat.completions.create.call_args.kwargs
        assert kwargs["reasoning_effort"] == "high"
        assert "temperature" not in kwargs

    def test_reasoning_openrouter_uses_extra_body(self):
        cleaner = TranscriptCleanup(
            provider="openrouter", api_key="k", reasoning="low"
        )
        cleaner.client = self._mock_client()

        cleaner.cleanup("raw text")
        kwargs = cleaner.client.chat.completions.create.call_args.kwargs
        assert kwargs["extra_body"] == {"reasoning": {"effort": "low"}}
        assert "temperature" not in kwargs

    def test_configure_switches_provider_and_model(self):
        cleaner = TranscriptCleanup(api_key="openai-key")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "router-key"}):
            cleaner.configure("openrouter", "meta-llama/llama-3-8b", "medium")

        assert cleaner.provider == "openrouter"
        assert cleaner.model == "meta-llama/llama-3-8b"
        assert cleaner.reasoning == "medium"
        assert cleaner.api_key == "router-key"
        assert cleaner.is_available()

    def test_configure_same_provider_keeps_client(self):
        cleaner = TranscriptCleanup(api_key="openai-key")
        original_client = cleaner.client
        cleaner.configure("openai", "gpt-4.1-mini")
        assert cleaner.client is original_client
        assert cleaner.model == "gpt-4.1-mini"

    def test_openai_model_filter_keeps_chat_models_only(self):
        ids = [
            "gpt-4o-mini",
            "whisper-1",
            "gpt-4o-audio-preview",
            "text-embedding-3-small",
            "o4-mini",
            "dall-e-3",
            "gpt-4o-mini-tts",
            "gpt-4o-realtime-preview",
        ]
        assert _filter_openai_chat_models(ids) == ["gpt-4o-mini", "o4-mini"]


class TestTranscriptCleanupSettings:
    """Settings key / default wiring for cleanup."""

    def test_settings_key_and_config_default(self):
        from config import config
        from services.settings import SettingsKey

        assert SettingsKey.TRANSCRIPT_CLEANUP_ENABLED == "transcript_cleanup_enabled"
        assert SettingsKey.TRANSCRIPT_CLEANUP_PROMPT == "transcript_cleanup_prompt"
        assert not config.TRANSCRIPT_CLEANUP_ENABLED
        assert config.TRANSCRIPT_CLEANUP_TIMEOUT_S == 8.0
        assert "speech-to-text" in config.TRANSCRIPT_CLEANUP_PROMPT

    def test_save_and_load_cleanup_setting(self):
        import os
        import tempfile

        from services.settings import SettingsKey, SettingsManager

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "settings.json")
            manager = SettingsManager(path)
            manager.save_setting(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, True)
            assert manager.get(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, False)

    def test_resolve_cleanup_prompt_custom_and_fallback(self):
        from config import config
        from services.settings import (
            SettingsKey,
            resolve_transcript_cleanup_prompt,
        )

        custom = "Make this a Slack message."
        assert resolve_transcript_cleanup_prompt(
                {SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: custom}
            ) == custom
        assert resolve_transcript_cleanup_prompt(
                {SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: "   "}
            ) == config.TRANSCRIPT_CLEANUP_PROMPT
        assert resolve_transcript_cleanup_prompt({}) == config.TRANSCRIPT_CLEANUP_PROMPT

    def test_resolve_provider_validates_and_falls_back(self):
        from config import config
        from services.settings import (
            SettingsKey,
            resolve_transcript_cleanup_provider,
        )

        assert resolve_transcript_cleanup_provider(
                {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter"}
            ) == "openrouter"
        assert resolve_transcript_cleanup_provider(
                {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "bogus"}
            ) == config.TRANSCRIPT_CLEANUP_PROVIDER
        assert resolve_transcript_cleanup_provider({}) == config.TRANSCRIPT_CLEANUP_PROVIDER

    def test_resolve_model_falls_back_per_provider(self):
        from config import config
        from services.settings import (
            SettingsKey,
            resolve_transcript_cleanup_model,
        )

        assert resolve_transcript_cleanup_model(
                {SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "  my-model  "}
            ) == "my-model"
        assert resolve_transcript_cleanup_model({}) == config.TRANSCRIPT_CLEANUP_MODEL
        assert resolve_transcript_cleanup_model(
                {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter"}
            ) == config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL

    def test_resolve_reasoning_validates_and_falls_back(self):
        from config import config
        from services.settings import (
            SettingsKey,
            resolve_transcript_cleanup_reasoning,
        )

        assert resolve_transcript_cleanup_reasoning(
                {SettingsKey.TRANSCRIPT_CLEANUP_REASONING: "high"}
            ) == "high"
        assert resolve_transcript_cleanup_reasoning(
                {SettingsKey.TRANSCRIPT_CLEANUP_REASONING: "extreme"}
            ) == config.TRANSCRIPT_CLEANUP_REASONING

    def test_save_and_load_cleanup_prompt(self):
        import os
        import tempfile

        from services.settings import SettingsKey, SettingsManager

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "settings.json")
            manager = SettingsManager(path)
            custom = "Format as meeting notes with bullets."
            manager.save_setting(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT, custom)
            assert manager.get(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT) == custom


class TestPolishCleanupRule:
    """Tests for polishing raw instructions into learned rules."""

    @staticmethod
    def _mock_openai(content):
        client = MagicMock()
        client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=content))]
        )
        return client

    def test_empty_instruction_returns_error(self):
        from services.transcript_cleanup import polish_cleanup_rule

        rule, error = polish_cleanup_rule("   ")
        assert rule == ""
        assert error is not None

    def test_unavailable_falls_back_to_verbatim(self):
        from services.transcript_cleanup import polish_cleanup_rule

        with patch("services.transcript_cleanup.find_api_key", return_value=None):
            rule, error = polish_cleanup_rule("  always spell my name Alex  ")
        assert rule == "always spell my name Alex"
        assert error is not None

    def test_success_returns_polished_rule_with_polish_prompt(self):
        from config import config
        from services.transcript_cleanup import polish_cleanup_rule

        client = self._mock_openai('Always spell the user\'s name "Alex Rivera".')
        with patch(
            "services.transcript_cleanup.find_api_key", return_value="test-key"
        ), patch(
            "services.transcript_cleanup.create_openai_client",
            return_value=client,
        ):
            rule, error = polish_cleanup_rule(
                "um so my name should always be spelled Alex Rivera"
            )

        assert rule == 'Always spell the user\'s name "Alex Rivera".'
        assert error is None
        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs["messages"][0]["content"] == config.TRANSCRIPT_CLEANUP_RULE_POLISH_PROMPT

    def test_api_error_falls_back_to_verbatim(self):
        from services.transcript_cleanup import polish_cleanup_rule

        client = MagicMock()
        client.chat.completions.create.side_effect = TimeoutError("timed out")
        with patch(
            "services.transcript_cleanup.find_api_key", return_value="test-key"
        ), patch(
            "services.transcript_cleanup.create_openai_client",
            return_value=client,
        ):
            rule, error = polish_cleanup_rule("expand SCWA")

        assert rule == "expand SCWA"
        assert error is not None


class TestCleanupCustomEndpoints:
    """Cleanup routes through named OpenAI-compatible profiles."""

    def test_configure_rebuilds_when_endpoint_url_changes(self):
        settings = {
            "text_llm_profiles": [
                {
                    "id": "custom_abcd1234",
                    "name": "Local",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "api_key_env": "",
                }
            ]
        }
        with patch(
            "services.text_llm._load_settings", return_value=settings
        ), patch(
            "services.transcript_cleanup.create_openai_client"
        ) as mock_create:
            mock_create.side_effect = lambda *args, **kwargs: MagicMock()
            cleaner = TranscriptCleanup(
                provider="custom_abcd1234", model="local-qwen"
            )
            first_client = cleaner.client
            first_connection = cleaner._connection
            assert cleaner.is_available()

            settings["text_llm_profiles"][0]["base_url"] = (
                "http://127.0.0.1:8000/v1"
            )
            cleaner.configure("custom_abcd1234", "local-qwen")

            assert cleaner.client is not first_client
            assert cleaner._connection != first_connection
            assert mock_create.call_count >= 2

    def test_list_cleanup_models_uses_custom_profile(self):
        from services.transcript_cleanup import list_cleanup_models

        settings = {
            "text_llm_profiles": [
                {
                    "id": "custom_abcd1234",
                    "name": "Local",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "api_key_env": "",
                }
            ]
        }
        with patch(
            "services.text_llm._load_settings", return_value=settings
        ), patch(
            "services.transcript_cleanup.list_chat_models",
            return_value=["alpha", "beta"],
        ) as mock_list:
            assert list_cleanup_models("custom_abcd1234") == ["alpha", "beta"]
            assert mock_list.call_args.args[0].id == "custom_abcd1234"

    def test_list_cleanup_models_catalog_failure_is_raised(self):
        from services.transcript_cleanup import list_cleanup_models

        settings = {
            "text_llm_profiles": [
                {
                    "id": "custom_abcd1234",
                    "name": "Local",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "api_key_env": "",
                }
            ]
        }
        with patch(
            "services.text_llm._load_settings", return_value=settings
        ), patch(
            "services.transcript_cleanup.list_chat_models",
            side_effect=RuntimeError("catalog down"),
        ):
            with pytest.raises(RuntimeError):
                list_cleanup_models("custom_abcd1234")

    def test_resolve_accepts_saved_custom_profile(self):
        from services.settings import (
            SettingsKey,
            resolve_transcript_cleanup_provider,
        )

        settings = {
            SettingsKey.TEXT_LLM_PROFILES: [
                {
                    "id": "custom_abcd1234",
                    "name": "Local",
                    "base_url": "http://127.0.0.1:1234/v1",
                    "api_key_env": "",
                }
            ],
            SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "custom_abcd1234",
        }
        assert resolve_transcript_cleanup_provider(settings) == "custom_abcd1234"




# --------------------------------------------------------------------------
# Streaming preview helpers (services.streaming_transcriber).
# --------------------------------------------------------------------------

from services.streaming_transcriber import append_preview_text


def test_append_preview_text_appends_with_space():
    assert append_preview_text("hello", "world") == "hello world"


def test_append_preview_text_ignores_empty_chunk():
    assert append_preview_text("hello", "  ") == "hello"
    assert append_preview_text("hello", "") == "hello"


def test_append_preview_text_starts_from_empty():
    assert append_preview_text("", "hello") == "hello"
    assert append_preview_text(None, "hello") == "hello"
