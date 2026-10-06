"""The cleanup-level golden set and its runner, without a network."""

import json

import pytest

from benchmarks.cleanup_levels import run as bench
from config import config
from services.cleanup_profiles import STARTER_PROFILES

PRESETS = config.TRANSCRIPT_CLEANUP_LEVEL_PROMPTS


class FakeCleaner:
    def __init__(self, outputs, error=None):
        self.outputs = outputs
        self.error = error
        self.calls = []
        self.last_error = "not run"

    def cleanup(self, text, system_prompt=None):
        self.calls.append((text, system_prompt))
        self.last_error = self.error
        return self.outputs.get(text, text)


@pytest.fixture(scope="module")
def cases():
    return bench.load_cases()


def test_the_golden_set_covers_every_level_and_behaviour(cases):
    labels = {bench.case_label(case) for case in cases}
    categories = {case["category"] for case in cases}

    assert len(cases) >= 30
    assert len({case["id"] for case in cases}) == len(cases)
    assert {"light", "medium", "high"} <= labels
    assert {f"profile:{profile.id}" for profile in STARTER_PROFILES} <= labels
    assert {
        "corrections", "corrections-negative", "lists", "lists-negative",
        "lists-inline", "light", "high", "language", "guard", "profile",
    } <= categories


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ({"id": "a", "input": "hi"}, "exactly one of level and profile"),
        ({"id": "a", "level": "extreme", "input": "hi", "must_contain": ["hi"]}, "unknown level"),
        ({"id": "a", "profile": "memo", "input": "hi", "must_contain": ["hi"]}, "unknown profile"),
        ({"id": "a", "level": "light", "input": "hi"}, "checks nothing"),
        ({"id": "a", "level": "light", "input": " ", "must_contain": ["hi"]}, "no input"),
        ({"id": "a", "level": "light", "input": "hi", "must_contain": ["hi"],
          "destination": "fax"}, "unknown destination"),
        ({"id": "a", "level": "light", "input": "hi", "must_contain": ["hi"],
          "expected": "Hi."}, "unknown keys"),
    ],
)
def test_bad_cases_are_named(tmp_path, line, message):
    path = tmp_path / "golden.jsonl"
    path.write_text(json.dumps(line) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        bench.load_cases(path)


def test_duplicate_ids_are_refused(tmp_path):
    line = json.dumps({"id": "a", "level": "light", "input": "hi", "must_contain": ["hi"]})
    path = tmp_path / "golden.jsonl"
    path.write_text(f"{line}\n\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="a: duplicate id"):
        bench.load_cases(path)


def test_prompts_are_the_shipped_ones(cases):
    by_id = {case["id"]: case for case in cases}

    assert bench.compose_prompt(by_id["light-keeps-retraction"]) == PRESETS["light"]
    assert bench.compose_prompt(by_id["list-numbers"]) == PRESETS["medium"]
    assert bench.compose_prompt(by_id["list-unknown-app-inline"]) == (
        f"{PRESETS['medium']}\n\n{config.TRANSCRIPT_CLEANUP_UNKNOWN_APP_LINES}"
    )
    profile = bench.compose_prompt(by_id["profile-email-correction"])
    assert config.TRANSCRIPT_CLEANUP_SPOKEN_CORRECTIONS in profile
    assert "Subject line" in profile


@pytest.mark.parametrize(
    ("case", "output", "failures"),
    [
        ({"must_contain": [["3", "three"]], "must_not_contain": ["2"]}, "Meet at 3.", []),
        ({"must_contain": [["3", "three"]]}, "Meet at Three.", []),
        ({"must_contain": ["eggs"]}, "Buy eggshells.", ["missing 'eggs'"]),
        ({"must_not_contain": ["um"]}, "Umbrella, please.", []),
        ({"must_not_contain": ["Mark"]}, "Send it to mark.", ["contains 'Mark'"]),
        ({"must_contain": ["milk"], "min_lines": 3}, "1. Milk\n\n2. Eggs", ["2 lines, wanted at least 3"]),
        ({"must_contain": ["milk"], "max_lines": 1}, "Milk\nEggs", ["2 lines, wanted at most 1"]),
    ],
)
def test_checks(case, output, failures):
    assert bench.check(case, output) == failures


def test_run_checks_each_answer_and_counts_rates(cases):
    chosen = [case for case in cases if case["id"] in ("corrections-time", "light-keeps-retraction")]
    cleaner = FakeCleaner({
        "let's meet at 2 actually 3": "Let's meet at 3.",
    })

    results = bench.run(chosen, cleaner)
    summary = bench.summarize(results)

    assert [result["passed"] for result in results] == [True, False]
    assert results[1]["failures"] == ["missing '2 / two'"]
    assert [prompt for _text, prompt in cleaner.calls] == [PRESETS["medium"], PRESETS["light"]]
    assert summary["passed"] == 1 and summary["total"] == 2
    assert summary["by_label"] == {"light": (0, 1), "medium": (1, 1)}
    assert summary["by_category"] == {"corrections": (1, 1), "light": (0, 1)}


def test_a_failed_cleanup_fails_the_case(cases):
    cleaner = FakeCleaner({}, error="empty response")

    results = bench.run(cases[:1], cleaner)

    assert results[0]["failures"] == ["cleanup failed: empty response"]


def test_main_stops_without_a_provider(monkeypatch, capsys):
    class Unavailable(FakeCleaner):
        def __init__(self, **kwargs):
            super().__init__({})
            self.provider = kwargs["provider"]

        def is_available(self):
            return False

    monkeypatch.setattr("services.transcript_cleanup.TranscriptCleanup", Unavailable)

    assert bench.main(["--case", "corrections-time"]) == 2
    assert "not set up" in capsys.readouterr().out
