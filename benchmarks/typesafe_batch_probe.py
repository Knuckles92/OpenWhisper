"""Measure repeated batched citation checks against the frozen holdout probes."""

from __future__ import annotations

import argparse
import copy
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import dotenv_values

from benchmarks.typesafe_cases import cases
from benchmarks.typesafe_experiments import (
    ENDPOINT,
    MODEL,
    QUESTIONS,
    ROOT,
    baseline,
    fingerprint,
    summarize,
    validate_response,
)


def build_batch(rows):
    questions = {}
    for index, row in enumerate(rows):
        for name, question in QUESTIONS["citation"].items():
            question = copy.deepcopy(question)
            instructions = question["instructions"]
            for field in ("segments", "claim"):
                instructions = instructions.replace(f"`{field}`", f"`cases[{index}].{field}`")
            question["instructions"] = (
                f"Evaluate only `cases[{index}]`. Other cases are unrelated and cannot "
                f"supply evidence for this case. {instructions}"
            )
            questions[f"{row['id']}_{name}"] = question
    return {"model": MODEL, "state": {"cases": [row["state"] for row in rows]}, "questions": questions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = [row for row in cases() if row["experiment"] == "citation" and row["split"] == "holdout"]
    request = build_batch(rows)
    if not args.live:
        print(json.dumps({"cases": len(rows), "questions": len(request["questions"]), "request_sha256": fingerprint(request)}))
        return
    if args.output is None or args.output.exists():
        parser.error("--live requires a new --output path")
    key = os.environ.get("TYPESAFE_API_KEY") or dotenv_values(ROOT / ".env").get("TYPESAFE_API_KEY")
    if not key or not key.strip():
        parser.error("TYPESAFE_API_KEY is unavailable")
    report = {"created_at": datetime.now(timezone.utc).isoformat(), "request": request,
              "request_sha256": fingerprint(request), "runs": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(headers={"Authorization": f"Bearer {key.strip()}"}, timeout=20, follow_redirects=False) as client:
        for repetition in range(3):
            started = time.perf_counter()
            try:
                response = client.post(ENDPOINT, json=request)
                response.raise_for_status()
                body = response.json()
                seconds = time.perf_counter() - started
                normalized = []
                for row in rows:
                    per_case = {"model": body["model"], "usage": {"input_tokens": 0, "output_tokens": 0},
                                "answers": {name: body["answers"][f"{row['id']}_{name}"] for name in QUESTIONS["citation"]}}
                    validate_response(per_case, QUESTIONS["citation"])
                    normalized.append({**row, "baseline": baseline(row), "seconds": seconds, "response": per_case})
                summary = summarize(normalized)["citation"]
                # Usage and elapsed time belong to the entire request, never each question.
                for field in ("input_tokens", "output_tokens", "estimated_usd", "median_seconds", "p95_seconds"):
                    summary.pop(field)
                report["runs"].append({"repetition": repetition, "seconds": round(seconds, 6),
                                       "response": body, "summary": summary})
            except (httpx.HTTPError, ValueError, KeyError, AssertionError, TypeError) as exc:
                report["runs"].append({"repetition": repetition, "error": type(exc).__name__})
                args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
                raise SystemExit("Batch probe failed; sanitized error saved") from None
            args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps([{key: value for key, value in run.items() if key != "response"} | {"usage": run["response"]["usage"]}
                      for run in report["runs"]], indent=2))


if __name__ == "__main__":
    main()
