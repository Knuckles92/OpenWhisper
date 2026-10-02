from unittest.mock import patch

import pytest

from services.hf_access import _download_model_files_in_process, is_model_cached
from services.local_asr.catalog import MODELS
from services.model_catalog import MODEL_REPOSITORIES, WHISPER_REVISIONS, get_model_details
from services.settings import HuggingFaceAccessPolicy
from services.whisper_sources import cached_model_path, model_revision
from ui_qt.dialogs.hf_consent_dialog import HuggingFaceConsentDialog


def _model_folder(path):
    path.mkdir(parents=True)
    for name, data in (("model.bin", b"weights"), ("config.json", b"{}"), ("tokenizer.json", b"{}")):
        (path / name).write_bytes(data)
    return str(path)


@pytest.mark.parametrize("name", [name for name in MODEL_REPOSITORIES if name not in MODELS])
def test_every_bundled_whisper_alias_has_an_immutable_revision(name):
    revision = model_revision(name)
    assert len(revision) == 40
    assert all(c in "0123456789abcdef" for c in revision)


def test_download_and_offline_load_use_same_pinned_revision(tmp_path):
    folder = _model_folder(tmp_path / "snapshot")
    with patch("huggingface_hub.snapshot_download", return_value=folder) as download:
        assert _download_model_files_in_process("base") == folder
        assert cached_model_path("base") == folder
        assert is_model_cached("base")
    assert all(call.kwargs["revision"] == WHISPER_REVISIONS["Systran/faster-whisper-base"]
               for call in download.call_args_list)
    assert [call.kwargs["local_files_only"] for call in download.call_args_list] == [False, True, True]


def test_other_cached_revision_cannot_satisfy_catalog_pin(tmp_path):
    old = _model_folder(tmp_path / "old-snapshot")
    def cached(repo, *, revision, **kwargs):
        if revision is None:
            return old
        raise FileNotFoundError("Pinned snapshot is missing")
    with patch("huggingface_hub.snapshot_download", side_effect=cached):
        assert not is_model_cached("base")


def test_custom_source_keeps_publisher_version_and_selected_folder(tmp_path):
    _model_folder(tmp_path / "variant")
    with patch("huggingface_hub.snapshot_download", return_value=str(tmp_path)) as download:
        assert _download_model_files_in_process("owner/model/variant").endswith("variant")
        cached_model_path("owner/model/variant")
    assert all(call.kwargs["revision"] is None for call in download.call_args_list)


def test_orukeet_metadata_and_notice_identify_actual_weight_terms():
    details = get_model_details("orukeet-v0.1")
    assert details.license == "CC-BY-SA-4.0"
    assert "Oruk AI" in details.maintainer and "NVIDIA" in details.maintainer
    assert "Russian SOTA" not in MODELS["orukeet-v0.1"].label
    body = HuggingFaceConsentDialog("orukeet-v0.1", HuggingFaceAccessPolicy.ASK)._body_text()
    assert details.license_url in body
    assert "SHA-256" in body and "do not guarantee" in body


def test_whisper_notice_does_not_claim_catalog_checksum_verification():
    body = HuggingFaceConsentDialog("base", HuggingFaceAccessPolicy.ASK)._body_text()
    assert "Pinned version" in body
    assert "SHA-256" not in body


def test_moonshine_notice_identifies_actual_download_host():
    body = HuggingFaceConsentDialog("moonshine-small", HuggingFaceAccessPolicy.ASK)._body_text()
    assert "download.moonshine.ai" in body
    assert "downloaded from Hugging Face" not in body


def test_custom_source_notice_does_not_claim_review_or_fixed_version():
    body = HuggingFaceConsentDialog("owner/unknown", HuggingFaceAccessPolicy.ASK)._body_text()
    assert "has not been reviewed" in body
    assert "Selected version:" not in body


def test_downloads_offers_selected_version_when_only_old_snapshot_is_cached(monkeypatch):
    from services.hf_access import CachedModelInfo
    from ui_qt.dialogs import settings_downloads
    cached = {"Systran/faster-whisper-base": CachedModelInfo(
        "Systran/faster-whisper-base", 12, "/cache/base", ("old-revision",))}
    monkeypatch.setattr(settings_downloads, "scan_cached_models", lambda **_: cached)
    monkeypatch.setattr("services.local_asr.cache.inventory", lambda: {})
    page = settings_downloads.DownloadsPage(background_cache_scan=False)
    assert not page.rows["base"].is_cached
    assert page.rows["base"].download_button.isVisibleTo(page)
    assert page.rows["base"].download_button.isEnabled()
