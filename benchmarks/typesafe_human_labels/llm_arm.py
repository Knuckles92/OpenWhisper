"""LLM comparison arms (configured cleanup + meeting models via the app's adapter) on E1/E2 subsets, same items as TypeSafe."""
import argparse
import json
import logging
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "D:/coding/whisper_local")
from benchmarks.meeting_mode.typesafe_real_audit import chat, schema
from services.text_llm import get_profile
from ts_client import auroc, prf, save

ROOT = Path("D:/coding/whisper_local")
ACTS = ["inform", "suggest", "offer", "assess", "elicit", "social", "understanding", "minor", "other"]


def models():
    settings = json.loads((ROOT / "openwhisper_settings.json").read_text(encoding="utf-8"))
    return {
        "cleanup": (get_profile(settings["transcript_cleanup_provider"], settings), settings["transcript_cleanup_model"], settings.get("transcript_cleanup_reasoning", "off")),
        "meeting": (get_profile(settings["meeting_llm_provider"], settings), settings["meeting_llm_model"], "off"),
    }


def e1_batches(n=150, batch=10, seed=7):
    from exp1_da_tagging import ACT_CRITERIA, build_jobs
    jobs = build_jobs()
    rng = random.Random(seed)
    subset = rng.sample(jobs, n)
    item = schema({"index": {"type": "integer"}, "act": {"type": "string", "enum": ACTS}, "is_question": {"type": "boolean"},
                   "note_worthy": {"type": "string", "enum": ["0", "1", "2", "3"]}})
    answer_schema = schema({"items": {"type": "array", "items": item}})
    system = ("You label utterances from a meeting transcript. For each item classify the communicative function of `utterance` (spoken by `speaker`, "
              "with `previous_turns` as context) into exactly one category:\n" + "\n".join(f"- {k}: {v}" for k, v in ACT_CRITERIA.items()) +
              "\nAlso answer is_question (does it ask the others for information, opinion, suggestion, offer or confirmation?) and note_worthy "
              "(0 nothing to note; 1 minor detail notes omit; 2 useful concrete fact/proposal/question/opinion; 3 essential decision, commitment, key requirement or finding). "
              "Return JSON with one entry per item index, in order.")
    batches = []
    for i in range(0, len(subset), batch):
        group = subset[i:i + batch]
        user = json.dumps([{"index": k, **g["state"]} for k, g in enumerate(group)])
        batches.append({"ids": [g["id"] for g in group], "gold": [g["gold"] for g in group], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "schema": answer_schema})
    return batches, [g["id"] for g in subset]


