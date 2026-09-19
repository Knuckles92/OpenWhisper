"""E8: (a) which later utterance answers a question; (b) stance of a response (agree/object/partial/uncertain/elaborate) vs AMI adjacency pairs."""
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ami_nxt import ANN, NS, load_dialogue_acts, load_words, roles, speaker_tag
from ts_client import auroc, prf, run_jobs, save, summarize

MEETINGS = ("ES2008a", "IS1008b", "TS3005a", "ES2008b")
AP = {"apt_1": "support", "apt_2": "object", "apt_3": "uncertain", "apt_4": "partial", "apt_5": "elaboration"}
STANCE = {"support": "The response agrees with, accepts, or positively assesses what the source utterance said or proposed.",
          "object": "The response disagrees with, rejects, or negatively assesses the source utterance.",
          "partial": "The response agrees with part of the source utterance but qualifies, limits, or hedges it.",
          "uncertain": "The response expresses uncertainty or does not commit either way about the source utterance.",
          "elaboration": "The response adds detail, explanation, or continuation to the source utterance without assessing it."}


def load_pairs(mid):
    path = ANN / "dialogueActs" / f"{mid}.adjacency-pairs.xml"
    rows = []
    for ap in ET.parse(path).getroot().iter("adjacency-pair"):
        rel = src = tgt = None
        for p in ap.iter(NS + "pointer"):
            ref = p.get("href").split("id(")[1].rstrip(")")
            if p.get("role") == "type":
                rel = AP.get(ref)
            elif p.get("role") == "source":
                src = ref
            elif p.get("role") == "target":
                tgt = ref
        if rel and src and tgt:
            rows.append({"type": rel, "source": src, "target": tgt})
    return rows


def build_jobs(seed=13):
    answer_jobs, stance_jobs = [], []
    for mid in MEETINGS:
        words = load_words(mid)
        acts = load_dialogue_acts(mid, words)
        by_id = {a["id"]: a for a in acts}
        role_map = roles(mid)
        tag = lambda a: {"speaker": speaker_tag(a["speaker"], role_map), "text": a["text"]}
        pairs = [p for p in load_pairs(mid) if p["source"] in by_id and p["target"] in by_id]
        targets_by_source = {}
        for p in pairs:
            targets_by_source.setdefault(p["source"], set()).add(p["target"])
        for p in pairs:
            s, t = by_id[p["source"]], by_id[p["target"]]
            if s["index"] >= t["index"]:
                continue
            between = [tag(a) for a in acts[s["index"] + 1: t["index"]]][-4:]
            stance_jobs.append({"id": f"stance|{mid}|{p['source']}|{p['target']}", "meeting": mid, "gold": p["type"],
                                "state": {"source": tag(s), "between": between, "response": tag(t)},
                                "questions": {"stance": {"type": "choice", "criteria": STANCE,
                                                         "instructions": "`response` was annotated as responding to `source` in a meeting. What stance does `response` take toward `source`? `between` lists utterances spoken in between, for context only."},
                                              "agrees": {"type": "noul", "instructions": "Does `response` express agreement with or acceptance of what `source` says or proposes?"},
                                              "disagrees": {"type": "noul", "instructions": "Does `response` express disagreement with, objection to, or rejection of what `source` says or proposes?"}}})
        # (a) answer pairing for questions
        seen = set()
        for p in pairs:
            s = by_id[p["source"]]
            if s["label"] != "elicit" or s["id"] in seen or s["words"] < 3:
                continue
            seen.add(s["id"])
            golds = {g for g in targets_by_source[s["id"]] if by_id[g]["index"] > s["index"] and by_id[g]["speaker"] != s["speaker"]}
            cands = [a for a in acts[s["index"] + 1:] if a["speaker"] != s["speaker"] and a["words"] >= 2 and a["start"] - s["start"] <= 45][:8]
            if not golds or not any(c["id"] in golds for c in cands):
                continue
            cands = cands[:6] if any(c["id"] in golds for c in cands[:6]) else cands
            first_other = cands[0]["id"] if cands else None
            criteria = {f"c{i+1}": f"Candidate c{i+1}" for i in range(len(cands))}
            criteria["none"] = "None of the candidates responds to or answers the question."
            answer_jobs.append({"id": f"answer|{mid}|{s['id']}", "meeting": mid, "n_candidates": len(cands),
                                "gold": [f"c{i+1}" for i, c in enumerate(cands) if c["id"] in golds],
                                "first_other_is_gold": first_other in golds,
                                "state": {"question": tag(s), "candidates": {f"c{i+1}": tag(c) for i, c in enumerate(cands)}},
                                "questions": {"answer": {"type": "choice", "criteria": criteria,
                                                         "instructions": "`question` was asked in a meeting. `candidates` are the next utterances by other participants, in time order. Which candidate directly responds to or answers the question? Choose none if no candidate does."},
                                              "answered": {"type": "noul", "instructions": "Do the `candidates`, taken together, contain a direct answer or response to `question`?"}}})
    return answer_jobs, stance_jobs


