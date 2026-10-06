"""Run the cleanup-level golden set against a real cleanup model.

Each case in golden.jsonl is a dictation at a level (or through a starter
cleanup profile) with checks a script can make: phrases the result must or
must not contain (whole words, any case; a nested list means any one of
them) and a range of non-empty lines. The prompt is composed the way a live
dictation's is, through services.dictation_pipeline, and sent through
TranscriptCleanup, so a run measures the shipped presets.

A run sends the synthetic inputs to the cleanup provider; the test suite
never does. With no options it uses the provider, model and thinking level
the app would, which on a fresh install is OpenRouter's free router. Free
models allow 20 requests a minute and a failed request is retried once, so
give them a few seconds between cases:

    python -m benchmarks.cleanup_levels.run --delay 4
    python -m benchmarks.cleanup_levels.run --level medium --case list-numbers
    python -m benchmarks.cleanup_levels.run --provider openai --model gpt-4o-mini --json out.json

The free router picks a different model per request, some of them not
chat models at all, so compare runs of one fixed model.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import Future
from pathlib import Path
from typing import Iterable, Optional

GOLDEN = Path(__file__).with_name("golden.jsonl")
#: Where a case's text is pasted: a known plain-text app, or one the focus
#: capture could not identify.
DESTINATIONS = ("app", "unknown")
_KNOWN_KEYS = {
    "id", "category", "level", "profile", "destination", "input",
    "must_contain", "must_not_contain", "min_lines", "max_lines", "note",
}


def load_cases(path: Path = GOLDEN) -> list[dict]:
    """The golden cases, validated; raises ValueError naming a bad case."""
    from services.cleanup_profiles import STARTER_PROFILES
    from services.cleanup_prompts import CleanupLevel

    profiles = {profile.id for profile in STARTER_PROFILES}
    cases, seen = [], set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = json.loads(line)
        name = case.get("id") or f"line {number}"
        problems = []
        if set(case) - _KNOWN_KEYS:
            problems.append(f"unknown keys {sorted(set(case) - _KNOWN_KEYS)}")
        if name in seen:
            problems.append("duplicate id")
        if not isinstance(case.get("input"), str) or not case["input"].strip():
            problems.append("no input")
        if ("level" in case) == ("profile" in case):
            problems.append("needs exactly one of level and profile")
        elif "level" in case and case["level"] not in CleanupLevel.STORED:
            problems.append(f"unknown level {case['level']!r}")
        elif "profile" in case and case["profile"] not in profiles:
            problems.append(f"unknown profile {case['profile']!r}")
        if case.get("destination", "app") not in DESTINATIONS:
            problems.append(f"unknown destination {case['destination']!r}")
        if not case.get("must_contain") and not case.get("must_not_contain"):
            problems.append("checks nothing")
        if problems:
            raise ValueError(f"{name}: {'; '.join(problems)}")
        seen.add(name)
        cases.append(case)
    return cases


def case_label(case: dict) -> str:
    return case["level"] if "level" in case else f"profile:{case['profile']}"


def compose_prompt(case: dict) -> str:
    """The system prompt a live dictation of ``case`` would get."""
    from services.cleanup_profiles import STARTER_PROFILES
    from services.dictation_pipeline import (
        DictationJob,
        compose_cleanup_prompt,
        prepare_text,
    )
    from services.focus_context import AppIdentity, FocusSnapshot
    from services.settings import SettingsKey

    identity = None
    if case.get("destination", "app") == "app":
        identity = AppIdentity("notepad.exe", "Notepad", platform="windows")
    focus: Future = Future()
    focus.set_result(FocusSnapshot(identity))
    job = DictationJob(focus=focus)
    settings = {
        SettingsKey.TRANSCRIPT_CLEANUP_ENABLED: True,
        SettingsKey.TRANSCRIPT_CLEANUP_LEVEL: case.get("level", "medium"),
    }
    profile = next(
        (item for item in STARTER_PROFILES if item.id == case.get("profile")), None
    )
    prepared = prepare_text(case["input"], job, settings)
    return compose_cleanup_prompt(
        job=job, settings=settings, profile=profile, rules=[], prepared=prepared
    )


def _has(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.IGNORECASE) is not None


def check(case: dict, output: str) -> list[str]:
    """What ``output`` gets wrong for ``case``; empty when it passes."""
    failures = []
    for wanted in case.get("must_contain", ()):
        options = wanted if isinstance(wanted, list) else [wanted]
        if not any(_has(output, option) for option in options):
            failures.append(f"missing {' / '.join(options)!r}")
    for unwanted in case.get("must_not_contain", ()):
        if _has(output, unwanted):
            failures.append(f"contains {unwanted!r}")
    lines = len([line for line in output.splitlines() if line.strip()])
    if "min_lines" in case and lines < case["min_lines"]:
        failures.append(f"{lines} lines, wanted at least {case['min_lines']}")
    if "max_lines" in case and lines > case["max_lines"]:
        failures.append(f"{lines} lines, wanted at most {case['max_lines']}")
    return failures


def run(cases: Iterable[dict], cleaner, *, delay_s: float = 0.0) -> list[dict]:
    """Clean each case's input with ``cleaner`` and check the result.

    ``cleaner`` is a TranscriptCleanup, or anything with its ``cleanup`` and
    ``last_error``.
    """
    results = []
    for index, case in enumerate(cases):
        if index and delay_s:
            time.sleep(delay_s)
        started = time.monotonic()
        output = cleaner.cleanup(case["input"], system_prompt=compose_prompt(case))
        error = cleaner.last_error
        # Provider error bodies can end in account details; the start says
        # what went wrong.
        failures = [f"cleanup failed: {error[:120]}"] if error else check(case, output)
        results.append({
            "id": case["id"],
            "label": case_label(case),
            "category": case.get("category", ""),
            "passed": not failures,
            "failures": failures,
            "output": output,
            "elapsed_s": round(time.monotonic() - started, 2),
        })
    return results


def summarize(results: list[dict]) -> dict:
    """Pass counts by level (or profile) and by category."""
    summary = {"by_label": defaultdict(lambda: [0, 0]), "by_category": defaultdict(lambda: [0, 0])}
    for result in results:
        for group, key in (("by_label", result["label"]), ("by_category", result["category"])):
            summary[group][key][0] += result["passed"]
            summary[group][key][1] += 1
    passed = sum(result["passed"] for result in results)
    return {
        "passed": passed,
        "total": len(results),
        "by_label": {key: tuple(value) for key, value in sorted(summary["by_label"].items())},
        "by_category": {key: tuple(value) for key, value in sorted(summary["by_category"].items())},
    }


def _rate(passed: int, total: int) -> str:
    return f"{passed}/{total} ({100 * passed / total:.0f}%)" if total else "0/0"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--provider", help="Text model endpoint id; default: the app's")
    parser.add_argument("--model", help="Model id; default: the app's")
    parser.add_argument("--reasoning", help="off, low, medium or high; default: the app's")
    parser.add_argument("--level", action="append", help="Only cases at this level (repeatable)")
    parser.add_argument("--case", action="append", help="Only this case id (repeatable)")
    parser.add_argument("--delay", type=float, default=0.0, help="Seconds between requests")
    parser.add_argument("--json", type=Path, help="Also write the results here")
    args = parser.parse_args(argv)
    # Model output may hold characters a Windows console code page lacks.
    sys.stdout.reconfigure(errors="replace")

    from services.settings import (
        resolve_transcript_cleanup_model,
        resolve_transcript_cleanup_provider,
        resolve_transcript_cleanup_reasoning,
        settings_manager,
    )
    from services.transcript_cleanup import TranscriptCleanup

    cases = load_cases()
    if args.level:
        cases = [case for case in cases if case_label(case) in args.level]
    if args.case:
        cases = [case for case in cases if case["id"] in args.case]
    if not cases:
        print("No cases match.")
        return 2

    settings = settings_manager.load_all_settings()
    provider = args.provider or resolve_transcript_cleanup_provider(settings)
    model = args.model or resolve_transcript_cleanup_model(settings)
    reasoning = args.reasoning or resolve_transcript_cleanup_reasoning(settings)
    cleaner = TranscriptCleanup(provider=provider, model=model, reasoning=reasoning)
    if not cleaner.is_available():
        print(f"AI cleanup is not set up for {provider}: no API key or model.")
        return 2
    print(f"{len(cases)} cases · {cleaner.provider} · {cleaner.model} · thinking {cleaner.reasoning}")

    results = run(cases, cleaner, delay_s=args.delay)
    for result in results:
        mark = "pass" if result["passed"] else "FAIL"
        print(f"{mark}  {result['label']:<22} {result['id']:<28} {result['elapsed_s']:>5.1f}s")
        if not result["passed"]:
            print(f"      {'; '.join(result['failures'])}")
            print("      " + result["output"].replace("\n", "\n      "))
    summary = summarize(results)
    print()
    print(f"All: {_rate(summary['passed'], summary['total'])}")
    for group in ("by_label", "by_category"):
        for key, (passed, total) in summary[group].items():
            print(f"  {key:<24} {_rate(passed, total)}")
    if args.json:
        args.json.write_text(
            json.dumps({"provider": cleaner.provider, "model": cleaner.model,
                        "summary": summary, "results": results}, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
