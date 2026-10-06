"""A snippet's link survives a whole backup and restore."""

import json
from pathlib import Path

from services import backup
from services.settings import SettingsKey
from services.snippets import Snippet, load_snippets


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
