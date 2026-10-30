"""Saved walkthrough review, recovery, retry, retention and file-boundary safety."""
import json
import math
import os
import subprocess
import sys
import threading
import time
import wave
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QColor, QImage

from services.walkthrough import document, finish, library, write_up
from services.walkthrough.session import Note, WalkthroughSession


@pytest.fixture
def capture(tmp_path):
    folder = tmp_path / "captures" / "2026-10-21_14-32-05"
    folder.mkdir(parents=True)
    session = WalkthroughSession(started_at=datetime(2026, 10, 21, 14, 32, 5),
                                  make_folder=lambda _: folder)
    image = QImage(120, 80, QImage.Format.Format_RGB32)
    image.fill(QColor("#f84f30"))
    session.add_shot(image, 1.0)
    session.add_shot(image, 2.0)
    shots = session.saved_shots()
    result = finish.build_documents(session, shots, ["first", "second", "last"],
                                     ["first", "second", "last"], duration_s=3.0)
    session.end()
    return SimpleNamespace(folder=folder, root=folder.parent, result=result)


def noted_capture(tmp_path):
    """Two screenshots, a note held before the second and one after the last, said alone."""
    folder = tmp_path / "captures" / "2026-10-21_14-32-05"
    folder.mkdir(parents=True)
    session = WalkthroughSession(started_at=datetime(2026, 10, 21, 14, 32, 5),
                                  make_folder=lambda _: folder)
    image = QImage(120, 80, QImage.Format.Format_RGB32)
    image.fill(QColor("#f84f30"))
    session.add_shot(image, 1.0)
    session.add_shot(image, 2.0)
    shots = session.saved_shots()
    finish.build_documents(session, shots, ["first", "second", ""], ["first", "second", ""],
                           duration_s=3.0,
                           notes=[Note("make it purple", 1), Note("sign off warmly", 2)])
    session.end()
    return folder


def _audio(path):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 48000)


def test_result_round_trip_rebuilds_paths_for_the_current_machine(capture, tmp_path):
    import shutil
    moved = tmp_path / "other-machine" / capture.folder.name
    shutil.copytree(capture.folder, moved)
    result = library.load_result(moved)
    assert result.folder == moved
    assert str(capture.folder) not in result.clipboard_text
    assert str(moved).replace("\\", "/") in result.clipboard_text.replace("\\", "/")
    assert result.history_text == "first\n\n[Screenshot 1]\n\nsecond\n\n[Screenshot 2]\n\nlast"
    assert result.shots == 2


def test_review_edits_words_and_omits_screenshot_but_keeps_the_original(capture):
    result = library.save_review(capture.folder, ["fixed", "next", "tail"], [False, True, False])
    saved = library.load_document(capture.folder)
    assert [s.text for s in saved.sections] == ["fixed", "next", "tail"]
    assert [s.shot.name if s.shot else None for s in saved.sections] == [None, "shot-2.png", None]
    assert result.shots == 1
    assert result.history_text == "fixed\n\nnext\n\n[Screenshot 1]\n\ntail"
    assert (capture.folder / "shot-1.png").is_file()
    original = library.read_json(capture.folder / "walkthrough.original.json")
    assert original["sections"][0]["text"] == "first"
    assert "fixed" in result.clipboard_text
    assert "shot-1.png" not in result.clipboard_text
    assert result.pdf.read_bytes().startswith(b"%PDF")
    library.save_review(capture.folder, ["again", "next", "tail"], [False, True, False])
    assert library.read_json(capture.folder / "walkthrough.original.json") == original


