"""Rebuild the offline summary of the saved TypeSafe API experiments."""

import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "benchmarks/typesafe_results"
AMBIGUOUS = {"dedup_4", "retrieval_6"}


def read(name):
    return json.loads((RESULTS / name).read_text(encoding="utf-8"))


def aggregate_comparison(filename):
    data = read(filename)
    requests = data["requests"]
    usage = [r.get("usage", r.get("response", {}).get("usage", {})) for r in requests]
    rows = data["results"]
    sensitive = [r for r in rows if r["id"] not in AMBIGUOUS]
    summary = data["summary"]
    return {
        "artifact": filename,
        "model": data["model"],
        "suite_sha256": data["suite_sha256"],
        "cases": sum(s["cases"] for s in summary.values()),
        "completed": sum(s["completed"] for s in summary.values()),
        "correct": sum(s["primary_correct"] for s in summary.values()),
        "fields_correct": sum(s["fields_correct"] for s in summary.values()),
        "fields": sum(s["fields"] for s in summary.values()),
        "median_seconds": statistics.median(r["seconds"] for r in requests),
        "min_seconds": min(r["seconds"] for r in requests),
        "max_seconds": max(r["seconds"] for r in requests),
        "requests": len(requests),
        "wall_seconds": data["wall_seconds"],
        "reported_cost_usd": sum(u["cost"] for u in usage) if all("cost" in u for u in usage) else None,
        "typesafe_estimate_usd": sum(u.get("input_tokens", 0) for u in usage) * .042 / 1_000_000 if data["arm"] == "typesafe" else None,
        "sensitivity_excluding_two_ambiguous": {
            "correct": sum(r.get("answers", {}).get("answer") == r["expected"]["answer"] for r in sensitive),
            "cases": len(sensitive),
        },
        "families": {name: f"{s['primary_correct']}/{s['cases']}" for name, s in summary.items()},
    }


def main():
    arms = [aggregate_comparison(name) for name in (
        "comparison-typesafe.json", "comparison-meeting-structured.json",
        "comparison-cleanup-structured.json", "comparison-openai-structured.json",
    )]
    assert len({arm["suite_sha256"] for arm in arms}) == 1
    agent_results = []
    for harness in ("direct", "direct-json", "pi", "opencode"):
        live = read(f"agent-{harness}.json")
        final = read(f"consolidation-{harness}.json")
        agent_results.append({
            "harness": harness,
            "live_checks_passed": sum(r["passed"] for r in live["results"]),
            "live_checks": len(live["results"]),
            "initial_notes_scenario_seconds": live["results"][0]["elapsed_s"],
            "live_suite_wall_seconds": live["instrumentation"]["wall_seconds"],
            "consolidation_seconds": final["seconds"],
            "consolidation_ok": final["ok"],
            "consolidation_items": sum(len(items) for items in final["state"]["cards"].values()),
            "consolidation_rejected_ops": sum(not r["ok"] for r in final["ops"]),
        })
    scaling = read("followup-scaling.json")["results"]
    grouped = {}
    for r in scaling:
        # Derive actual size from the retained payload; original metadata said
        # 4,000 but the generated nine-word phrase repeated 500 times = 4,500.
        words = len(r["request"]["state"].get("unrelated_history", "").split())
        grouped.setdefault((r["cases"], words), []).append(r)
    scale_summary = [{
        "cases": cases, "distractor_words_actual": words,
        "questions": rows[0]["questions"],
        "median_seconds": statistics.median(r["seconds"] for r in rows),
        "fields_correct": sum(r["fields_correct"] for r in rows),
        "fields": sum(r["fields"] for r in rows),
        "repetitions": len(rows),
    } for (cases, words), rows in grouped.items()]
    cascade = read("hybrid-cascade.json")
    ts_tokens = sum(r["response"]["usage"]["input_tokens"] for r in cascade["requests"] if r["stage"] == "typesafe")
    llm_cost = sum(r["usage"]["cost"] for r in cascade["requests"] if r["stage"] == "llm")
    verification = read("followup-verification.json")
    sliced = read("followup-verification-sliced.json")
    bad = {28, 29, 30, 31}
    def score_verification(answers):
        text = {k: v for k, v in answers.items() if k.endswith("_text")}
        return {
            "correct": sum(v["choice"] == ("unsupported" if int(k.split("_")[0][1:]) in bad else "supported") for k, v in text.items()),
            "total": len(text),
            "confidence_at_least_point8": sum(v["confidence"] >= .8 for v in text.values()),
        }
    retrieval = read("hybrid-retrieval.json")["results"]
    output = {
        "scope": "Synthetic text/API experiments; timings exclude ASR; no local model calls.",
        "comparison": arms,
        "agents": agent_results,
        "scaling": scale_summary,
        "cascade": {
            "cases": len(cascade["results"]), "routed_cases": cascade["routed_cases"],
            "correct": sum(s["primary_correct"] for s in cascade["summary"].values()),
            "wall_seconds": cascade["wall_seconds"],
            "llm_requests": sum(r["stage"] == "llm" for r in cascade["requests"]),
            "llm_reported_cost_usd": llm_cost,
            "typesafe_estimate_usd": ts_tokens * .042 / 1_000_000,
        },
        "verification": {
            "one_batch": score_verification(verification["decomposed"]["response"]["answers"]),
            "batches_of_eight": score_verification({k: v for r in sliced["runs"] for k, v in r["response"]["answers"].items()}),
        },
        "retrieval": {
            "positive_queries": sum(r["expected"] != "none" for r in retrieval),
            "literal_recalled": sum(r["expected"] in r["original_hits"] for r in retrieval),
            "expanded_recalled": sum(r["expected"] in r["expanded_hits"] for r in retrieval),
            "correct": sum(r["expected"] == r["actual"] for r in retrieval),
            "queries": len(retrieval),
        },
    }
    (RESULTS / "comparison-summary.json").write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
