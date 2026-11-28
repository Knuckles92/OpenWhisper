"""Tests for WalkthroughRecords sync kind: bundling, import, incremental update,
listing, deletion, security validation, and end-to-end TLS sync over SpeechHost.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from services.remote_asr import settings as remote_settings
from services.remote_asr.client import RemoteConnection, pair_with_host
from services.remote_asr.host import DeviceRegistry, SpeechHost
from services.remote_asr.settings import ClientPairing
from services.remote_asr.tls import ensure_host_identity
from services.remote_records import host_store as host_store_mod
from services.remote_records import sync as sync_module
from services.remote_records import walkthrough_kind as walkthrough_kind_mod
from services.remote_records.host_store import HostRecordStore
from services.remote_records.kinds import WalkthroughRecords
from services.remote_records.sync import RecordSync
from services.walkthrough import library as wt_library
from tests.fakes.remote import FakeEngine, ListStore, no_real_tailscale  # noqa: F401

PNG_HEADER = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def _make_walkthrough(
    root: Path,
    folder_name: str = "2026-11-20_10-15-00",
    *,
    title: str = "Deploy Guide",
    text: str = "Click deploy to ship the build.",
    number: int = 1,
    with_write_up: bool = False,
    draft: bool = False,
) -> Path:
    folder = root / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    if draft:
        (folder / "draft.json").write_text(
            json.dumps({"version": 1, "started_at": "2026-11-20T10:15:00", "shots": []}),
            encoding="utf-8",
        )
        return folder
    manifest = {
        "version": 1,
        "started_at": "2026-11-20T10:15:00",
        "duration_s": 42.5,
        "number": number,
        "sections": [
            {"text": text, "shot": "shot-1.png", "kept_at_s": 5.0, "notes": []},
        ],
    }
    (folder / "walkthrough.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "walkthrough.md").write_text(f"# {title}\n\n{text}\n\n![](shot-1.png)\n", encoding="utf-8")
    (folder / "walkthrough.pdf").write_bytes(b"%PDF-1.4\n%fake\n")
    (folder / "walkthrough.docx").write_bytes(b"PK\x03\x04fake-docx")
    (folder / "shot-1.png").write_bytes(PNG_HEADER)
    if with_write_up:
        (folder / "write-up.md").write_text(f"# {title} (Polished)\n\nRefined guide.\n", encoding="utf-8")
        (folder / "write-up.pdf").write_bytes(b"%PDF-1.4\n%polished\n")
        (folder / "write-up.docx").write_bytes(b"PK\x03\x04polished-docx")
    return folder


def _stage_bundle(kind: WalkthroughRecords, origin, record_id: str, staging_dir: Path):
    bundle = kind.bundle(origin, record_id)
    for rel_name, src in bundle.files.items():
        dst = staging_dir / Path(*rel_name.split("/"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    return bundle


def test_walkthrough_records_local_lifecycle_and_digest(tmp_path):
    captures = tmp_path / "captures"
    rec_id = "2026-11-20_10-15-00"
    draft_id = "2026-11-20_10-20-00"
    folder = _make_walkthrough(captures, rec_id)
    _make_walkthrough(captures, draft_id, draft=True)
    # Stray disallowed file should be ignored by bundle.
    (folder / "audio.wav").write_bytes(b"RIFF....WAVE")
    (folder / "notes.exe").write_bytes(b"MZ")

    busy = {"flag": False}
    wt = WalkthroughRecords(str(captures), in_use=lambda rid: busy["flag"] and rid == rec_id)

    assert wt.kind == "walkthrough"
    assert wt.exists(rec_id) is True
    assert wt.exists(draft_id) is False
    assert wt.local_ids() == [rec_id]
    assert wt.ready(rec_id) is None
    assert wt.ready(draft_id) == "it hasn't finished"

    busy["flag"] = True
    assert wt.ready(rec_id) == "it is currently open"
    busy["flag"] = False

    bundle = wt.bundle(None, rec_id)
    assert set(bundle.files) == {
        "files/walkthrough.json",
        "files/walkthrough.md",
        "files/walkthrough.pdf",
        "files/walkthrough.docx",
        "files/shot-1.png",
    }
    assert bundle.record["kind"] == "walkthrough"
    assert bundle.record["metadata"]["title"] == "Deploy Guide"
    assert bundle.record["metadata"]["shot_count"] == 1

    d1 = wt.digest(rec_id)
    (folder / "walkthrough.md").write_text("# Deploy Guide\n\nEdited text.\n", encoding="utf-8")
    d2 = wt.digest(rec_id)
    assert d1 != d2

    wt.delete_local(rec_id)
    assert wt.exists(rec_id) is False
    assert not folder.exists()


def test_walkthrough_records_import_update_list_owners_and_delete(tmp_path):
    src_root = tmp_path / "client_captures"
    host_root = tmp_path / "host_captures"
    rec_id = "2026-11-20_11-00-00"
    rec_id2 = "2026-11-20_12-00-00"
    folder1 = _make_walkthrough(src_root, rec_id, title="Billing Setup", text="Open billing page.")
    _make_walkthrough(src_root, rec_id2, title="SSO Login", text="Configure SAML.", number=2)

    client_wt = WalkthroughRecords(str(src_root))
    host_wt = WalkthroughRecords(str(host_root))
    device = {"id": "dev-laptop-1", "name": "Work Laptop"}

    # Full import of both walkthroughs
    for rid in (rec_id, rec_id2):
        stage = tmp_path / f"stage-{rid}"
        bundle = _stage_bundle(client_wt, None, rid, stage)
        res = host_wt.import_record(rid, str(stage), bundle.record, device)
        assert res["files"] == 5

    # Host's own walkthrough library listing must NOT see paired devices' folders!
    assert host_wt.local_ids() == []
    assert wt_library.entries(host_root) == []

    # Ownership, summary, stored_files, list & search
    assert host_wt.owns("dev-laptop-1", rec_id) is True
    assert host_wt.owns("other-device", rec_id) is False
    assert host_wt.owners() == [{"id": "dev-laptop-1", "name": "Work Laptop", "count": 2}]
    summary = host_wt.summary("dev-laptop-1")
    assert summary["count"] == 2 and summary["bytes"] > 0

    stored = host_wt.stored_files("dev-laptop-1", rec_id)
    assert "files/shot-1.png" in stored and stored["files/shot-1.png"] == len(PNG_HEADER)

    listed_all = host_wt.list("dev-laptop-1", "", 10)
    assert [item["id"] for item in listed_all] == [rec_id2, rec_id]
    listed_search = host_wt.list("dev-laptop-1", "billing", 10)
    assert [item["id"] for item in listed_search] == [rec_id]

    # Incremental update: add write-up files without re-staging shot-1.png
    (folder1 / "write-up.md").write_text("# Billing Setup Guide\n\nPolished.", encoding="utf-8")
    (folder1 / "write-up.pdf").write_bytes(b"%PDF-1.4\n%wu\n")
    update_bundle = client_wt.bundle(None, rec_id)
    update_bundle.record["update"] = True
    stage_upd = tmp_path / "stage-upd"
    for rel_name, src in update_bundle.files.items():
        if rel_name == "files/shot-1.png":
            continue  # kept on host!
        dst = stage_upd / Path(*rel_name.split("/"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    upd_res = host_wt.import_record(rec_id, str(stage_upd), update_bundle.record, device)
    assert upd_res["files"] == 7
    assert host_wt.stored_files("dev-laptop-1", rec_id)["files/write-up.md"] > 0
    assert host_wt.stored_files("dev-laptop-1", rec_id)["files/shot-1.png"] == len(PNG_HEADER)

    # Export and rename_owner
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    exported_files = host_wt.export("dev-laptop-1", rec_id, str(export_dir))
    assert (export_dir / "record.json").is_file()
    assert "files/write-up.md" in exported_files

    assert host_wt.rename_owner("dev-laptop-1", "Renamed Laptop") == 2
    assert host_wt.owners() == [{"id": "dev-laptop-1", "name": "Renamed Laptop", "count": 2}]

    # Delete single and delete_all
    assert host_wt.delete("dev-laptop-1", rec_id) is True
    assert host_wt.delete("dev-laptop-1", rec_id) is False
    assert host_wt.owns("dev-laptop-1", rec_id) is False
    assert host_wt.delete_all("dev-laptop-1") == 1
    assert host_wt.owners() == []


def test_walkthrough_records_security_rejects_traversal_and_disallowed_files(tmp_path):
    host_root = tmp_path / "host_captures"
    host_wt = WalkthroughRecords(str(host_root))
    device = {"id": "dev-1", "name": "Laptop"}

    def _stage_one(filename: str, content: bytes):
        stage = tmp_path / f"bad-{abs(hash((filename, content)))}"
        files_dir = stage / "files"
        files_dir.mkdir(parents=True, exist_ok=True)
        (files_dir / "walkthrough.json").write_text(
            json.dumps({"version": 1, "started_at": "2026-11-20T10:00:00", "duration_s": 1.0, "sections": []}),
            encoding="utf-8",
        )
        if "/" not in filename and "\\" not in filename and filename not in ("..", "."):
            (files_dir / filename).write_bytes(content)
        return stage

    # Disallowed extensions
    for bad_ext in ("payload.exe", "script.py", "audio.wav", "draft.json", "shell.sh"):
        stage = _stage_one(bad_ext, b"evil")
        rec = {
            "kind": "walkthrough",
            "format": 1,
            "id": "2026-11-20_10-00-00",
            "files": {
                "files/walkthrough.json": os.path.getsize(stage / "files" / "walkthrough.json"),
                f"files/{bad_ext}": 4,
            },
        }
        with pytest.raises(ValueError):
            host_wt.import_record("2026-11-20_10-00-00", str(stage), rec, device)

    # Path traversal in file names and record ids
    stage = _stage_one("ok.md", b"ok")
    for bad_rel in ("../escape.md", "files/../escape.md", "other/walkthrough.json", "files/sub/a.md", "files/.hidden.json"):
        rec = {
            "kind": "walkthrough",
            "format": 1,
            "id": "2026-11-20_10-00-00",
            "files": {
                "files/walkthrough.json": os.path.getsize(stage / "files" / "walkthrough.json"),
                bad_rel: 2,
            },
        }
        with pytest.raises(ValueError):
            host_wt.import_record("2026-11-20_10-00-00", str(stage), rec, device)

    for bad_rid in ("../outside", "a/b", ".hidden", "C:/evil", ""):
        rec = {
            "kind": "walkthrough",
            "format": 1,
            "id": bad_rid,
            "files": {"files/walkthrough.json": 10},
        }
        with pytest.raises(ValueError):
            host_wt.import_record(bad_rid, str(stage), rec, device)

    # Missing walkthrough.json
    rec_no_manifest = {
        "kind": "walkthrough",
        "format": 1,
        "id": "2026-11-20_10-00-00",
        "files": {"files/ok.md": 2},
    }
    with pytest.raises(ValueError, match="walkthrough.json"):
        host_wt.import_record("2026-11-20_10-00-00", str(stage), rec_no_manifest, device)

    # Corrupt walkthrough.json rolls back without clobbering an existing valid record
    valid_src = tmp_path / "valid_src"
    _make_walkthrough(valid_src, "2026-11-20_10-00-00", title="Original")
    valid_stage = tmp_path / "valid_stage"
    valid_bundle = _stage_bundle(WalkthroughRecords(str(valid_src)), None, "2026-11-20_10-00-00", valid_stage)
    host_wt.import_record("2026-11-20_10-00-00", str(valid_stage), valid_bundle.record, device)

    corrupt_stage = tmp_path / "corrupt_stage"
    (corrupt_stage / "files").mkdir(parents=True)
    (corrupt_stage / "files" / "walkthrough.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError):
        host_wt.import_record(
            "2026-11-20_10-00-00",
            str(corrupt_stage),
            {
                "kind": "walkthrough",
                "format": 1,
                "id": "2026-11-20_10-00-00",
                "files": {"files/walkthrough.json": 9},
            },
            device,
        )
    # Original walkthrough survived untouched!
    assert host_wt.list("dev-1", "", 10)[0]["title"] == "Original"


def test_walkthrough_records_end_to_end_over_tls_speech_host(tmp_path, monkeypatch):
    """Real TLS SpeechHost + HostRecordStore + RecordSync round-trip for walkthroughs."""
    monkeypatch.setattr(
        host_store_mod, "RECORD_KINDS", ("dictation", "meeting", "walkthrough")
    )

    host_captures = tmp_path / "host_captures"
    client_captures = tmp_path / "client_captures"
    rec_id = "2026-11-20_14-30-00"
    _make_walkthrough(client_captures, rec_id, title="VPN Setup", with_write_up=True)

    store = HostRecordStore(
        str(tmp_path / "remote_records"),
        {"walkthrough": WalkthroughRecords(str(host_captures))},
    )
    devices = ListStore()
    server = SpeechHost(
        engine_provider=lambda: FakeEngine(),
        registry=DeviceRegistry(devices.load, devices.save),
        identity=ensure_host_identity(str(tmp_path / "identity")),
        host_name="devbox",
        records_enabled=lambda: True,
        records=store.handle,
        records_summary=store.summary,
    )
    server.start(port=0, bind="127.0.0.1")
    try:
        paired = pair_with_host("127.0.0.1", server.port, server.open_pairing(), "laptop")
        pairing = ClientPairing(
            "127.0.0.1", server.port, paired.fingerprint, "devbox", device_id=paired.device_id
        )
        monkeypatch.setattr(remote_settings, "load_client_pairing", lambda settings=None: pairing)
        monkeypatch.setattr(remote_settings, "load_client_token", lambda: paired.token)

        sync = RecordSync(
            {"walkthrough": WalkthroughRecords(str(client_captures))},
            connect=lambda: RemoteConnection(
                "127.0.0.1", server.port, paired.token, paired.fingerprint
            ),
            cache_dir=str(tmp_path / "cache"),
        )
        monkeypatch.setattr(sync_module.record_sync, "_instance", sync)
        try:
            sync.set_location("host")
            sync.record_saved("walkthrough", rec_id)
            sync.run_once()

            # Moved off client onto host
            assert not (client_captures / rec_id).exists()
            host_wt = store.kinds["walkthrough"]
            assert host_wt.owns(paired.device_id, rec_id) is True
            listed = sync.list_remote("walkthrough")
            assert len(listed) == 1
            assert listed[0]["id"] == rec_id
            assert listed[0]["written_up"] is True

            # Delete remote removes it cleanly
            assert sync.delete_remote("walkthrough", rec_id) is True
            assert sync.list_remote("walkthrough") == []
        finally:
            sync.stop()
    finally:
        server.stop()


def _snapshot_folder(folder: Path) -> dict[str, bytes]:
    return {
        entry.name: entry.read_bytes()
        for entry in sorted(folder.iterdir())
        if entry.is_file()
    }


def _leftover_temp_dirs(parent: Path) -> list[str]:
    return [
        entry.name
        for entry in parent.iterdir()
        if entry.name.startswith((".staging-", ".deleting-"))
    ]


def _prepare_update_fixture(tmp_path: Path):
    src_root = tmp_path / "client_captures"
    host_root = tmp_path / "host_captures"
    rec_id = "2026-11-20_16-00-00"
    folder = _make_walkthrough(src_root, rec_id, title="Original Guide", text="Step one.")
    client_wt = WalkthroughRecords(str(src_root))
    host_wt = WalkthroughRecords(str(host_root))
    device = {"id": "dev-atomic", "name": "Laptop"}

    initial_stage = tmp_path / "stage-init"
    init_bundle = _stage_bundle(client_wt, None, rec_id, initial_stage)
    host_wt.import_record(rec_id, str(initial_stage), init_bundle.record, device)

    dest = host_root / "devices" / "dev-atomic" / rec_id
    before = _snapshot_folder(dest)

    # Prepare an incremental update (modify walkthrough.md, add write-up.md, keep shot-1.png)
    (folder / "walkthrough.md").write_text("# Updated Guide\n\nStep one revised.\n", encoding="utf-8")
    (folder / "write-up.md").write_text("# Updated Guide (Polished)\n\nRefined.\n", encoding="utf-8")
    upd_bundle = client_wt.bundle(None, rec_id)
    upd_bundle.record["update"] = True
    stage_upd = tmp_path / "stage-upd"
    for rel_name, src in upd_bundle.files.items():
        if rel_name in ("files/shot-1.png", "files/walkthrough.pdf", "files/walkthrough.docx"):
            continue
        dst = stage_upd / Path(*rel_name.split("/"))
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    return host_wt, device, rec_id, dest, before, stage_upd, upd_bundle


def test_walkthrough_update_rolls_back_atomically_on_mid_move_failure(tmp_path, monkeypatch):
    host_wt, device, rec_id, dest, before, stage_upd, upd_bundle = _prepare_update_fixture(tmp_path)

    real_move = walkthrough_kind_mod._move
    calls = {"n": 0}

    def flaky_move(src: str, dst: str) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated mid-update move failure")
        real_move(src, dst)

    monkeypatch.setattr(walkthrough_kind_mod, "_move", flaky_move)

    with pytest.raises(OSError, match="mid-update move failure"):
        host_wt.import_record(rec_id, str(stage_upd), upd_bundle.record, device)

    assert _snapshot_folder(dest) == before
    assert _leftover_temp_dirs(dest.parent) == []


def test_walkthrough_update_rolls_back_atomically_on_post_swap_failure(tmp_path, monkeypatch):
    host_wt, device, rec_id, dest, before, stage_upd, upd_bundle = _prepare_update_fixture(tmp_path)

    real_read = walkthrough_kind_mod._read_walkthrough_manifest
    calls = {"n": 0}

    def fail_on_installed_manifest(folder: str) -> dict:
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("simulated post-swap manifest corruption")
        return real_read(folder)

    monkeypatch.setattr(walkthrough_kind_mod, "_read_walkthrough_manifest", fail_on_installed_manifest)

    with pytest.raises(ValueError, match="post-swap manifest corruption"):
        host_wt.import_record(rec_id, str(stage_upd), upd_bundle.record, device)

    assert _snapshot_folder(dest) == before
    assert _leftover_temp_dirs(dest.parent) == []


def test_walkthrough_update_rolls_back_when_final_replace_fails(tmp_path, monkeypatch):
    host_wt, device, rec_id, dest, before, stage_upd, upd_bundle = _prepare_update_fixture(tmp_path)

    real_replace = os.replace
    failed_once = {"done": False}

    def flaky_replace(src: str, dst: str) -> None:
        if (
            not failed_once["done"]
            and os.path.basename(src).startswith(".staging-")
            and os.path.abspath(dst) == os.path.abspath(str(dest))
        ):
            failed_once["done"] = True
            raise OSError("simulated final swap failure")
        real_replace(src, dst)

    monkeypatch.setattr(walkthrough_kind_mod.os, "replace", flaky_replace)

    with pytest.raises(OSError, match="final swap failure"):
        host_wt.import_record(rec_id, str(stage_upd), upd_bundle.record, device)

    assert _snapshot_folder(dest) == before
    assert _leftover_temp_dirs(dest.parent) == []


def test_walkthrough_update_drops_stale_files_and_keeps_unchanged(tmp_path):
    src_root = tmp_path / "client_captures"
    host_root = tmp_path / "host_captures"
    rec_id = "2026-11-20_17-00-00"
    folder = _make_walkthrough(src_root, rec_id, title="Stale Check", with_write_up=True)

    client_wt = WalkthroughRecords(str(src_root))
    host_wt = WalkthroughRecords(str(host_root))
    device = {"id": "dev-stale", "name": "Laptop"}

    init_stage = tmp_path / "stage-init"
    init_bundle = _stage_bundle(client_wt, None, rec_id, init_stage)
    assert host_wt.import_record(rec_id, str(init_stage), init_bundle.record, device)["files"] == 8

    # Remove write-up artifacts on client and edit walkthrough.md; keep shot-1.png & docs on host
    for name in ("write-up.md", "write-up.pdf", "write-up.docx"):
        (folder / name).unlink()
    (folder / "walkthrough.md").write_text("# Stale Check\n\nCleaned up.\n", encoding="utf-8")

    upd_bundle = client_wt.bundle(None, rec_id)
    upd_bundle.record["update"] = True
    stage_upd = tmp_path / "stage-upd"
    dst_md = stage_upd / "files" / "walkthrough.md"
    dst_md.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(folder / "walkthrough.md", dst_md)

    res = host_wt.import_record(rec_id, str(stage_upd), upd_bundle.record, device)
    assert res["files"] == 5

    dest = host_root / "devices" / "dev-stale" / rec_id
    assert set(host_wt.stored_files("dev-stale", rec_id)) == {
        "files/walkthrough.json",
        "files/walkthrough.md",
        "files/walkthrough.pdf",
        "files/walkthrough.docx",
        "files/shot-1.png",
    }
    assert not (dest / "write-up.md").exists()
    assert not (dest / "write-up.pdf").exists()
    assert not (dest / "write-up.docx").exists()
    assert (dest / "shot-1.png").read_bytes() == PNG_HEADER
    assert "Cleaned up." in (dest / "walkthrough.md").read_text(encoding="utf-8")
    assert _leftover_temp_dirs(dest.parent) == []