def test_review_can_include_an_omitted_screenshot_again(capture):
    library.save_review(capture.folder, ["first", "", "last"], [True, False, True])
    saved = library.load_document(capture.folder)
    # The emptied section survives so its screenshot can come back.
    assert len(saved.sections) == 3
    assert saved.sections[1].shot is None
    assert saved.sections[1].omitted.name == "shot-2.png"
    assert saved.sections[1].kept_at_s == 2.0
    manifest = library.read_json(capture.folder / document.MANIFEST_NAME)
    assert manifest["sections"][1] == {"text": "", "shot": None, "kept_at_s": 2.0,
                                       "omitted_shot": "shot-2.png"}
    assert "shot-2.png" not in (capture.folder / document.MARKDOWN_NAME).read_text()
    result = library.save_review(capture.folder, ["first", "back", "last"], [True, True, True])
    saved = library.load_document(capture.folder)
    assert [s.shot.name if s.shot else None for s in saved.sections] == ["shot-1.png", "shot-2.png", None]
    assert saved.sections[1].omitted is None
    assert result.shots == 2
    assert "omitted_shot" not in json.dumps(library.read_json(capture.folder / document.MANIFEST_NAME))


def test_a_deleted_omitted_screenshot_does_not_break_the_document(capture):
    library.save_review(capture.folder, ["first", "second", "last"], [False, True, True])
    (capture.folder / "shot-1.png").unlink()
    saved = library.load_document(capture.folder)
    assert saved.sections[0].shot is None and saved.sections[0].omitted is None


def test_review_failure_keeps_existing_documents_and_write_up(capture, monkeypatch):
    before = {p.name: p.read_bytes() for p in capture.folder.iterdir()}
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(document, "write_pdf", fail)
    with pytest.raises(OSError, match="disk full"):
        library.save_review(capture.folder, ["new", "next", "last"], [True] * 3)
    assert {p.name: p.read_bytes() for p in capture.folder.iterdir()} == before


def test_failed_publication_rolls_back_documents_and_write_up(capture, monkeypatch):
    (capture.folder / "write-up.md").write_text("old Markdown")
    (capture.folder / "write-up.pdf").write_bytes(b"old PDF")
    before = {p.name: p.read_bytes() for p in capture.folder.iterdir()}
    real_replace = library.os.replace
    failed = False
    def fail_once(source, target):
        nonlocal failed
        if not failed and Path(source).name == "walkthrough.pdf":
            failed = True
            raise PermissionError("PDF is locked")
        return real_replace(source, target)
    monkeypatch.setattr(library.os, "replace", fail_once)
    with pytest.raises(PermissionError, match="locked"):
        library.save_review(capture.folder, ["new", "next", "last"], [True] * 3)
    for name, contents in before.items():
        assert (capture.folder / name).read_bytes() == contents


def test_review_invalidates_a_previous_write_up(capture):
    from docx import Document

    (capture.folder / "write-up.md").write_text("old write-up")
    (capture.folder / "write-up.pdf").write_bytes(b"old PDF")
    (capture.folder / "write-up.docx").write_bytes(b"old Word document")
    result = library.save_review(capture.folder, ["new", "next", "last"], [True] * 3)
    assert not result.written_up
    assert not (capture.folder / "write-up.pdf").exists()
    assert not (capture.folder / "write-up.docx").exists()
    assert result.docx == capture.folder / "walkthrough.docx"
    assert "new" in [paragraph.text for paragraph in Document(str(result.docx)).paragraphs]
    assert library.word_document(capture.folder) == result.docx


def test_review_rejects_stale_editor_shape(capture):
    with pytest.raises(ValueError, match="changed"):
        library.save_review(capture.folder, ["one"], [True])


@pytest.mark.parametrize("name", ["../outside.png", "C:\\outside.png", "/tmp/outside.png", "walkthrough.pdf"])
def test_manifest_cannot_reference_other_files(capture, name):
    path = capture.folder / document.MANIFEST_NAME
    value = library.read_json(path)
    value["sections"][0]["shot"] = name
    library.atomic_json(path, value)
    with pytest.raises(ValueError):
        library.load_document(capture.folder)


@pytest.mark.parametrize("bad", [float("inf"), -1, "NaN"])
def test_manifest_rejects_invalid_times(capture, bad):
    path = capture.folder / document.MANIFEST_NAME
    value = library.read_json(path)
    value["duration_s"] = bad
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        library.load_document(capture.folder)


