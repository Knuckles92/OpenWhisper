"""Bounded-concurrency TypeSafe caller that records latency, tokens, and validation errors."""
import asyncio
import json
import os
import statistics
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path("D:/coding/whisper_local")
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
OUT = Path(__file__).resolve().parent / "results"
OUT.mkdir(exist_ok=True)


def api_key():
    key = os.environ.get("TYPESAFE_API_KEY") or dotenv_values(ROOT / ".env").get("TYPESAFE_API_KEY")
    if not key:
        raise SystemExit("TYPESAFE_API_KEY unavailable")
    return key


def validate(body, questions):
    answers = body["answers"]
    if set(answers) != set(questions):
        raise ValueError("question id mismatch")
    for name, q in questions.items():
        a = answers[name]
        if a["type"] != q["type"]:
            raise ValueError(f"type mismatch on {name}")
        if q["type"] == "choice" and a["choice"] not in q["criteria"]:
            raise ValueError(f"unknown choice on {name}: {a['choice']}")
        if q["type"] == "noul" and not 0 <= a["noul"] <= 1:
            raise ValueError("bad probability")
        if q["type"] == "score" and not 0 <= a["score"] <= len(q["criteria"]) - 1:
            raise ValueError("bad score")


async def run_jobs(jobs, concurrency=4, spacing=0.05, timeout=30, progress_every=100, label=""):
    """jobs: list of {'id', 'state', 'questions', ...meta}. Returns rows with response/seconds/error."""
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()
    last = [0.0]
    rows = []
    started = time.perf_counter()
    async with httpx.AsyncClient(headers={"Authorization": "Bearer " + api_key()}, timeout=timeout,
                                 limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)) as client:
        async def one(job):
            payload = {"model": MODEL, "state": job["state"], "questions": job["questions"]}
            row = {k: v for k, v in job.items() if k not in ("state", "questions")}
            row["request_tokens_estimate"] = len(json.dumps(payload)) // 4
            async with sem:
                async with lock:
                    wait = spacing - (time.perf_counter() - last[0])
                    if wait > 0:
                        await asyncio.sleep(wait)
                    last[0] = time.perf_counter()
                t0 = time.perf_counter()
                try:
                    r = await client.post(ENDPOINT, json=payload)
                    row["status"] = r.status_code
                    r.raise_for_status()
                    body = r.json()
                    validate(body, job["questions"])
                    row["answers"] = body["answers"]
                    row["usage"] = body.get("usage", {})
                except Exception as exc:  # record, never retry
                    row["error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
                row["seconds"] = time.perf_counter() - t0
                rows.append(row)
                if progress_every and len(rows) % progress_every == 0:
                    print(json.dumps({"label": label, "done": len(rows), "total": len(jobs),
                                      "elapsed": round(time.perf_counter() - started, 1)}), flush=True)
                return row
        await asyncio.gather(*(one(j) for j in jobs))
    rows.sort(key=lambda r: jobs_index[r["id"]] if (jobs_index := {j["id"]: i for i, j in enumerate(jobs)}) else 0)
    return rows, time.perf_counter() - started


def summarize(rows, wall):
    ok = [r for r in rows if "answers" in r]
    tokens = sum(r.get("usage", {}).get("input_tokens", 0) for r in ok)
    lat = [r["seconds"] for r in ok]
    return {
        "requests": len(rows), "errors": len(rows) - len(ok),
        "error_samples": [r["error"] for r in rows if "error" in r][:5],
        "median_s": round(statistics.median(lat), 3) if lat else None,
        "p95_s": round(sorted(lat)[int(len(lat) * 0.95) - 1], 3) if len(lat) >= 20 else None,
        "wall_s": round(wall, 1), "input_tokens": tokens, "est_cost_usd": round(tokens * 0.042 / 1e6, 5),
    }


def save(name, payload):
    path = OUT / name
    path.write_text(json.dumps(payload, indent=1, default=list), encoding="utf-8")
    return str(path)


def auroc(scores_labels):
    """Rank-based AUROC; scores_labels = [(score, label in {0,1})]."""
    pos = [s for s, l in scores_labels if l == 1]
    neg = [s for s, l in scores_labels if l == 0]
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else (0.5 if p == n else 0.0)
    return round(wins / (len(pos) * len(neg)), 4)


def prf(pred_true, gold_true):
    tp = sum(1 for p, g in zip(pred_true, gold_true) if p and g)
    fp = sum(1 for p, g in zip(pred_true, gold_true) if p and not g)
    fn = sum(1 for p, g in zip(pred_true, gold_true) if not p and g)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec is not None and rec is not None else None)
    return {"tp": tp, "fp": fp, "fn": fn, "precision": _r(prec), "recall": _r(rec), "f1": _r(f1)}


def _r(x):
    return None if x is None else round(x, 4)


if __name__ == "__main__":
    jobs = [{"id": "smoke", "state": {"utterance": "I'll send the slides tonight."},
             "questions": {"offer": {"type": "noul", "instructions": "Does the speaker of `utterance` volunteer to do something themselves?"},
                           "act": {"type": "choice", "instructions": "What kind of utterance is `utterance`?",
                                   "criteria": {"offer": "speaker volunteers", "question": "asks something", "other": "anything else"}}}}]
    rows, wall = asyncio.run(run_jobs(jobs, progress_every=0))
    print(json.dumps(rows, indent=1)[:800])
    print(summarize(rows, wall))
