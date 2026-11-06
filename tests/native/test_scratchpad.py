"""Scratchpad durability, atomic failure recovery and bounded plain-text I/O."""
import logging
import threading
from pathlib import Path

import pytest

from services.native_desktop import scratchpad


def test_existing_plain_utf8_notes_load_and_unchanged_close_preserves_bytes(tmp_path):
    path = tmp_path / "scratchpad.txt"
    original = "café\r\n日本語 🙂\n".encode()
    path.write_bytes(original)
    store = scratchpad.ScratchpadStore(path)
    assert store.text == "café\n日本語 🙂\n"
    assert store.close()
    assert path.read_bytes() == original


def test_edits_coalesce_on_background_writer_and_close_flushes_latest(tmp_path):
    path = tmp_path / "scratchpad.txt"
    writes = []
    owner = threading.get_ident()

    def write(destination, text):
        writes.append((text, threading.get_ident()))
        scratchpad._write_atomically(destination, text)

    store = scratchpad.ScratchpadStore(path, debounce=60, writer=write)
    for index in range(200):
        store.set_text(f"Newest notes {index} — 日本語")
    assert not path.exists()
    assert store.close()
    assert writes == [("Newest notes 199 — 日本語", writes[0][1])]
    assert writes[0][1] != owner
    assert path.read_text(encoding="utf-8") == "Newest notes 199 — 日本語"
    assert not store.snapshot()["dirty"]


def test_failed_atomic_replace_preserves_previous_file_and_retries_without_logging_notes(tmp_path, monkeypatch, caplog):
    path = tmp_path / "scratchpad.txt"
    path.write_text("Previously saved notes")
    store = scratchpad.ScratchpadStore(path, debounce=60)
    store.set_text("PRIVATE_NEW_NOTES_MUST_NOT_APPEAR_IN_LOGS")
    replace = scratchpad.os.replace
    monkeypatch.setattr(scratchpad.os, "replace", lambda *_args: (_ for _ in ()).throw(PermissionError("blocked")))
    with caplog.at_level(logging.WARNING):
        assert store.flush() is False
    assert path.read_text() == "Previously saved notes"
    assert store.snapshot()["dirty"] and store.snapshot()["error"]
    assert "PRIVATE_NEW_NOTES" not in caplog.text
    assert list(tmp_path.glob(".scratchpad-*.tmp")) == []
    monkeypatch.setattr(scratchpad.os, "replace", replace)
    assert store.flush()
    assert path.read_text() == "PRIVATE_NEW_NOTES_MUST_NOT_APPEAR_IN_LOGS"
    assert not store.snapshot()["dirty"] and not store.snapshot()["error"]
    assert store.close()


def test_unreadable_or_oversized_notes_are_never_replaced_with_empty_text(tmp_path):
    path = tmp_path / "scratchpad.txt"
    original = "Existing notes exceed the test limit"
    path.write_text(original)
    store = scratchpad.ScratchpadStore(path, max_chars=8)
    assert not store.snapshot()["loaded"]
    with pytest.raises(ValueError, match="preserved"):
        store.set_text("edit")
    assert not store.close()
    assert path.read_text() == original
    path.unlink()
    path.mkdir()
    unreadable = scratchpad.ScratchpadStore(path)
    assert not unreadable.snapshot()["loaded"]
    assert not unreadable.close()
    assert path.is_dir()


def test_oversized_edit_never_mutates_notes_or_pending_save(tmp_path):
    path = tmp_path / "scratchpad.txt"
    store = scratchpad.ScratchpadStore(path, max_chars=8)
    store.set_text("saved")
    with pytest.raises(ValueError, match="limited"):
        store.set_text("x" * 9)
    assert store.text == "saved"
    assert store.close()
    assert path.read_text() == "saved"


def test_autosave_finishes_while_editor_remains_open(tmp_path):
    path = tmp_path / "scratchpad.txt"
    saved = threading.Event()
    store = scratchpad.ScratchpadStore(path, debounce=.01,
        on_state=lambda state: saved.set() if not state["dirty"] else None)
    store.set_text("Automatically saved before quitting")
    assert saved.wait(3)
    assert path.read_text() == "Automatically saved before quitting"
    assert not store.snapshot()["dirty"]
    store.close()


def test_slow_older_write_cannot_overwrite_a_newer_revision(tmp_path):
    path = tmp_path / "scratchpad.txt"
    entered, release = threading.Event(), threading.Event()

    def write(destination, text):
        if text == "older":
            entered.set()
            assert release.wait(3)
        scratchpad._write_atomically(destination, text)

    store = scratchpad.ScratchpadStore(path, debounce=0, writer=write)
    store.set_text("older")
    assert entered.wait(3)
    store.set_text("newest")
    release.set()
    assert store.close()
    assert Path(path).read_text() == "newest"
