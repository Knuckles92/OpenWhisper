"""Tests for the Whisper hallucination filter applied before segments are stored."""
from types import SimpleNamespace

import pytest

from meeting.asr.hallucination import is_hallucination


def seg(text, no_speech=0.0, logprob=-0.2, compression=1.3):
    return SimpleNamespace(
        text=text, no_speech_prob=no_speech, avg_logprob=logprob,
        compression_ratio=compression,
    )


@pytest.mark.parametrize("segment", [
    seg("We should ship on Friday.", no_speech=0.7, logprob=-1.3),
    seg("the plan the plan the plan the plan the plan", compression=3.1),
    seg(" Thanks for watching!", no_speech=0.4),
    seg("You", logprob=-0.9),
])
def test_non_speech_is_dropped(segment):
    assert is_hallucination(segment)


@pytest.mark.parametrize("segment", [
    seg("We should ship on Friday."),
    seg("Hmm, not sure.", no_speech=0.7, logprob=-0.4),
    seg("Thank you for watching the demo, everyone."),
    seg("Thanks for watching.", no_speech=0.1, logprob=-0.3),
    seg("you", no_speech=0.05, logprob=-0.2),
])
def test_real_speech_is_kept(segment):
    assert not is_hallucination(segment)


def _row(seg_id, channel, start, end, text):
    from meeting.interfaces import TranscriptSegment

    return TranscriptSegment(
        segment_id=seg_id, meeting_id="m", chunk_id=None, channel=channel,
        start_s=start, end_s=end, text=text,
    )


def test_mic_copy_of_system_audio_is_dropped():
    from meeting.asr.offline import drop_mic_echo

    rows = [
        _row("lb1", "loopback", 10.0, 14.0, "The vendor ships the parts on Monday."),
        _row("mic_echo", "mic", 10.4, 14.3, "vendor ships the parts on Monday"),
        _row("mic_real", "mic", 15.0, 17.0, "Great, I will tell the warehouse."),
        _row("mic_short", "mic", 10.5, 11.0, "Monday."),
    ]
    kept = [seg.segment_id for seg in drop_mic_echo(rows)]
    assert kept == ["lb1", "mic_real", "mic_short"]


def test_same_words_at_a_different_time_are_not_echo():
    from meeting.asr.offline import drop_mic_echo

    rows = [
        _row("lb1", "loopback", 10.0, 12.0, "ship the parts on Monday"),
        _row("mic1", "mic", 40.0, 42.0, "ship the parts on Monday"),
    ]
    assert [seg.segment_id for seg in drop_mic_echo(rows)] == ["lb1", "mic1"]


def test_backends_without_scores_are_never_filtered():
    assert not is_hallucination(SimpleNamespace(text="Thanks for watching"))
    assert not is_hallucination(SimpleNamespace(
        text="you", avg_logprob=0.0, no_speech_prob=0.0,
    ))
