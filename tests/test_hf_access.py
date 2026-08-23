"""Unit tests for the Hugging Face cache/access coordinator."""
import pytest
import os
import tempfile
import types
from unittest.mock import MagicMock, patch

from services.hf_access import (
    AccessDecision,
    CachedModelInfo,
    HuggingFaceAccessCoordinator,
    _progress_tqdm_class,
    delete_model_from_cache,
    download_model_files,
    format_download_size,
    format_size_bytes,
    is_model_cached,
    resolve_model_repo,
    scan_cached_models,
)
from services.settings import HuggingFaceAccessPolicy


def _fake_repo(repo_id, size_on_disk, revisions, repo_type="model"):
    return types.SimpleNamespace(
        repo_id=repo_id,
        repo_type=repo_type,
        size_on_disk=size_on_disk,
        repo_path=f"/hub/models--{repo_id.replace('/', '--')}",
        revisions=[types.SimpleNamespace(commit_hash=h) for h in revisions],
    )


class TestHelpers:
    def test_format_download_size_known_models(self):
        assert format_download_size("base") == "~145 MB"
        assert format_download_size("turbo") == "~1.6 GB"

    def test_format_download_size_unknown_model(self):
        assert format_download_size("some/custom-repo") is None

    def test_resolve_model_repo(self):
        assert resolve_model_repo("base") == "Systran/faster-whisper-base"
        # Unknown names (custom repos, paths) pass through unchanged
        assert resolve_model_repo("me/my-model") == "me/my-model"

    def test_is_model_cached_local_directory(self):
        """A local model directory counts as cached without any lookup."""
        with tempfile.TemporaryDirectory() as tmp:
            assert is_model_cached(tmp)

    def test_progress_tqdm_reports_bytes(self):
        events = []
        tqdm_cls = _progress_tqdm_class(lambda done, total: events.append((done, total)))
        bar = tqdm_cls(total=100)
        bar.update(40)
        bar.update(60)
        bar.close()
        assert events[0] == (0, 100)
        assert events[-1] == (100, 100)

    def test_download_model_files_forwards_progress_and_allow_patterns(self):
        captured = {}

        def fake_snapshot(repo_id, **kwargs):
            captured["repo_id"] = repo_id
            captured["kwargs"] = kwargs
            return "/cache/base"

        with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot), patch(
            "faster_whisper.utils._MODELS", {"base": "Systran/faster-whisper-base"}
        ):
            path = download_model_files("base", progress_callback=lambda *_: None)

        assert path == "/cache/base"
        assert captured["repo_id"] == "Systran/faster-whisper-base"
        assert "model.bin" in captured["kwargs"]["allow_patterns"]
        assert captured["kwargs"]["local_files_only"] is False
        assert captured["kwargs"]["tqdm_class"] is not None


