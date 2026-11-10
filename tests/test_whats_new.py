"""The "New in OpenWhisper" note stays current and fits its tile."""
from datetime import date

from _version import __version__
from ui_qt.whats_new import MAX_TEXT_LENGTH, WHATS_NEW, is_seen


def test_note_describes_the_current_release_line():
    current = ".".join(__version__.split(".")[:2])
    assert WHATS_NEW.release_line == current, (
        f"OpenWhisper is {__version__} but the note in ui_qt/whats_new.py "
        f"describes {WHATS_NEW.release_line}: rewrite its text, then set "
        "release_line and updated."
    )


def test_note_fits_the_home_tile():
    assert WHATS_NEW.text.strip() == WHATS_NEW.text
    assert 0 < len(WHATS_NEW.text) <= MAX_TEXT_LENGTH


def test_updated_is_a_real_date_not_in_the_future():
    assert date.fromisoformat(WHATS_NEW.updated) <= date.today()


def test_only_the_current_note_counts_as_seen():
    assert is_seen(WHATS_NEW.updated)
    for stored in ("", "2000-01-01", True, None):
        assert not is_seen(stored)