def e2_batches(n=120, batch=10, seed=7):
    from exp2_support_links import build_jobs
    pairs, _ = build_jobs()
    rng = random.Random(seed)
    subset = rng.sample(pairs, n)
    item = schema({"index": {"type": "integer"}, "supports": {"type": "boolean"}, "degree": {"type": "string", "enum": ["none", "partial", "full"]}})
    answer_schema = schema({"items": {"type": "array", "items": item}})
    system = ("For each item decide whether what the speaker says in `segment` provides evidence that `claim` describes something that actually happened or was said in this meeting. "
              "Use `surrounding` only to interpret `segment`. Being on the same general topic is not enough; the segment must itself establish at least part of the claim. "
              "supports: true/false. degree: none (establishes nothing), partial (establishes part, e.g. the topic or one position), full (essentially the whole claim). "
              "Return JSON with one entry per item index, in order.")
    batches = []
    for i in range(0, len(subset), batch):
        group = subset[i:i + batch]
        user = json.dumps([{"index": k, **g["state"]} for k, g in enumerate(group)])
        batches.append({"ids": [g["id"] for g in group], "gold": [{"kind": g["kind"]} for g in group], "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "schema": answer_schema})
    return batches, [g["id"] for g in subset]


def run_arm(name, profile, model, reasoning, batches, workers=3):
    results = []
    started = time.perf_counter()

    def call(b):
        try:
            r = chat(profile, model, reasoning, b["messages"], b["schema"])
            return {"ids": b["ids"], "gold": b["gold"], "seconds": r["seconds"], "usage": r["usage"], "items": r["response"]["items"]}
        except Exception as exc:
            return {"ids": b["ids"], "gold": b["gold"], "error": f"{type(exc).__name__}: {str(exc)[:200]}"}
    with ThreadPoolExecutor(workers) as ex:
        for fut in as_completed([ex.submit(call, b) for b in batches]):
            results.append(fut.result())
    wall = time.perf_counter() - started
    ok = [r for r in results if "items" in r]
    lat = sorted(r["seconds"] for r in ok)
    cost = sum((r["usage"] or {}).get("cost", 0) or 0 for r in ok)
    return {"arm": name, "model": model, "reasoning": reasoning, "requests": len(results), "errors": [r["error"] for r in results if "error" in r],
            "median_request_s": round(lat[len(lat) // 2], 3) if lat else None, "min_s": round(lat[0], 3) if lat else None, "max_s": round(lat[-1], 3) if lat else None,
            "wall_s": round(wall, 1), "reported_cost_usd": round(cost, 5), "usage_sample": ok[0]["usage"] if ok else None, "results": results}


def score_e1(arm, ts_rows):
    ts = {r["id"]: r for r in ts_rows if "answers" in r}
    pred = {}
    for r in arm["results"]:
        if "items" not in r:
            continue
        for it in r["items"]:
            if 0 <= it["index"] < len(r["ids"]):
                pred[r["ids"][it["index"]]] = (it, r["gold"][it["index"]])
    ids = [i for i in pred if i in ts]
    if not ids:
        return {"n_scored": 0}
    acc_llm = sum(pred[i][0]["act"] == pred[i][1]["label"] for i in ids) / len(ids)
    acc_ts = sum(ts[i]["answers"]["act"]["choice"] == pred[i][1]["label"] for i in ids) / len(ids)
    q_llm = prf([pred[i][0]["is_question"] for i in ids], [pred[i][1]["label"] == "elicit" for i in ids])
    q_ts = prf([ts[i]["answers"]["is_question"]["noul"] >= 0.5 for i in ids], [pred[i][1]["label"] == "elicit" for i in ids])
    nw_llm = auroc([(int(pred[i][0]["note_worthy"]), int(pred[i][1]["extractive"])) for i in ids])
    nw_ts = auroc([(ts[i]["answers"]["note_worthy"]["score"], int(pred[i][1]["extractive"])) for i in ids])
    return {"n_scored": len(ids), "act_accuracy": {"llm": round(acc_llm, 4), "typesafe_same_items": round(acc_ts, 4)},
            "is_question_f1": {"llm": q_llm["f1"], "typesafe_same_items": q_ts["f1"]},
            "note_worthy_auroc": {"llm": nw_llm, "typesafe_same_items": nw_ts}}


def score_e2(arm, ts_rows):
    ts = {r["id"]: r for r in ts_rows if "answers" in r}
    pred = {}
    for r in arm["results"]:
        if "items" not in r:
            continue
        for it in r["items"]:
            if 0 <= it["index"] < len(r["ids"]):
                pred[r["ids"][it["index"]]] = (it, r["gold"][it["index"]]["kind"])
    ids = [i for i in pred if i in ts]
    if not ids:
        return {"n_scored": 0}
    gold = [pred[i][1] == "positive" for i in ids]
    llm = prf([pred[i][0]["supports"] for i in ids], gold)
    tsp = prf([ts[i]["answers"]["supports"]["noul"] >= 0.5 for i in ids], gold)
    llm_deg = auroc([({"none": 0, "partial": 1, "full": 2}[pred[i][0]["degree"]], int(pred[i][1] == "positive")) for i in ids])
    ts_auc = auroc([(ts[i]["answers"]["supports"]["noul"], int(pred[i][1] == "positive")) for i in ids])
    return {"n_scored": len(ids), "supports_prf": {"llm": llm, "typesafe_same_items@0.5": tsp}, "auroc": {"llm_degree_ordinal": llm_deg, "typesafe_noul": ts_auc}}


def main():
    logging.disable(logging.CRITICAL)
    p = argparse.ArgumentParser()
    p.add_argument("--exp", choices=("e1", "e2"), required=True)
    p.add_argument("--arms", nargs="+", default=["cleanup", "meeting"])
    a = p.parse_args()
    res_dir = Path(__file__).resolve().parent / "results"
    ts_rows = json.loads((res_dir / ("exp1_da_tagging.json" if a.exp == "e1" else "exp2_support_links.json")).read_text(encoding="utf-8"))["rows"]
    batches, subset_ids = (e1_batches if a.exp == "e1" else e2_batches)()
    report = {"exp": a.exp, "subset": len(subset_ids), "arms": {}}
    for name in a.arms:
        profile, model, reasoning = models()[name]
        arm = run_arm(name, profile, model, reasoning, batches)
        arm["scores"] = (score_e1 if a.exp == "e1" else score_e2)(arm, ts_rows)
        report["arms"][name] = arm
        print(json.dumps({k: v for k, v in arm.items() if k != "results"}, indent=1, default=str), flush=True)
    print("saved", save(f"llm_arm_{a.exp}.json", report))


if __name__ == "__main__":
    main()
