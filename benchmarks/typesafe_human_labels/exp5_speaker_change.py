"""E5: text-only speaker-change detection between consecutive utterances vs AMI speaker labels."""
import asyncio
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ami_nxt import load_dialogue_acts, load_words
from ts_client import auroc, prf, run_jobs, save, summarize

MEETINGS = ("ES2008a", "IS1008b", "TS3005a")
PER_CLASS = 70


def build_jobs(seed=5):
    rng = random.Random(seed)
    jobs = []
    base_rates = {}
    for mid in MEETINGS:
        acts = load_dialogue_acts(mid, load_words(mid))
        pairs = [(acts[i - 1], acts[i]) for i in range(1, len(acts)) if acts[i - 1]["words"] >= 3 and acts[i]["words"] >= 3]
        diff = [p for p in pairs if p[0]["speaker"] != p[1]["speaker"]]
        same = [p for p in pairs if p[0]["speaker"] == p[1]["speaker"]]
        base_rates[mid] = {"pairs": len(pairs), "different_speaker_rate": round(len(diff) / len(pairs), 3)}
        for kind, pool in (("different", diff), ("same", same)):
            for a, b in rng.sample(pool, min(PER_CLASS, len(pool))):
                earlier = [x["text"] for x in acts[max(0, a["index"] - 3): a["index"]]]
                jobs.append({"id": f"{mid}|{b['id']}", "meeting": mid, "gold": int(kind == "different"),
                             "gap_s": round(b["start"] - a["end"], 2), "overlap": b["start"] < a["end"],
                             "state": {"earlier": earlier, "first": a["text"], "second": b["text"]},
                             "questions": {"different_speaker": {"type": "noul", "instructions": "In this meeting transcript without speaker labels, is `second` most likely spoken by a different person than `first`? A reply, answer, agreement, objection, or question about `first` suggests a different person; continuing the same sentence, thought, list or explanation suggests the same person. `earlier` is context only."}}})
    return jobs, base_rates


def evaluate(rows):
    ok = [r for r in rows if "answers" in r]
    pairs = [(r["answers"]["different_speaker"]["noul"], r["gold"]) for r in ok]
    out = {"n": len(ok), "auroc": auroc(pairs)}
    for thr in (0.5,):
        pred = [p >= thr for p, _ in pairs]
        out[f"accuracy@{thr}"] = round(sum((p >= thr) == bool(l) for p, l in pairs) / len(pairs), 4)
        out[f"prf@{thr}"] = prf(pred, [bool(l) for _, l in pairs])
    # decisive subset
    dec = [(p, l) for p, l in pairs if p <= 0.2 or p >= 0.8]
    out["decisive(<=0.2 or >=0.8)"] = {"coverage": round(len(dec) / len(pairs), 3),
                                        "accuracy": round(sum((p >= 0.5) == bool(l) for p, l in dec) / len(dec), 4) if dec else None}
    # timing baseline: gap >= 0.5s or overlap -> different
    gap_pairs = [(r["gap_s"], r["gold"]) for r in ok]
    out["baseline_gap_auroc"] = auroc(gap_pairs)
    out["baseline_overlap_rule"] = prf([r["overlap"] for r in ok], [bool(r["gold"]) for r in ok])
    return out


def main():
    jobs, base = build_jobs()
    print("jobs", len(jobs), base)
    rows, wall = asyncio.run(run_jobs(jobs, label="E5"))
    summary = summarize(rows, wall)
    metrics = evaluate(rows)
    metrics["base_rates"] = base
    print(json.dumps({"summary": summary, "metrics": metrics}, indent=1))
    print("saved", save("exp5_speaker_change.json", {"meetings": MEETINGS, "summary": summary, "metrics": metrics, "rows": rows}))


if __name__ == "__main__":
    main()