def evaluate(arows, srows):
    out = {}
    aok = [r for r in arows if "answers" in r]
    if aok:
        hit = sum(r["answers"]["answer"]["choice"] in r["gold"] for r in aok)
        out["answer_pairing"] = {"n": len(aok), "top1_hit": round(hit / len(aok), 4),
                                 "baseline_first_other_speaker": round(sum(r["first_other_is_gold"] for r in aok) / len(aok), 4),
                                 "chance": round(sum(len(r["gold"]) / r["n_candidates"] for r in aok) / len(aok), 4),
                                 "none_chosen": sum(r["answers"]["answer"]["choice"] == "none" for r in aok),
                                 "mean_answered_noul": round(sum(r["answers"]["answered"]["noul"] for r in aok) / len(aok), 3)}
        for thr in (0.5, 0.7):
            sub = [r for r in aok if r["answers"]["answer"]["confidence"] >= thr]
            out["answer_pairing"][f"conf>={thr}"] = {"coverage": round(len(sub) / len(aok), 3),
                                                     "top1_hit": round(sum(r["answers"]["answer"]["choice"] in r["gold"] for r in sub) / len(sub), 4) if sub else None}
    sok = [r for r in srows if "answers" in r]
    if sok:
        acc = sum(r["answers"]["stance"]["choice"] == r["gold"] for r in sok) / len(sok)
        maj = Counter(r["gold"] for r in sok).most_common(1)[0]
        per = {}
        for lab in STANCE:
            per[lab] = {**prf([r["answers"]["stance"]["choice"] == lab for r in sok], [r["gold"] == lab for r in sok]), "support": sum(r["gold"] == lab for r in sok)}
        conf = Counter((r["gold"], r["answers"]["stance"]["choice"]) for r in sok)
        out["stance"] = {"n": len(sok), "accuracy": round(acc, 4), "majority": {"label": maj[0], "accuracy": round(maj[1] / len(sok), 4)}, "per_class": per,
                         "top_confusions": sorted(((g, p, v) for (g, p), v in conf.items() if g != p), key=lambda x: -x[2])[:6],
                         "agrees_auroc_vs_support": auroc([(r["answers"]["agrees"]["noul"], int(r["gold"] == "support")) for r in sok]),
                         "disagrees_auroc_vs_object": auroc([(r["answers"]["disagrees"]["noul"], int(r["gold"] == "object")) for r in sok]),
                         "disagrees_auroc_vs_object_or_partial": auroc([(r["answers"]["disagrees"]["noul"], int(r["gold"] in ("object", "partial"))) for r in sok]),
                         "disagrees>=0.5_prf_vs_object": prf([r["answers"]["disagrees"]["noul"] >= 0.5 for r in sok], [r["gold"] == "object" for r in sok]),
                         "agrees>=0.5_prf_vs_support": prf([r["answers"]["agrees"]["noul"] >= 0.5 for r in sok], [r["gold"] == "support" for r in sok])}
        for thr in (0.5, 0.7):
            sub = [r for r in sok if r["answers"]["stance"]["confidence"] >= thr]
            out["stance"][f"conf>={thr}"] = {"coverage": round(len(sub) / len(sok), 3), "accuracy": round(sum(r["answers"]["stance"]["choice"] == r["gold"] for r in sub) / len(sub), 4) if sub else None}
    return out


def main():
    a, s = build_jobs()
    print("answer jobs", len(a), "stance jobs", len(s), Counter(j["gold"] for j in s))
    rows, wall = asyncio.run(run_jobs(a + s, label="E8"))
    save("exp8_responses.raw.json", {"rows": rows})
    summary = summarize(rows, wall)
    metrics = evaluate([r for r in rows if r["id"].startswith("answer|")], [r for r in rows if r["id"].startswith("stance|")])
    print(json.dumps({"summary": summary, "metrics": metrics}, indent=1))
    print("saved", save("exp8_responses.json", {"meetings": MEETINGS, "summary": summary, "metrics": metrics, "rows": rows}))


if __name__ == "__main__":
    main()
