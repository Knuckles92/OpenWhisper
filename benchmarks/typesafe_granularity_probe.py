"""Compare independent claim requests with small shared-state verification batches."""

import argparse
import asyncio
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

from benchmarks.typesafe_experiments import ENDPOINT, MODEL, ROOT


def requests_for(original, batch_size, split_questions=False):
    checks = original["state"]["checks"]
    jobs = []
    for offset in range(0, len(checks), batch_size):
        questions = {}
        for index in range(offset, min(offset + batch_size, len(checks))):
            for key, value in original["questions"].items():
                if key.startswith(f"c{index}_"):
                    question = dict(value)
                    question["instructions"] = question["instructions"].replace(
                        f"checks[{index}]", f"checks[{index - offset}]"
                    )
                    questions[key] = question
        groups = [{key: value} for key, value in questions.items()] if split_questions else [questions]
        for group in groups:
            jobs.append({"model": MODEL, "state": {"checks": checks[offset:offset + batch_size]}, "questions": group})
    return jobs


def summarize(runs, expected, wall_seconds):
    answers = {key: value for run in runs for key, value in run.get("response", {}).get("answers", {}).items()}
    times = sorted(run["seconds"] for run in runs)
    tokens = sum(run.get("response", {}).get("usage", {}).get("input_tokens", 0) for run in runs)
    return {
        "text_correct": sum(answers.get(key, {}).get("choice") == value for key, value in expected.items()),
        "text_total": len(expected),
        "text_completed": sum(key in answers for key in expected),
        "text_confidence_ge_08": sum(answers.get(key, {}).get("confidence", 0) >= .8 for key in expected),
        "wall_seconds": wall_seconds,
        "median_request_seconds": statistics.median(times),
        "first_text_result_seconds": min((r["completed_at_seconds"] for r in runs if any(k.endswith("_text") for k in r.get("response", {}).get("answers", {}))), default=None),
        "requests": len(runs),
        "errors": sum("error" in r for r in runs),
        "input_tokens": tokens,
        "estimated_cost_usd": tokens * .042 / 1_000_000,
        "text_errors": [{"question": key, "expected": value, "answer": answers.get(key)} for key, value in expected.items() if answers.get(key, {}).get("choice") != value],
    }


async def run_arm(key, jobs, concurrency):
    semaphore = asyncio.Semaphore(concurrency)
    starts = asyncio.Lock()
    last_start = 0.0
    begin = time.perf_counter()
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + key}, timeout=30,
                                 limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
                                 follow_redirects=False) as client:
        async def call(payload):
            nonlocal last_start
            async with semaphore:
                # At most 20 request starts per second; queue time is included
                # in wall time, separately from the measured HTTP round trip.
                async with starts:
                    delay = .05 - (time.perf_counter() - last_start)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    last_start = time.perf_counter()
                started = time.perf_counter()
                result = {"request": payload, "started_at_seconds": started - begin}
                try:
                    response = await client.post(ENDPOINT, json=payload)
                    result["status_code"] = response.status_code
                    response.raise_for_status()
                    body = response.json()
                    if set(body["answers"]) != set(payload["questions"]):
                        raise ValueError("Response question IDs differ from the request")
                    for name, question in payload["questions"].items():
                        answer = body["answers"][name]
                        if answer["type"] != question["type"]:
                            raise ValueError("Unexpected answer type")
                        if question["type"] == "choice" and answer["choice"] not in question["criteria"]:
                            raise ValueError("Unexpected choice")
                        if question["type"] == "noul" and not 0 <= answer["noul"] <= 1:
                            raise ValueError("Invalid probability")
                    result["response"] = body
                except Exception as exc:
                    result["error"] = type(exc).__name__
                result["seconds"] = time.perf_counter() - started
                result["completed_at_seconds"] = time.perf_counter() - begin
                return result
        results = await asyncio.gather(*(call(payload) for payload in jobs))
    return results, time.perf_counter() - begin


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Choose a new output filename")
    key = os.environ.get("TYPESAFE_API_KEY") or dotenv_values(ROOT / ".env").get("TYPESAFE_API_KEY")
    if not key:
        parser.error("TypeSafe key unavailable")
    source = json.loads((ROOT / "benchmarks/typesafe_results/followup-verification.json").read_text())
    controls = json.loads((ROOT / "benchmarks/typesafe_results/hybrid-insights.json").read_text())["verification_controls"]["expected_supported"]
    original = source["decomposed"]["request"]
    flags = iter(controls)
    expected = {f"c{index}_text": "supported" if (next(flags) if row["origin"] == "control" else True) else "unsupported"
                for index, row in enumerate(source["checks"])}
    arms = [("one_claim_serial", 1, 1, False), ("one_claim_parallel4", 1, 4, False),
            ("eight_claims_serial", 8, 1, False), ("eight_claims_parallel4", 8, 4, False),
            ("one_question_parallel4", 1, 4, True)]
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "model": MODEL, "data": "saved synthetic claims",
              "expected_text_labels": expected, "max_request_starts_per_second": 20, "runs": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for repetition in range(2):
        for name, size, concurrency, split in (arms if repetition == 0 else list(reversed(arms))):
            jobs = requests_for(original, size, split)
            runs, wall = await run_arm(key, jobs, concurrency)
            summary = summarize(runs, expected, wall)
            report["runs"].append({"arm": name, "repetition": repetition, "batch_size": size,
                                   "concurrency": concurrency, "split_questions": split, "summary": summary, "requests": runs})
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"arm": name, "repetition": repetition, **summary}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
