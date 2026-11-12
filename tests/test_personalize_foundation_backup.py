"""Backups carry the Scratchpad and keep URLs in snippet text, through a whole restore too."""

import json
import zipfile
from pathlib import Path

from services import backup
from services.settings import SettingsKey
from services.snippets import Snippet, load_snippets


def test_scratchpad_is_backed_up_and_restored(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "scratchpad.txt").write_text("Groceries:\n- oat milk\n", encoding="utf-8")
    (source / "openwhisper_settings.json").write_text("{}", encoding="utf-8")
    archive = tmp_path / "notes.owbackup"

    assert "scratchpad.txt" in backup.OWNED_NAMES
    backup.create_backup(archive, data_dir=source)
    with zipfile.ZipFile(archive) as zf:
        assert "data/scratchpad.txt" in zf.namelist()

    backup.prepare_restore(archive, data_dir=target)
    assert backup.apply_pending_restore(data_dir=target)
    assert (target / "scratchpad.txt").read_text(encoding="utf-8") == "Groceries:\n- oat milk\n"


def test_snippet_text_keeps_its_urls_while_endpoints_are_scrubbed() -> None:
    snippets = [{
        "id": "s1",
        "trigger": "my calendar",
        "text": "Book a time: https://cal.example.com/dana?ref=email#slots",
        "formatted": False,
    }]
    settings = {
        "dictation_snippets": snippets,
        "transcript_cleanup_base_url": "https://user:pw@api.example.com/v1?key=abc",
    }

    cleaned = backup._sanitize_settings(settings)

    assert cleaned["dictation_snippets"] == snippets
    assert cleaned["transcript_cleanup_base_url"] == "https://api.example.com/v1"
    assert json.loads(json.dumps(cleaned))["dictation_snippets"][0]["text"].endswith("#slots")


def test_snippet_links_with_queries_survive_a_backup_and_restore(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    snippet = Snippet(
        "cal", "my calendar link",
        "Book here: [a time](https://cal.example.com/dana?ref=email&src=ow#slots)",
        formatted=True,
    )
    (source / "openwhisper_settings.json").write_text(json.dumps({
        SettingsKey.DICTATION_SNIPPETS: [snippet.to_dict()],
        "transcript_cleanup_base_url": "https://user:pw@api.example.com/v1?key=abc",
    }), encoding="utf-8")
    archive = tmp_path / "snippets.owbackup"

    backup.create_backup(archive, data_dir=source)
    backup.prepare_restore(archive, data_dir=target)
    assert backup.apply_pending_restore(data_dir=target)

    restored = json.loads((target / "openwhisper_settings.json").read_text(encoding="utf-8"))
    assert load_snippets(restored) == [snippet]
    assert restored["transcript_cleanup_base_url"] == "https://api.example.com/v1"
