"""Tests for the real-meeting benchmark's parser and trustworthy metrics."""
from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.meeting_mode.ami import ReferenceWord, parse_reference_words
from benchmarks.meeting_mode.metrics import (
    edit_counts,
    normalize_tokens,
    reference_overlap_stats,
    score_timed_transcript,
)
from benchmarks.meeting_mode.run import DRAFT_PROMPT_WORDS, _parse_args


def test_ami_reference_parser_merges_speakers_and_skips_events(tmp_path: Path):
    words = tmp_path / "words"
    words.mkdir()
    (words / "TEST.A.words.xml").write_text(
        """<?xml version="1.0"?>
        <root xmlns:nite="http://nite.sourceforge.net/">
          <w starttime="2.0" endtime="2.2">Later</w>
          <vocalsound starttime="2.2" endtime="2.5" type="laugh" />
          <w starttime="2.5" endtime="2.5" punc="true">.</w>
        </root>""",
        encoding="utf-8",
    )
    (words / "TEST.B.words.xml").write_text(
        """<?xml version="1.0"?>
        <root xmlns:nite="http://nite.sourceforge.net/">
          <w starttime="1.0" endtime="1.2">First</w>
          <gap starttime="1.3" endtime="1.4" />
        </root>""",
        encoding="utf-8",
    )

    parsed = parse_reference_words(tmp_path, "TEST")

    assert [(word.text, word.speaker) for word in parsed] == [
        ("First", "B"),
        ("Later", "A"),
    ]


def test_edit_counts_are_exact_and_directional():
    counts = edit_counts(
        ["the", "quick", "brown", "fox"],
        ["the", "slow", "fox", "today"],
    )

    assert counts.substitutions == 1
    assert counts.deletions == 1
    assert counts.insertions == 1
    assert counts.errors == 3


def test_timed_score_contains_alignment_drift_within_windows():
    reference = [
        ReferenceWord("alpha", 1.0, 1.2, "A"),
        ReferenceWord("beta", 2.0, 2.2, "A"),
        ReferenceWord("gamma", 301.0, 301.2, "A"),
        ReferenceWord("delta", 302.0, 302.2, "A"),
    ]
    hypothesis = [
        {"start_s": -5.0, "end_s": -1.0, "text": "outside before"},
        {"start_s": 1.0, "end_s": 1.2, "text": "alpha beta extra"},
        {"start_s": 301.0, "end_s": 301.2, "text": "gamma"},
        {"start_s": 400.0, "end_s": 401.0, "text": "outside after"},
    ]

    score = score_timed_transcript(reference, hypothesis, window_s=300.0)

    assert score["reference_words"] == 4
    assert score["hypothesis_words"] == 4
    assert score["deletions"] == 1
    assert score["insertions"] == 1
    assert score["substitutions"] == 0
    assert score["wer"] == pytest.approx(1 / 2)
    assert len(score["windows"]) == 2
    assert score["out_of_reference_hypothesis_words"] == 4


def test_normalization_is_conservative():
    assert normalize_tokens("It’s GPU-based, A_S_R_ version 2.0!") == [
        "it's",
        "gpu",
        "based",
        "asr",
        "version",
        "2",
        "0",
    ]


def test_reference_overlap_stats_counts_both_speakers():
    reference = [
        ReferenceWord("one", 0.0, 1.0, "A"),
        ReferenceWord("two", 0.5, 1.5, "B"),
        ReferenceWord("three", 1.5, 2.0, "A"),
        ReferenceWord("four", 1.6, 1.8, "A"),
    ]

    stats = reference_overlap_stats(reference)

    assert stats["reference_words"] == 4
    assert stats["overlapping_reference_words"] == 2
    assert stats["overlap_rate"] == pytest.approx(0.5)


def test_benchmark_defaults_to_stable_production_profile():
    args = _parse_args([])

    assert args.enable_revisions is False
    assert args.draft_prompt_words == DRAFT_PROMPT_WORDS
