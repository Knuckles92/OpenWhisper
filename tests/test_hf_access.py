"""Unit tests for the Hugging Face cache/access coordinator."""
import os
import tempfile
import unittest
from unittest.mock import patch

from services.hf_access import (
    AccessDecision,
    HuggingFaceAccessCoordinator,
    format_download_size,
    is_model_cached,
    resolve_model_repo,
)
from services.settings import HuggingFaceAccessPolicy


class TestHelpers(unittest.TestCase):
    """Tests for module-level helper functions."""

    def test_format_download_size_known_models(self):
        self.assertEqual(format_download_size("base"), "~145 MB")
        self.assertEqual(format_download_size("turbo"), "~1.6 GB")

    def test_format_download_size_unknown_model(self):
        self.assertIsNone(format_download_size("some/custom-repo"))

    def test_resolve_model_repo(self):
        self.assertEqual(resolve_model_repo("base"), "Systran/faster-whisper-base")
        # Unknown names (custom repos, paths) pass through unchanged
        self.assertEqual(resolve_model_repo("me/my-model"), "me/my-model")

    def test_is_model_cached_local_directory(self):
        """A local model directory counts as cached without any lookup."""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(is_model_cached(tmp))


@patch("services.hf_access.is_hf_hub_offline_env_set", return_value=False)
@patch("services.hf_access.is_model_cached", return_value=False)
class TestEvaluateAccess(unittest.TestCase):
    """Policy/grant/env evaluation for a model missing from the cache."""

    def setUp(self):
        self.coordinator = HuggingFaceAccessCoordinator()

    def _set_policy(self, policy):
        self.coordinator.get_policy = lambda: policy

    def test_cached_model_always_loads_locally(self, mock_cached, _mock_env):
        mock_cached.return_value = True
        for policy in HuggingFaceAccessPolicy.ALL:
            self._set_policy(policy)
            self.assertEqual(
                self.coordinator.evaluate_access("base"),
                AccessDecision.LOAD_CACHED,
            )

    def test_ask_policy_needs_consent(self, _mock_cached, _mock_env):
        self._set_policy(HuggingFaceAccessPolicy.ASK)
        self.assertEqual(
            self.coordinator.evaluate_access("base"),
            AccessDecision.NEEDS_CONSENT,
        )

    def test_never_policy_needs_consent(self, _mock_cached, _mock_env):
        """'never' still surfaces the dialog (with its Download once override)."""
        self._set_policy(HuggingFaceAccessPolicy.NEVER)
        self.assertEqual(
            self.coordinator.evaluate_access("base"),
            AccessDecision.NEEDS_CONSENT,
        )

    def test_always_policy_allows_download(self, _mock_cached, _mock_env):
        self._set_policy(HuggingFaceAccessPolicy.ALWAYS)
        self.assertEqual(
            self.coordinator.evaluate_access("base"),
            AccessDecision.DOWNLOAD_ALLOWED,
        )

    def test_env_override_blocks_even_with_grant(self, _mock_cached, mock_env):
        mock_env.return_value = True
        self._set_policy(HuggingFaceAccessPolicy.ALWAYS)
        self.coordinator.grant_once("base")
        self.assertEqual(
            self.coordinator.evaluate_access("base"),
            AccessDecision.BLOCKED_BY_ENV,
        )

    def test_one_time_grant_is_consumed_once(self, _mock_cached, _mock_env):
        self._set_policy(HuggingFaceAccessPolicy.ASK)
        self.coordinator.grant_once("base")

        self.assertEqual(
            self.coordinator.evaluate_access("base"),
            AccessDecision.DOWNLOAD_ALLOWED,
        )
        # Grant is spent — a second request needs fresh consent
        self.assertEqual(
            self.coordinator.evaluate_access("base"),
            AccessDecision.NEEDS_CONSENT,
        )

    def test_advisory_check_preserves_grant(self, _mock_cached, _mock_env):
        self._set_policy(HuggingFaceAccessPolicy.ASK)
        self.coordinator.grant_once("base")

        self.assertEqual(
            self.coordinator.evaluate_access("base", consume_grant=False),
            AccessDecision.DOWNLOAD_ALLOWED,
        )
        # Grant survives the advisory check and is consumed here
        self.assertEqual(
            self.coordinator.evaluate_access("base"),
            AccessDecision.DOWNLOAD_ALLOWED,
        )

    def test_grant_applies_only_to_that_model(self, _mock_cached, _mock_env):
        self._set_policy(HuggingFaceAccessPolicy.ASK)
        self.coordinator.grant_once("base")
        self.assertEqual(
            self.coordinator.evaluate_access("small"),
            AccessDecision.NEEDS_CONSENT,
        )


class TestRequestDeduplication(unittest.TestCase):
    """Only one consent dialog / download may exist per model."""

    def setUp(self):
        self.coordinator = HuggingFaceAccessCoordinator()

    def test_begin_request_claims_and_rejects_duplicates(self):
        self.assertTrue(self.coordinator.begin_request("base"))
        self.assertFalse(self.coordinator.begin_request("base"))
        # A different model gets its own slot
        self.assertTrue(self.coordinator.begin_request("small"))

    def test_end_request_releases_slot(self):
        self.assertTrue(self.coordinator.begin_request("base"))
        self.coordinator.end_request("base")
        self.assertTrue(self.coordinator.begin_request("base"))

    def test_end_request_for_unclaimed_model_is_harmless(self):
        self.coordinator.end_request("never-claimed")
        self.assertTrue(self.coordinator.begin_request("never-claimed"))


if __name__ == "__main__":
    unittest.main()
