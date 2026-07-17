"""Tests for the bundled, offline local-model catalog."""

import unittest
from dataclasses import FrozenInstanceError

from config import config
from services.hf_access import MODEL_DOWNLOAD_SIZE_MB, resolve_model_repo
from services.model_catalog import MODEL_CATALOG, get_model_details


class TestModelCatalog(unittest.TestCase):
    """Catalog coverage and internal consistency."""

    def test_catalog_covers_every_concrete_model(self):
        expected = set(config.WHISPER_MODEL_CHOICES) - {"auto"}
        self.assertEqual(set(MODEL_CATALOG), expected)
        with self.assertRaises(KeyError):
            get_model_details("auto")

    def test_every_entry_is_complete_and_uses_https_sources(self):
        required_text_fields = (
            "model_name",
            "description",
            "origin_name",
            "origin_url",
            "repository_id",
            "repository_url",
            "maintainer",
            "family",
            "language_support",
            "task_support",
            "parameter_count",
            "relative_performance",
            "memory_guidance",
            "runtime_format",
            "license",
            "best_for",
        )
        for model_name, details in MODEL_CATALOG.items():
            with self.subTest(model=model_name):
                self.assertEqual(details.model_name, model_name)
                for field in required_text_fields:
                    self.assertTrue(getattr(details, field).strip(), field)
                self.assertGreater(details.download_size_mb, 0)
                self.assertTrue(details.limitations)
                self.assertGreaterEqual(len(details.source_urls), 2)
                self.assertTrue(details.origin_url.startswith("https://"))
                self.assertTrue(details.repository_url.startswith("https://"))
                self.assertTrue(
                    all(url.startswith("https://") for url in details.source_urls)
                )

    def test_repository_and_size_match_download_configuration(self):
        for model_name, details in MODEL_CATALOG.items():
            with self.subTest(model=model_name):
                self.assertEqual(details.repository_id, resolve_model_repo(model_name))
                self.assertEqual(
                    details.download_size_mb,
                    MODEL_DOWNLOAD_SIZE_MB[model_name],
                )

    def test_parameter_counts_match_published_model_families(self):
        expected = {
            "tiny": "39 million",
            "tiny.en": "39 million",
            "base": "74 million",
            "base.en": "74 million",
            "small": "244 million",
            "small.en": "244 million",
            "medium": "769 million",
            "medium.en": "769 million",
            "large-v1": "1.55 billion",
            "large-v2": "1.55 billion",
            "large-v3": "1.55 billion",
            "turbo": "809 million",
            "distil-small.en": "166 million",
            "distil-medium.en": "394 million",
            "distil-large-v2": "756 million",
            "distil-large-v3": "756 million",
        }
        self.assertEqual(
            {name: item.parameter_count for name, item in MODEL_CATALOG.items()},
            expected,
        )

    def test_catalog_entries_are_immutable(self):
        details = get_model_details("base")
        with self.assertRaises(FrozenInstanceError):
            details.description = "changed"
        with self.assertRaises(TypeError):
            MODEL_CATALOG["base"] = details

    def test_turbo_uses_full_upstream_model_name(self):
        details = get_model_details("turbo")
        self.assertEqual(
            details.origin_name,
            "openai/whisper-large-v3-turbo",
        )


if __name__ == "__main__":
    unittest.main()
