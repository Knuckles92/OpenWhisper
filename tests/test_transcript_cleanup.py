"""Unit tests for post-ASR transcript cleanup."""
import os
import unittest
from unittest.mock import MagicMock, patch

from services.transcript_cleanup import (
    TranscriptCleanup,
    _filter_openai_chat_models,
)


class TestTranscriptCleanup(unittest.TestCase):
    """Tests for TranscriptCleanup behavior and fallbacks."""

    def test_empty_text_returns_unchanged(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        cleaner.client = MagicMock()
        self.assertEqual(cleaner.cleanup(""), "")
        self.assertEqual(cleaner.cleanup("   "), "   ")
        cleaner.client.chat.completions.create.assert_not_called()

    def test_unavailable_returns_raw(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        cleaner.client = None
        cleaner.api_key = None
        self.assertFalse(cleaner.is_available())
        self.assertEqual(cleaner.cleanup("hello um world"), "hello um world")

    def test_success_returns_cleaned_text(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Hello world."))]
        )
        cleaner.client = mock_client

        result = cleaner.cleanup("hello um world")
        self.assertEqual(result, "Hello world.")
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
        self.assertEqual(kwargs["messages"][0]["content"], custom)

    def test_api_error_falls_back_to_raw(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = TimeoutError("timed out")
        cleaner.client = mock_client

        result = cleaner.cleanup("raw transcript")
        self.assertEqual(result, "raw transcript")

    def test_empty_model_response_falls_back_to_raw(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="  "))]
        )
        cleaner.client = mock_client

        self.assertEqual(cleaner.cleanup("keep me"), "keep me")

    def test_last_error_tracks_run_outcome(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        self.assertIsNotNone(cleaner.last_error)  # no run yet

        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Cleaned."))]
        )
        cleaner.client = mock_client
        cleaner.cleanup("raw text")
        self.assertIsNone(cleaner.last_error)

        mock_client.chat.completions.create.side_effect = TimeoutError("timed out")
        cleaner.cleanup("raw text")
        self.assertIsNotNone(cleaner.last_error)

    def test_last_error_set_when_unavailable_or_empty(self):
        cleaner = TranscriptCleanup(api_key="test-key")
        cleaner.client = None
        cleaner.api_key = None
        cleaner.cleanup("hello")
        self.assertIsNotNone(cleaner.last_error)

        cleaner.cleanup("")
        self.assertIsNotNone(cleaner.last_error)


class TestTranscriptCleanupProviders(unittest.TestCase):
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
        self.assertEqual(kwargs["model"], "my-model")

    def test_default_models_per_provider(self):
        from config import config

        openai_cleaner = TranscriptCleanup(api_key="k")
        self.assertEqual(openai_cleaner.model, config.TRANSCRIPT_CLEANUP_MODEL)

        router_cleaner = TranscriptCleanup(provider="openrouter", api_key="k")
        self.assertEqual(
            router_cleaner.model, config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL
        )

    def test_reasoning_off_sends_temperature_zero(self):
        cleaner = TranscriptCleanup(api_key="k")
        cleaner.client = self._mock_client()

        cleaner.cleanup("raw text")
        kwargs = cleaner.client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["temperature"], 0)
        self.assertNotIn("reasoning_effort", kwargs)

    def test_reasoning_openai_sends_reasoning_effort(self):
        cleaner = TranscriptCleanup(api_key="k", reasoning="high")
        cleaner.client = self._mock_client()

        cleaner.cleanup("raw text")
        kwargs = cleaner.client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["reasoning_effort"], "high")
        self.assertNotIn("temperature", kwargs)

    def test_reasoning_openrouter_uses_extra_body(self):
        cleaner = TranscriptCleanup(
            provider="openrouter", api_key="k", reasoning="low"
        )
        cleaner.client = self._mock_client()

        cleaner.cleanup("raw text")
        kwargs = cleaner.client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["extra_body"], {"reasoning": {"effort": "low"}})
        self.assertNotIn("temperature", kwargs)

    def test_configure_switches_provider_and_model(self):
        cleaner = TranscriptCleanup(api_key="openai-key")
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "router-key"}):
            cleaner.configure("openrouter", "meta-llama/llama-3-8b", "medium")

        self.assertEqual(cleaner.provider, "openrouter")
        self.assertEqual(cleaner.model, "meta-llama/llama-3-8b")
        self.assertEqual(cleaner.reasoning, "medium")
        self.assertEqual(cleaner.api_key, "router-key")
        self.assertTrue(cleaner.is_available())

    def test_configure_same_provider_keeps_client(self):
        cleaner = TranscriptCleanup(api_key="openai-key")
        original_client = cleaner.client
        cleaner.configure("openai", "gpt-4.1-mini")
        self.assertIs(cleaner.client, original_client)
        self.assertEqual(cleaner.model, "gpt-4.1-mini")

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
        self.assertEqual(
            _filter_openai_chat_models(ids), ["gpt-4o-mini", "o4-mini"]
        )


