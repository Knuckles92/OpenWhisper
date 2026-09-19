"""E2: claim<->segment evidence support vs AMI human abstractive/extractive links (real-data citation check)."""
import asyncio
import json
import random
import sys
from collections import Counter

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from ami_nxt import load_abstractive, load_dialogue_acts, load_summlinks, load_words, roles, speaker_tag
from ts_client import auroc, prf, run_jobs, save, summarize

MEETINGS = ("ES2008a", "IS1008b", "TS3005a")
MIN_WORDS = 5


def build_jobs(seed=7):
    rng = random.Random(seed)
    pair_jobs, shortlist_jobs = [], []
    for mid in MEETINGS:
        words = load_words(mid)
        acts = load_dialogue_acts(mid, words)
        role_map = roles(mid)
        by_id = {a["id"]: a for a in acts}
        links = load_summlinks(mid)
        linked_any = set().union(*links.values()) if links else set()
        sentences = [s for s in load_abstractive(mid) if s["id"] in links]

        def ctx(a):
            i = a["index"]
            return [{"speaker": speaker_tag(p["speaker"], role_map), "text": p["text"]} for p in acts[max(0, i - 2): i] + acts[i + 1: i + 3]]

        def seg(a):
            return {"speaker": speaker_tag(a["speaker"], role_map), "text": a["text"]}

        for s in sentences:
            pos_all = [by_id[d] for d in links[s["id"]] if d in by_id and by_id[d]["words"] >= MIN_WORDS]
            if not pos_all:
                continue
            pos = rng.sample(pos_all, min(3, len(pos_all)))
            pos_times = [p["start"] for p in pos_all]
            far = [a for a in acts if a["words"] >= MIN_WORDS and a["id"] not in links[s["id"]] and all(abs(a["start"] - t) > 120 for t in pos_times)]
            near = [a for a in acts if a["words"] >= MIN_WORDS and a["id"] not in links[s["id"]] and a["id"] in linked_any
                    and any(abs(a["start"] - t) <= 45 for t in pos_times)]
            far_s = rng.sample(far, min(3, len(far)))
            near_s = rng.sample(near, min(2, len(near)))
            cands = [(a, "positive") for a in pos] + [(a, "far_negative") for a in far_s] + [(a, "near_negative") for a in near_s]
            for a, kind in cands:
                pair_jobs.append({
                    "id": f"{s['id']}|{a['id']}", "meeting": mid, "sentence_id": s["id"], "section": s["section"], "kind": kind,
                    "state": {"claim": s["text"], "segment": seg(a), "surrounding": ctx(a)},
                    "questions": {
                        "supports": {"type": "noul", "instructions": "Does what the speaker says in `segment` provide evidence that `claim` describes something that actually happened or was said in this meeting? Use `surrounding` only to interpret `segment`. Being on the same general topic is not enough; the segment must itself establish at least part of the claim."},
                        "degree": {"type": "choice", "criteria": {
                            "none": "The segment does not establish any part of the claim.",
                            "partial": "The segment establishes part of the claim, for example the topic or one participant's position, but not the whole claim.",
                            "full": "The segment on its own establishes essentially the whole claim."},
                            "instructions": "How much of `claim` is established by `segment` itself, using `surrounding` only for interpretation?"},
                    }})
            # shortlist selection: 2 pos + up to 2 far + up to 2 near
            short = [(a, "positive") for a in pos[:2]] + [(a, "far_negative") for a in far_s[:2]] + [(a, "near_negative") for a in near_s[:2]]
            if len(short) >= 4 and sum(k == "positive" for _, k in short) >= 1:
                rng.shuffle(short)
                criteria = {f"c{i+1}": f"Candidate c{i+1}" for i in range(len(short))}
                criteria["none"] = "No candidate supports the claim."
                shortlist_jobs.append({
                    "id": f"short|{s['id']}", "meeting": mid, "sentence_id": s["id"], "section": s["section"], "n_candidates": len(short),
                    "positives": [f"c{i+1}" for i, (_, k) in enumerate(short) if k == "positive"],
                    "state": {"claim": s["text"], "candidates": {f"c{i+1}": seg(a) for i, (a, _) in enumerate(short)}},
                    "questions": {"best": {"type": "choice", "criteria": criteria,
                                           "instructions": "Which entry in `candidates` most directly supports `claim` about this meeting? Pick none only if no candidate establishes any part of the claim."}}})
    return pair_jobs, shortlist_jobs


def evaluate(pairs, shorts):
    ok = [r for r in pairs if "answers" in r]
    out = {"n_pairs": len(ok), "kinds": dict(Counter(r["kind"] for r in ok))}
    lab = lambda r: int(r["kind"] == "positive")
    all_pairs = [(r["answers"]["supports"]["noul"], lab(r)) for r in ok]
    out["auroc_all"] = auroc(all_pairs)
    out["auroc_pos_vs_far"] = auroc([(r["answers"]["supports"]["noul"], lab(r)) for r in ok if r["kind"] in ("positive", "far_negative")])
    out["auroc_pos_vs_near"] = auroc([(r["answers"]["supports"]["noul"], lab(r)) for r in ok if r["kind"] in ("positive", "near_negative")])
    for thr in (0.5, 0.8):
        out[f"supports>={thr}"] = prf([p >= thr for p, _ in all_pairs], [bool(l) for _, l in all_pairs])
    deg = Counter((r["kind"], r["answers"]["degree"]["choice"]) for r in ok)
    out["degree_by_kind"] = {f"{k}|{c}": v for (k, c), v in sorted(deg.items())}
    out["degree_none_as_negative"] = prf([r["answers"]["degree"]["choice"] != "none" for r in ok], [bool(lab(r)) for r in ok])
    by_section = {}
    for sec in sorted(set(r["section"] for r in ok)):
        sub = [(r["answers"]["supports"]["noul"], lab(r)) for r in ok if r["section"] == sec]
        by_section[sec] = {"n": len(sub), "auroc": auroc(sub)}
    out["by_section"] = by_section
    mean = lambda xs: round(sum(xs) / len(xs), 3) if xs else None
    out["mean_noul_by_kind"] = {k: mean([r["answers"]["supports"]["noul"] for r in ok if r["kind"] == k]) for k in ("positive", "near_negative", "far_negative")}
    sok = [r for r in shorts if "answers" in r]
    if sok:
        hit = sum(r["answers"]["best"]["choice"] in r["positives"] for r in sok)
        chance = sum(len(r["positives"]) / r["n_candidates"] for r in sok) / len(sok)
        out["shortlist"] = {"n": len(sok), "top1_hit_rate": round(hit / len(sok), 4), "chance": round(chance, 4),
                            "none_chosen": sum(r["answers"]["best"]["choice"] == "none" for r in sok)}
    return out


def main():
    pairs, shorts = build_jobs()
    print("pair jobs", len(pairs), Counter(j["kind"] for j in pairs), "shortlists", len(shorts))
    rows, wall = asyncio.run(run_jobs(pairs + shorts, label="E2"))
    save("exp2_support_links.raw.json", {"rows": rows, "wall": wall})
    prow = [r for r in rows if not r["id"].startswith("short|")]
    srow = [r for r in rows if r["id"].startswith("short|")]
    summary = summarize(rows, wall)
    metrics = evaluate(prow, srow)
    print(json.dumps({"summary": summary, "metrics": metrics}, indent=1))
    print("saved", save("exp2_support_links.json", {"meetings": MEETINGS, "summary": summary, "metrics": metrics, "rows": rows}))


if __name__ == "__main__":
    main()