@patch("services.hf_access.is_hf_hub_offline_env_set", return_value=False)
@patch("services.hf_access.is_model_cached", return_value=False)
class TestEvaluateAccess:
    """Policy/grant/env evaluation for a model missing from the cache."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.coordinator = HuggingFaceAccessCoordinator()

    def _set_policy(self, policy):
        self.coordinator.get_policy = lambda: policy

    def test_cached_model_always_loads_locally(self, mock_cached, _mock_env):
        mock_cached.return_value = True
        for policy in HuggingFaceAccessPolicy.ALL:
            self._set_policy(policy)
            assert self.coordinator.evaluate_access("base") == AccessDecision.LOAD_CACHED

    def test_ask_policy_needs_consent(self, _mock_cached, _mock_env):
        self._set_policy(HuggingFaceAccessPolicy.ASK)
        assert self.coordinator.evaluate_access("base") == AccessDecision.NEEDS_CONSENT

    def test_never_policy_needs_consent(self, _mock_cached, _mock_env):
        """'never' still surfaces the dialog (with its Download once override)."""
        self._set_policy(HuggingFaceAccessPolicy.NEVER)
        assert self.coordinator.evaluate_access("base") == AccessDecision.NEEDS_CONSENT

    def test_always_policy_allows_download(self, _mock_cached, _mock_env):
        self._set_policy(HuggingFaceAccessPolicy.ALWAYS)
        assert self.coordinator.evaluate_access("base") == AccessDecision.DOWNLOAD_ALLOWED

    def test_env_override_blocks_even_with_grant(self, _mock_cached, mock_env):
        mock_env.return_value = True
        self._set_policy(HuggingFaceAccessPolicy.ALWAYS)
        self.coordinator.grant_once("base")
        assert self.coordinator.evaluate_access("base") == AccessDecision.BLOCKED_BY_ENV

    def test_one_time_grant_is_consumed_once(self, _mock_cached, _mock_env):
        self._set_policy(HuggingFaceAccessPolicy.ASK)
        self.coordinator.grant_once("base")

        assert self.coordinator.evaluate_access("base") == AccessDecision.DOWNLOAD_ALLOWED
        # Grant is spent — a second request needs fresh consent
        assert self.coordinator.evaluate_access("base") == AccessDecision.NEEDS_CONSENT

    def test_advisory_check_preserves_grant(self, _mock_cached, _mock_env):
        self._set_policy(HuggingFaceAccessPolicy.ASK)
        self.coordinator.grant_once("base")

        assert self.coordinator.evaluate_access("base", consume_grant=False) == AccessDecision.DOWNLOAD_ALLOWED
        # Grant survives the advisory check and is consumed here
        assert self.coordinator.evaluate_access("base") == AccessDecision.DOWNLOAD_ALLOWED

    def test_grant_applies_only_to_that_model(self, _mock_cached, _mock_env):
        self._set_policy(HuggingFaceAccessPolicy.ASK)
        self.coordinator.grant_once("base")
        assert self.coordinator.evaluate_access("small") == AccessDecision.NEEDS_CONSENT


class TestFormatSizeBytes:
    """Human-readable formatting of actual on-disk sizes."""

    def test_boundaries(self):
        assert format_size_bytes(512) == "512 B"
        assert format_size_bytes(145_000_000) == "145 MB"
        assert format_size_bytes(1_530_000_000) == "1.53 GB"
        assert format_size_bytes(12_000) == "12 KB"


class TestScanCachedModels:
    """Cache enumeration via huggingface_hub.scan_cache_dir."""

    def test_maps_repos_by_repo_id(self):
        cache_info = types.SimpleNamespace(
            repos=[
                _fake_repo("Systran/faster-whisper-base", 145_000_000, ["abc"]),
                _fake_repo(
                    "Systran/faster-whisper-large-v3", 3_090_000_000, ["d1", "d2"]
                ),
                _fake_repo("some/dataset", 10, ["x"], repo_type="dataset"),
            ]
        )
        with patch("huggingface_hub.scan_cache_dir", return_value=cache_info):
            cached = scan_cached_models()

        assert set(cached) == {
            "Systran/faster-whisper-base",
            "Systran/faster-whisper-large-v3",
        }
        base = cached["Systran/faster-whisper-base"]
        assert isinstance(base, CachedModelInfo)
        assert base.size_bytes == 145_000_000
        assert cached["Systran/faster-whisper-large-v3"].revision_hashes == ("d1", "d2")

    def test_missing_cache_dir_returns_empty(self):
        with patch(
            "huggingface_hub.scan_cache_dir",
            side_effect=Exception("cache not found"),
        ):
            assert scan_cached_models() == {}


class TestDeleteModelFromCache:
    """Deletion routes through huggingface_hub's delete strategy."""

    def _cached_base(self):
        return {
            "Systran/faster-whisper-base": CachedModelInfo(
                repo_id="Systran/faster-whisper-base",
                size_bytes=145_000_000,
                path="/hub/models--Systran--faster-whisper-base",
                revision_hashes=("abc", "def"),
            )
        }

    def test_deletes_all_revisions_of_resolved_repo(self):
        strategy = MagicMock()
        strategy.expected_freed_size = 145_000_000
        cache_info = MagicMock()
        cache_info.delete_revisions.return_value = strategy

        with patch(
            "services.hf_access.scan_cached_models",
            return_value=self._cached_base(),
        ), patch("huggingface_hub.scan_cache_dir", return_value=cache_info):
            delete_model_from_cache("base")

        cache_info.delete_revisions.assert_called_once_with("abc", "def")
        strategy.execute.assert_called_once_with()

    def test_uncached_model_raises_value_error(self):
        with patch("services.hf_access.scan_cached_models", return_value={}):
            with pytest.raises(ValueError):
                delete_model_from_cache("base")

    def test_permission_error_propagates(self):
        strategy = MagicMock()
        strategy.expected_freed_size = 145_000_000
        strategy.execute.side_effect = PermissionError("file locked")
        cache_info = MagicMock()
        cache_info.delete_revisions.return_value = strategy

        with patch(
            "services.hf_access.scan_cached_models",
            return_value=self._cached_base(),
        ), patch("huggingface_hub.scan_cache_dir", return_value=cache_info):
            with pytest.raises(PermissionError):
                delete_model_from_cache("base")


class TestRequestDeduplication:
    """Only one consent dialog / download may exist per model."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.coordinator = HuggingFaceAccessCoordinator()

    def test_begin_request_claims_and_rejects_duplicates(self):
        assert self.coordinator.begin_request("base")
        assert not self.coordinator.begin_request("base")
        # A different model gets its own slot
        assert self.coordinator.begin_request("small")

    def test_end_request_releases_slot(self):
        assert self.coordinator.begin_request("base")
        self.coordinator.end_request("base")
        assert self.coordinator.begin_request("base")

    def test_end_request_for_unclaimed_model_is_harmless(self):
        self.coordinator.end_request("never-claimed")
        assert self.coordinator.begin_request("never-claimed")


@patch("services.hf_access.is_model_cached", return_value=False)
class TestClaimBatch:
    """Batch planning: skip cached/duplicate/in-flight models, hold slots."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.coordinator = HuggingFaceAccessCoordinator()

    def test_claims_every_downloadable_model(self, _mock_cached):
        claimed = self.coordinator.claim_batch(["tiny", "base", "small"])
        assert claimed == ["tiny", "base", "small"]
        # Slots stay held so a concurrent request cannot double-download.
        assert not self.coordinator.begin_request("base")

    def test_skips_cached_models(self, mock_cached):
        mock_cached.side_effect = lambda name: name == "tiny"
        assert self.coordinator.claim_batch(["tiny", "base"]) == ["base"]

    def test_skips_duplicates_in_the_request(self, _mock_cached):
        assert self.coordinator.claim_batch(["tiny", "tiny"]) == ["tiny"]

    def test_skips_models_already_in_flight(self, _mock_cached):
        assert self.coordinator.begin_request("base")
        assert self.coordinator.claim_batch(["base", "tiny"]) == ["tiny"]

    def test_requests_in_flight_counts_claimed_slots(self, _mock_cached):
        assert self.coordinator.requests_in_flight == 0
        self.coordinator.claim_batch(["tiny", "base"])
        assert self.coordinator.requests_in_flight == 2
        self.coordinator.end_request("tiny")
        assert self.coordinator.requests_in_flight == 1


