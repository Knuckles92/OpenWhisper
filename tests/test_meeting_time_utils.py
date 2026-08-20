"""Tests for Meeting Mode UTC storage and local display compatibility."""
from datetime import datetime
from zoneinfo import ZoneInfo

from meeting.time_utils import (
    as_local_time,
    elapsed_seconds,
    parse_meeting_time,
    utc_now_iso,
)


def test_utc_timestamp_converts_to_requested_local_timezone():
    local = as_local_time(
        "2026-08-20T16:55:00Z",
        local_tz=ZoneInfo("America/Los_Angeles"),
    )

    assert local is not None
    assert local.strftime("%Y-%m-%d %I:%M %p") == "2026-08-20 09:55 AM"


def test_legacy_naive_timestamp_keeps_its_wall_time():
    legacy = as_local_time(
        "2026-08-20T09:55:00",
        local_tz=ZoneInfo("America/New_York"),
    )

    assert legacy == datetime(2026, 8, 20, 9, 55)
    assert legacy.tzinfo is None


def test_new_timestamps_are_aware_utc():
    value = utc_now_iso()
    parsed = parse_meeting_time(value)

    assert value.endswith("Z")
    assert parsed is not None
    assert parsed.utcoffset().total_seconds() == 0


def test_elapsed_seconds_handles_aware_and_legacy_pairs():
    assert elapsed_seconds(
        "2026-08-20T16:55:00Z",
        "2026-08-20T16:55:40Z",
    ) == 40
    assert elapsed_seconds(
        "2026-08-20T09:55:00",
        "2026-08-20T09:55:40",
    ) == 40