def test_retry_uses_the_spoken_document_and_current_agent_settings(capture, monkeypatch):
    import services.settings as settings
    called = []
    monkeypatch.setattr(settings, "resolve_walkthrough_write_up_agent", lambda _: "codex")
    monkeypatch.setattr(settings, "resolve_walkthrough_write_up_instructions", lambda _: "Make steps")
    monkeypatch.setattr(settings, "resolve_walkthrough_write_up_notes", lambda _: True)
    def run(result, shots, **kwargs):
        called.append((result, shots, kwargs))
        return replace(result, written_up=True)
    monkeypatch.setattr(write_up, "write_up", run)
    cancel = threading.Event()
    result = library.retry_write_up(capture.folder, cancel_event=cancel)
    assert result.written_up
    source, shots, options = called[0]
    assert source.pdf.name == "walkthrough.pdf"
    assert [p.name for p in shots] == ["shot-1.png", "shot-2.png"]
    assert options == dict(agent="codex", instructions="Make steps", follow_notes=True,
                           notes=[], cancel_event=cancel)


def test_held_notes_are_saved_with_their_section_and_never_in_the_documents(tmp_path):
    folder = noted_capture(tmp_path)
    saved = library.load_document(folder)
    assert [s.notes for s in saved.sections] == [(), ("make it purple",), ("sign off warmly",)]
    assert saved.sections[2].text == ""  # kept for its note alone
    assert "purple" not in (folder / "walkthrough.md").read_text(encoding="utf-8")
    assert "purple" not in library.load_result(folder).history_text
    value = json.loads((folder / "walkthrough.json").read_text(encoding="utf-8"))
    value["sections"][1]["notes"] = [7]
    (folder / "walkthrough.json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        library.load_document(folder)


def test_retry_follows_the_notes_held_while_recording(tmp_path, monkeypatch):
    folder = noted_capture(tmp_path)
    called = []
    monkeypatch.setattr(write_up, "write_up",
                        lambda result, shots, **kwargs: called.append(kwargs) or result)
    library.retry_write_up(folder)
    assert [(n.text, n.section) for n in called[0]["notes"]] == [
        ("make it purple", 1), ("sign off warmly", 2)]


def test_notes_go_with_their_section_through_review(tmp_path):
    folder = noted_capture(tmp_path)
    library.save_review(folder, ["second", "first", ""], [True, False, True], [1, 0, 2])
    saved = library.load_document(folder)
    assert [s.notes for s in saved.sections] == [("make it purple",), (), ("sign off warmly",)]
    # Screenshot 2 is now the only one shown, so it is the first.
    notes = library.held_notes(saved)
    assert [(n.text, n.section) for n in notes] == [("make it purple", 0), ("sign off warmly", 1)]
    prompt = write_up._notes(notes, ["shot-2.png"])
    assert "About screenshot 1 (shot-2.png): make it purple" in prompt
    assert "After the last screenshot: sign off warmly" in prompt


def test_retry_render_failure_does_not_replace_existing_write_up(capture, monkeypatch):
    (capture.folder / "write-up.md").write_text("old Markdown")
    (capture.folder / "write-up.pdf").write_bytes(b"old PDF")
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(document, "write_markdown_pdf", fail)
    with pytest.raises(write_up.WriteUpFailed):
        write_up.write_up(capture.result, [], run=lambda *args, **kwargs: SimpleNamespace(
            canceled=False, ok=True, error="", text="# New write-up"))
    assert (capture.folder / "write-up.md").read_text() == "old Markdown"
    assert (capture.folder / "write-up.pdf").read_bytes() == b"old PDF"


def test_checkpoint_survives_session_recreation_and_keeps_audio(tmp_path):
    folder = tmp_path / "2026-10-21_14-32-05"
    folder.mkdir()
    session = WalkthroughSession(make_folder=lambda _: folder)
    session.checkpoint_recording("a" * 32)
    image = QImage(20, 20, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    session.add_shot(image, 1.25)
    session.saved_shots()
    source = tmp_path / "recording.wav"
    _audio(source)
    session.preserve_audio(str(source))
    session.end()
    recovered = library.recovery_session(folder)
    assert not recovered.open
    assert [(s.path.name, s.kept_at_s) for s in recovered.saved_shots()] == [("shot-1.png", 1.25)]
    assert (folder / library.AUDIO_NAME).read_bytes() == source.read_bytes()
    recovered.end()


def test_a_draft_keeps_held_notes_so_recovery_leaves_them_out(tmp_path):
    folder = tmp_path / "2026-10-21_14-32-05"
    folder.mkdir()
    session = WalkthroughSession(make_folder=lambda _: folder)
    session.checkpoint_recording("a" * 32)
    session.hold_note(0.5)
    session.release_note(1.0)
    session.hold_note(1.5)
    session._writer.submit(lambda: None).result()  # the checkpoints queued before it
    draft = json.loads((folder / library.DRAFT_NAME).read_text(encoding="utf-8"))
    assert draft["notes_s"] == [[0.5, 1.0], [1.5, None]]
    source = tmp_path / "recording.wav"
    _audio(source)
    session.preserve_audio(str(source))
    session.end()
    recovered = library.recovery_session(folder)
    assert recovered.storyboard().notes_s == ((0.5, 1.0), (1.5, math.inf))
    recovered.end()


def test_a_draft_with_notes_out_of_order_is_refused(tmp_path):
    folder = tmp_path / "2026-10-21_14-32-05"
    folder.mkdir()
    _audio(folder / library.AUDIO_NAME)
    (folder / library.DRAFT_NAME).write_text(json.dumps(dict(
        version=1, started_at="2026-10-21T14:32:05", journal_id="a" * 32, shots=[],
        notes_s=[[2.0, 3.0], [1.0, 1.5]])), encoding="utf-8")
    with pytest.raises(ValueError):
        library.recovery_session(folder)


def test_hard_process_exit_recovers_audio_and_screenshot_timing(tmp_path):
    from services.recording_journal import recover_recordings

    root = tmp_path / "captures"
    folder = root / "2026-10-21_14-32-05"
    folder.mkdir(parents=True)
    output = tmp_path / "recorded_audio.wav"
    code = '''
import os, sys
from pathlib import Path
from PyQt6.QtGui import QImage, QColor
from services.recording_journal import RecordingJournal
from services.walkthrough.session import WalkthroughSession
journal = RecordingJournal(sys.argv[1], 16000, 1, 2, print)
journal.append(b'\\x10\\x00' * 16000)
journal._queue.join()
session = WalkthroughSession(make_folder=lambda _: Path(sys.argv[2]))
session.checkpoint_recording(journal.directory.name)
image = QImage(20, 20, QImage.Format.Format_RGB32)
image.fill(QColor('red'))
session.add_shot(image, 0.5)
session.saved_shots()
os._exit(7)
'''
    child = subprocess.run([sys.executable, "-c", code, str(output), str(folder)],
                           cwd=Path(__file__).resolve().parents[1], capture_output=True,
                           timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert child.returncode == 7, child.stderr.decode(errors="replace")
    recordings = tmp_path / "recordings"
    recovered = []
    for _ in range(20):
        recovered = recover_recordings(str(output), str(recordings))
        if recovered:
            break
        time.sleep(0.05)  # Windows can release a dead process's locks asynchronously.
    assert len(recovered) == 1
    assert library.attach_recovered_audio(root, recordings) == 1
    session = library.recovery_session(folder)
    assert session.saved_shots()[0].kept_at_s == 0.5
    with wave.open(str(folder / library.AUDIO_NAME), "rb") as wav:
        assert wav.getnframes() == 16000
    session.end()


def test_recovery_joins_journal_identity_and_is_idempotent(tmp_path):
    root = tmp_path / "captures"
    folder = root / "2026-10-21_14-32-05"
    folder.mkdir(parents=True)
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    journal = "a" * 32
    library.atomic_json(folder / library.DRAFT_NAME,
                        dict(version=1, started_at=datetime.now().isoformat(), journal_id=journal, shots=[]))
    audio = recordings / f"recording_20261021_143205-recovered-{journal}.wav"
    _audio(audio)
    assert library.attach_recovered_audio(root, recordings) == 1
    assert library.attach_recovered_audio(root, recordings) == 0
    assert (folder / library.AUDIO_NAME).read_bytes() == audio.read_bytes()
    assert library.recovery_session(folder).saved_shots() == []


def test_draft_ignores_a_truncated_png(tmp_path):
    folder = tmp_path / "2026-10-21_14-32-05"
    folder.mkdir()
    _audio(folder / library.AUDIO_NAME)
    (folder / "shot-1.png").write_bytes(b"\\x89PNG truncated")
    library.atomic_json(folder / library.DRAFT_NAME,
                        dict(version=1, started_at=datetime.now().isoformat(),
                             shots=[dict(shot="shot-1.png", kept_at_s=1.0)]))
    session = library.recovery_session(folder)
    assert session.saved_shots() == []
    session.end()


def test_draft_skips_a_screenshot_whose_write_was_interrupted(tmp_path):
    folder = tmp_path / "2026-10-21_14-32-05"
    folder.mkdir()
    _audio(folder / library.AUDIO_NAME)
    library.atomic_json(folder / library.DRAFT_NAME,
                        dict(version=1, started_at=datetime.now().isoformat(),
                             shots=[dict(shot="shot-1.png", kept_at_s=1.0)]))
    session = library.recovery_session(folder)
    assert session.saved_shots() == []
    session.end()


def test_retention_defaults_to_forever_and_never_prunes_drafts(capture):
    path = capture.folder / document.MANIFEST_NAME
    old = time.time() - 100 * 86400
    os.utime(path, (old, old))
    assert library.prune(capture.root) == 0
    library.set_retention(capture.root, 30)
    (capture.folder / library.DRAFT_NAME).write_text("{}")
    assert library.prune(capture.root) == 0
    (capture.folder / library.DRAFT_NAME).unlink()
    assert library.prune(capture.root) == 1
    assert not capture.folder.exists()


def test_retention_refuses_extra_files(capture):
    (capture.folder / "my-notes.txt").write_text("keep")
    with pytest.raises(ValueError, match="extra files"):
        library.delete_entry(capture.folder)
    assert (capture.folder / "my-notes.txt").read_text() == "keep"


@pytest.mark.parametrize("days", [-1, 3651, True, "30"])
def test_retention_rejects_invalid_values(capture, days):
    with pytest.raises(ValueError):
        library.set_retention(capture.root, days)


def test_finished_document_removes_recovery_checkpoint(capture):
    assert not (capture.folder / library.DRAFT_NAME).exists()
    assert library.entries(capture.root) == [capture.folder]


def test_deletion_and_document_loading_refuse_symbolic_links(capture, tmp_path):
    image = capture.folder / "shot-1.png"
    target = tmp_path / "outside.png"
    image.rename(target)
    try:
        image.symlink_to(target)
    except OSError:
        pytest.skip("This Windows account cannot create symbolic links")
    with pytest.raises(ValueError):
        library.load_document(capture.folder)
    with pytest.raises(ValueError):
        library.delete_entry(capture.folder)
    assert target.exists()


def test_metadata_requires_an_object(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("[]")
    with pytest.raises(ValueError, match="object"):
        library.read_json(path)


def test_startup_recovery_does_not_load_the_qt_document_renderer():
    # Recovery and retention run on the startup worker; the PDF renderer
    # imports QtGui and loads only for review and write-ups.
    code = ("import sys\n"
            "from services.walkthrough import library\n"
            "assert 'services.walkthrough.document' not in sys.modules, 'document loaded'\n"
            "assert 'PyQt6.QtGui' not in sys.modules, 'QtGui loaded'\n")
    child = subprocess.run([sys.executable, "-c", code],
                           cwd=Path(__file__).resolve().parents[1], capture_output=True,
                           timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert child.returncode == 0, child.stderr.decode(errors="replace")
