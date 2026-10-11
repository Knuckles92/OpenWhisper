"""Measure Route by voice (services.actions.routing) against labelled utterances.

    python -m benchmarks.voice_routing.run                 # offline: the gate only
    python -m benchmarks.voice_routing.run --live          # ask Jev
        [--provider typesafe|openrouter] [--variant default --variant none_first]

A live run sends only the synthetic utterances in ``cases.py``, through the
judge ``judge_from_settings()`` builds for the chosen provider, so it uses the
key saved in Settings (never read here, never written). Each request is the
exact request the app sends; a variant changes one thing about it. Results go
to ``results/<stamp>-<provider>.json`` (kept local) and the metrics to
``results/summary.json`` (committed).

Metrics, at each threshold on the judge's confidence (what the app gates on)
and on the chosen option's probability:

- accuracy: the outcome is one of the case's acceptable outcomes;
- false routes: words sent to an action they weren't meant for, as a share
  of all cases and of the cases that should be pasted (false routes are worse
  than misses: a miss only pastes the words);
- recall: cases meant for an action that reached it;
- payload: correctly routed cases whose words are an acceptable payload.

The gate (offline and live): of the plain dictations, how many meant for an
action reach the judge, and how many that must be pasted are stopped before
anything is sent.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from benchmarks.voice_routing.cases import ACTION_SETS, CASES, Case
from services.actions import routing

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results"
THRESHOLDS = (0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)
#: Long enough to see the real latency distribution; the app gives up at
#: routing.ROUTE_TIMEOUT_S, and the report counts answers slower than that.
BENCH_TIMEOUT_S = 10.0
#: TypeSafe's list price for input tokens; output tokens are free.
INPUT_USD_PER_MILLION = 0.042


def _none_first(questions: dict) -> dict:
    changed = copy.deepcopy(questions)
    criteria = changed["route"]["criteria"]
    changed["route"]["criteria"] = {routing.NONE: criteria[routing.NONE],
                                    **{k: v for k, v in criteria.items() if k != routing.NONE}}
    return changed


def _no_payload(questions: dict) -> dict:
    return {"route": copy.deepcopy(questions["route"])}


VARIANTS: dict[str, Callable[[dict], dict]] = {
    # Exactly what the app sends.
    "default": lambda questions: questions,
    # The "none" option listed before the actions (Jev leans to the first option).
    "none_first": _none_first,
    # The route question alone, to see what asking both together costs.
    "route_only": _no_payload,
}


class _UsageTap:
    """Records the usage block of each response (OpenRouter adds ``cost``)."""

    def __init__(self) -> None:
        from services import typesafe

        self._module = typesafe
        self._original = typesafe._http_post
        self.last: dict = {}

    def __enter__(self) -> "_UsageTap":
        def tapped(payload, timeout_s, **kwargs):
            status, text = self._original(payload, timeout_s, **kwargs)
            try:
                usage = json.loads(text).get("usage") if status == 200 else None
            except ValueError:
                usage = None
            self.last = usage if isinstance(usage, dict) else {}
            return status, text

        self._module._http_post = tapped
        return self

    def __exit__(self, *exc) -> None:
        self._module._http_post = self._original


def _norm(text: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", (text or "").casefold()))


def _enabled(case: Case):
    return [a for a in ACTION_SETS[case.action_set].values() if a.enabled]


def run_case(case: Case, judge: Any, variant: Callable[[dict], dict], tap: Optional[_UsageTap]
             ) -> dict:
    plan = routing.plan(case.text, case.route, _enabled(case))
    record: dict[str, Any] = {
        "id": case.id, "set": case.action_set, "route": case.route, "tags": list(case.tags),
        "text": case.text, "expect": list(case.expected_ids()), "payloads": list(case.payloads),
        "gate": plan is not None,
    }
    if plan is None:
        return record
    questions = variant(plan.questions)
    record.update(
        candidates=len(plan.candidates), matched=list(plan.matched),
        payload_options=len(plan.spans),
        request_chars=len(json.dumps({"state": plan.state, "questions": questions})),
    )
    if judge is None:
        return record
    if tap is not None:
        tap.last = {}
    started = time.perf_counter()
    answers = judge.ask(plan.state, questions, timeout_s=BENCH_TIMEOUT_S)
    record["latency_s"] = round(time.perf_counter() - started, 4)
    record["usage"] = dict(tap.last) if tap is not None else {}
    judgment = plan.read(answers)
    if judgment is None:
        record["failed"] = True
        return record
    record.update(
        choice=judgment.action_id, confidence=round(judgment.confidence, 4),
        probability=round(judgment.probability, 4), payload=judgment.payload,
        route_probabilities=(answers or {}).get("route", {}).get("probabilities", {}),
    )
    return record


def outcome(record: dict, threshold: float, metric: str) -> Optional[str]:
    if not record.get("choice"):
        return None
    return record["choice"] if record.get(metric, 0.0) >= threshold else None


def evaluate(records: list[dict], threshold: float, metric: str = "confidence") -> dict:
    n = len(records)
    negatives = [r for r in records if None in r["expect"]]
    positives = [r for r in records if None not in r["expect"]]
    correct = false_routes = neg_routed = reached = payload_checked = payload_ok = 0
    for r in records:
        got = outcome(r, threshold, metric)
        if got in r["expect"]:
            correct += 1
        if got is not None and got not in r["expect"]:
            false_routes += 1
            if None in r["expect"]:
                neg_routed += 1
        if None not in r["expect"] and got in r["expect"]:
            reached += 1
            if r["payloads"]:
                payload_checked += 1
                payload_ok += _norm(r.get("payload", "")) in {_norm(p) for p in r["payloads"]}
    return {
        "threshold": threshold,
        "accuracy": round(correct / n, 4) if n else None,
        "false_routes": false_routes,
        "false_route_rate": round(false_routes / n, 4) if n else None,
        "false_route_rate_on_paste_cases": (round(neg_routed / len(negatives), 4)
                                            if negatives else None),
        "recall": round(reached / len(positives), 4) if positives else None,
        "payload_accuracy": round(payload_ok / payload_checked, 4) if payload_checked else None,
        "payload_checked": payload_checked,
    }


def _percentile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def hardest_paste_cases(records: list[dict], count: int = 4) -> list[dict]:
    """Cases that must be pasted, by how strongly the judge leaned to an action anyway.

    ``confidence_if_chosen`` is the confidence the judge would have reported had
    that action been its choice, which is what the threshold has to stay above.
    """
    rows = []
    for r in records:
        probabilities = r.get("route_probabilities") or {}
        if set(r["expect"]) != {None} or not probabilities:
            continue
        action, p = max(((k, v) for k, v in probabilities.items() if k != routing.NONE),
                        key=lambda kv: kv[1], default=(None, 0.0))
        n = len(probabilities)
        rows.append({"id": r["id"], "route": r["route"], "p_action": round(p, 4),
                     "confidence_if_chosen": round((p - 1 / n) / (1 - 1 / n), 4)})
    return sorted(rows, key=lambda row: -row["confidence_if_chosen"])[:count]


def gate_summary(records: list[dict]) -> dict:
    """What the code gate lets through on plain dictations (Route by voice skips it)."""
    plain = [r for r in records if r["route"] == routing.ROUTE_AUTO]
    addressed = [r for r in plain if None not in r["expect"]]
    paste = [r for r in plain if set(r["expect"]) == {None}]
    return {
        "addressed": len(addressed),
        "addressed_passed": sum(r["gate"] for r in addressed),
        "addressed_stopped_ids": [r["id"] for r in addressed if not r["gate"]],
        "must_paste": len(paste),
        "must_paste_stopped": sum(not r["gate"] for r in paste),
        "must_paste_sent_ids": [r["id"] for r in paste if r["gate"]],
    }


def summarize(records: list[dict]) -> dict:
    asked = [r for r in records if "latency_s" in r]
    latencies = [r["latency_s"] for r in asked]
    tokens = sum(int(r.get("usage", {}).get("input_tokens") or 0) for r in asked)
    costs = [r["usage"]["cost"] for r in asked
             if isinstance(r.get("usage", {}).get("cost"), (int, float))]
    by_threshold = {
        metric: [evaluate(records, t, metric) for t in THRESHOLDS]
        for metric in ("confidence", "probability")
    }
    by_route = {
        route: evaluate([r for r in records if r["route"] == route], routing.ROUTE_CONFIDENCE)
        for route in (routing.ROUTE_AUTO, routing.ROUTE_HOTKEY)
    }
    return {
        "cases": len(records),
        "judged": len(asked),
        "gate_skipped": sum(not r["gate"] for r in records),
        "gate": gate_summary(records),
        "failures": sum(bool(r.get("failed")) for r in asked),
        "latency_s": {
            "median": round(statistics.median(latencies), 4) if latencies else None,
            "p95": _percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
            "over_route_timeout": sum(v > routing.ROUTE_TIMEOUT_S for v in latencies),
        },
        "input_tokens": tokens,
        "input_tokens_per_request": round(tokens / len(asked)) if asked else None,
        "cost_usd": round(sum(costs), 6) if costs else None,
        "list_price_usd": round(tokens * INPUT_USD_PER_MILLION / 1e6, 6),
        "at_chosen_threshold": evaluate(records, routing.ROUTE_CONFIDENCE),
        "at_chosen_threshold_by_route": by_route,
        "hardest_paste_cases": hardest_paste_cases(records),
        "by_threshold": by_threshold,
    }


def _print_summary(variant: str, summary: dict) -> None:
    lat = summary["latency_s"]
    gate = summary["gate"]
    print(f"\n== {variant}: {summary['cases']} cases, {summary['judged']} judged, "
          f"{summary['gate_skipped']} stopped by the gate, {summary['failures']} failed")
    print(f"   gate [auto]: {gate['addressed_passed']}/{gate['addressed']} meant for an action "
          f"reach the judge (stopped: {', '.join(gate['addressed_stopped_ids']) or 'none'}); "
          f"{gate['must_paste_stopped']}/{gate['must_paste']} that must be pasted are stopped "
          f"(sent: {', '.join(gate['must_paste_sent_ids']) or 'none'})")
    if lat["median"] is not None:
        print(f"   latency median {lat['median'] * 1000:.0f} ms, p95 {lat['p95'] * 1000:.0f} ms, "
              f"max {lat['max'] * 1000:.0f} ms, {lat['over_route_timeout']} over "
              f"{routing.ROUTE_TIMEOUT_S} s")
        cost = summary["cost_usd"]
        print(f"   {summary['input_tokens']} input tokens "
              f"({summary['input_tokens_per_request']}/request), "
              f"list price ${summary['list_price_usd']:.5f}"
              + (f", billed ${cost:.5f}" if cost is not None else ""))
    for row in summary["hardest_paste_cases"]:
        print(f"   hardest paste case {row['id']} [{row['route']}]: action p={row['p_action']}, "
              f"confidence if chosen {row['confidence_if_chosen']}")
    for route, row in summary["at_chosen_threshold_by_route"].items():
        print(f"   at {routing.ROUTE_CONFIDENCE} [{route}]: accuracy {row['accuracy']}, "
              f"false routes {row['false_routes']}, recall {row['recall']}, "
              f"payload {row['payload_accuracy']}")
    for metric, rows in summary["by_threshold"].items():
        print(f"   {metric:>11}  thr   acc   false  false/paste  recall  payload")
        for row in rows:
            print(f"   {'':>11}  {row['threshold']:.1f}  {row['accuracy'] or 0:.3f}  "
                  f"{row['false_routes']:>5}  {row['false_route_rate_on_paste_cases'] or 0:>11.3f}"
                  f"  {row['recall'] or 0:.3f}   {row['payload_accuracy'] or 0:.3f}")


def _print_errors(records: list[dict]) -> None:
    for r in records:
        got = outcome(r, routing.ROUTE_CONFIDENCE, "confidence")
        payload_bad = (got in r["expect"] and got is not None and r["payloads"]
                       and _norm(r.get("payload", "")) not in {_norm(p) for p in r["payloads"]})
        if got in r["expect"] and not payload_bad:
            continue
        what = "payload" if payload_bad else ("FALSE ROUTE" if got is not None else "miss")
        print(f"   {what:<11} {r['id']} [{r['route']}] choice={r.get('choice')} "
              f"conf={r.get('confidence')} p={r.get('probability')} gate={r['gate']} "
              f"payload={r.get('payload')!r}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--live", action="store_true", help="ask Jev (costs a few cents)")
    parser.add_argument("--provider", default="typesafe", choices=("typesafe", "openrouter"))
    parser.add_argument("--variant", action="append", choices=sorted(VARIANTS))
    parser.add_argument("--only", help="comma-separated case ids")
    args = parser.parse_args(argv)

    cases = list(CASES)
    if args.only:
        wanted = set(args.only.split(","))
        cases = [c for c in cases if c.id in wanted]
    judge = None
    if args.live:
        from services.typesafe import judge_from_settings

        judge = judge_from_settings({"typesafe_enabled": True,
                                     "typesafe_provider": args.provider})
        if judge is None:
            print(f"No key for {args.provider}; nothing was sent.", file=sys.stderr)
            return 2
    variants = args.variant or ["default"]
    report: dict[str, Any] = {
        "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provider": args.provider if judge is not None else None,
        "model": getattr(judge, "model", None),
        "route_confidence": routing.ROUTE_CONFIDENCE,
        "route_timeout_s": routing.ROUTE_TIMEOUT_S,
        "variants": {},
    }
    tap = _UsageTap() if judge is not None else None
    for name in variants:
        if judge is not None:
            with tap:
                # Warm the connection first: this measures judgments, not TLS handshakes.
                judge.ask({"text": "ok"}, {"q": {"type": "noul",
                                                 "instructions": "Is `text` non-empty?"}})
                records = [run_case(c, judge, VARIANTS[name], tap) for c in cases]
        else:
            records = [run_case(c, None, VARIANTS[name], None) for c in cases]
        summary = summarize(records)
        report["variants"][name] = {"summary": summary, "records": records}
        _print_summary(name, summary)
        if judge is not None:
            _print_errors(records)
        else:
            for r in records:
                print(f"   {r['id']} [{r['route']}] gate={r['gate']} "
                      f"candidates={r.get('candidates')} spans={r.get('payload_options')} "
                      f"chars={r.get('request_chars')}")
    if judge is None:
        return 0
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"{stamp}-{args.provider}.json").write_text(
        json.dumps(report, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    summary_only = {**report, "variants": {k: v["summary"] for k, v in report["variants"].items()}}
    (RESULTS / "summary.json").write_text(
        json.dumps(summary_only, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"\nWrote {RESULTS / (stamp + '-' + args.provider + '.json')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