class TestTranscriptCleanupSettings(unittest.TestCase):
    """Settings key / default wiring for cleanup."""

    def test_settings_key_and_config_default(self):
        from config import config
        from services.settings import SettingsKey

        self.assertEqual(
            SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, "transcript_cleanup_enabled"
        )
        self.assertEqual(
            SettingsKey.TRANSCRIPT_CLEANUP_PROMPT, "transcript_cleanup_prompt"
        )
        self.assertFalse(config.TRANSCRIPT_CLEANUP_ENABLED)
        self.assertEqual(config.TRANSCRIPT_CLEANUP_TIMEOUT_S, 8.0)
        self.assertIn("speech-to-text", config.TRANSCRIPT_CLEANUP_PROMPT)

    def test_save_and_load_cleanup_setting(self):
        import os
        import tempfile

        from services.settings import SettingsKey, SettingsManager

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "settings.json")
            manager = SettingsManager(path)
            manager.save_setting(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, True)
            self.assertTrue(
                manager.get(SettingsKey.TRANSCRIPT_CLEANUP_ENABLED, False)
            )

    def test_resolve_cleanup_prompt_custom_and_fallback(self):
        from config import config
        from services.settings import (
            SettingsKey,
            resolve_transcript_cleanup_prompt,
        )

        custom = "Make this a Slack message."
        self.assertEqual(
            resolve_transcript_cleanup_prompt(
                {SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: custom}
            ),
            custom,
        )
        self.assertEqual(
            resolve_transcript_cleanup_prompt(
                {SettingsKey.TRANSCRIPT_CLEANUP_PROMPT: "   "}
            ),
            config.TRANSCRIPT_CLEANUP_PROMPT,
        )
        self.assertEqual(
            resolve_transcript_cleanup_prompt({}),
            config.TRANSCRIPT_CLEANUP_PROMPT,
        )

    def test_resolve_provider_validates_and_falls_back(self):
        from config import config
        from services.settings import (
            SettingsKey,
            resolve_transcript_cleanup_provider,
        )

        self.assertEqual(
            resolve_transcript_cleanup_provider(
                {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter"}
            ),
            "openrouter",
        )
        self.assertEqual(
            resolve_transcript_cleanup_provider(
                {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "bogus"}
            ),
            config.TRANSCRIPT_CLEANUP_PROVIDER,
        )
        self.assertEqual(
            resolve_transcript_cleanup_provider({}),
            config.TRANSCRIPT_CLEANUP_PROVIDER,
        )

    def test_resolve_model_falls_back_per_provider(self):
        from config import config
        from services.settings import (
            SettingsKey,
            resolve_transcript_cleanup_model,
        )

        self.assertEqual(
            resolve_transcript_cleanup_model(
                {SettingsKey.TRANSCRIPT_CLEANUP_MODEL: "  my-model  "}
            ),
            "my-model",
        )
        self.assertEqual(
            resolve_transcript_cleanup_model({}),
            config.TRANSCRIPT_CLEANUP_MODEL,
        )
        self.assertEqual(
            resolve_transcript_cleanup_model(
                {SettingsKey.TRANSCRIPT_CLEANUP_PROVIDER: "openrouter"}
            ),
            config.TRANSCRIPT_CLEANUP_OPENROUTER_MODEL,
        )

    def test_resolve_reasoning_validates_and_falls_back(self):
        from config import config
        from services.settings import (
            SettingsKey,
            resolve_transcript_cleanup_reasoning,
        )

        self.assertEqual(
            resolve_transcript_cleanup_reasoning(
                {SettingsKey.TRANSCRIPT_CLEANUP_REASONING: "high"}
            ),
            "high",
        )
        self.assertEqual(
            resolve_transcript_cleanup_reasoning(
                {SettingsKey.TRANSCRIPT_CLEANUP_REASONING: "extreme"}
            ),
            config.TRANSCRIPT_CLEANUP_REASONING,
        )

    def test_save_and_load_cleanup_prompt(self):
        import os
        import tempfile

        from services.settings import SettingsKey, SettingsManager

        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, "settings.json")
            manager = SettingsManager(path)
            custom = "Format as meeting notes with bullets."
            manager.save_setting(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT, custom)
            self.assertEqual(
                manager.get(SettingsKey.TRANSCRIPT_CLEANUP_PROMPT), custom
            )


if __name__ == "__main__":
    unittest.main()
