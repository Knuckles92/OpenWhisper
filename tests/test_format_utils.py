"""Unit tests for shared display-formatting helpers."""

from datetime import datetime, timezone, timedelta

from services.format_utils import format_audio_duration, format_timestamp
from services.models import TranscriptionHistory


def test_format_audio_duration_keeps_subsecond_precision():
    assert format_audio_duration(3.72) == "3.7s"
    assert format_audio_duration(0) == "0.0s"
    assert format_audio_duration(59.9) == "59.9s"
    assert format_audio_duration(125) == "2m 5s"
    assert format_audio_duration(3600) == "1h"


def test_format_timestamp_converts_aware_utc_to_local():
    utc = datetime(2026, 8, 20, 16, 46, tzinfo=timezone.utc)
    local = utc.astimezone()
    expected = local.strftime("%b %d, %Y %I:%M %p")
    assert format_timestamp(utc.isoformat()) == expected


def test_format_timestamp_keeps_naive_legacy_wall_time():
    naive = datetime(2026, 8, 20, 16, 46)
    assert format_timestamp(naive.isoformat()) == "Aug 20, 2026 04:46 PM"


def test_history_create_stores_aware_utc_and_empty_preview():
    entry = TranscriptionHistory.create(
        text="   ",
        model="local_whisper",
        source_name="sample.wav",
    )
    parsed = datetime.fromisoformat(entry.timestamp)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)
    assert entry.preview_text == "Empty transcript"
    assert entry.source_name == "sample.wav"
