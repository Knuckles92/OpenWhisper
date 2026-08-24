"""Tests for the bundled, offline component profiles."""

import pytest
from dataclasses import FrozenInstanceError

from services.component_catalog import (
    COMPONENT_CATALOG,
    PI_HOME_URL,
    get_component_details,
)
from services.components import ComponentId, component_coordinator


class TestComponentCatalog:
    """Catalog coverage and internal consistency."""

    def test_catalog_covers_every_component_id(self):
        expected = {
            ComponentId.GPU_ACCEL,
            ComponentId.MEETING_AGENT,
            ComponentId.SPEAKER_ID,
        }
        assert set(COMPONENT_CATALOG) == expected
        with pytest.raises(KeyError):
            get_component_details("not-a-component")

    @pytest.mark.parametrize("component_id", sorted(COMPONENT_CATALOG))
    def test_every_entry_is_complete_and_uses_https_sources(self, component_id):
        required_text_fields = (
            "display_name",
            "summary",
            "description",
            "origin_name",
            "origin_url",
            "origin_label",
            "source_name",
            "source_url",
            "source_label",
            "maintainer",
            "family",
            "requires",
            "payload",
            "local_format",
            "license",
            "best_for",
            "compact_tags",
            "source_note",
        )
        details = COMPONENT_CATALOG[component_id]
        assert details.component_id == component_id
        for field in required_text_fields:
            assert getattr(details, field).strip(), field
        assert details.limitations
        assert len(details.source_urls) >= 2
        assert details.origin_url.startswith("https://")
        assert details.source_url.startswith("https://")
        assert all(url.startswith("https://") for url in details.source_urls)
        assert details.description != details.summary

    def test_meeting_agent_links_to_pi_and_nodejs(self):
        details = get_component_details(ComponentId.MEETING_AGENT)
        assert details.origin_url == PI_HOME_URL
        assert details.origin_label.startswith("Pi")
        assert details.source_url.startswith("https://nodejs.org")

    def test_gpu_links_to_nvidia_and_pypi(self):
        details = get_component_details(ComponentId.GPU_ACCEL)
        assert "nvidia" in details.origin_url
        assert "pypi.org" in details.source_url

    def test_describe_uses_the_catalog_copy(self):
        details = get_component_details(ComponentId.GPU_ACCEL)
        info = component_coordinator.describe(ComponentId.GPU_ACCEL)
        assert info.display_name == details.display_name
        assert info.summary == details.summary

    def test_catalog_entries_are_immutable(self):
        details = get_component_details(ComponentId.GPU_ACCEL)
        with pytest.raises(FrozenInstanceError):
            details.description = "changed"
        with pytest.raises(TypeError):
            COMPONENT_CATALOG["gpu-accel"] = details
