"""E1b: same dialogue acts as E1 but with ASR-like text (lowercase, punctuation stripped) to measure robustness."""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from exp1_da_tagging import build_jobs
from ts_client import auroc, prf, run_jobs, save, summarize


def asrify(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", text.lower())).strip()


def main():
    jobs = build_jobs()
    for j in jobs:
        s = j["state"]
        s["utterance"] = asrify(s["utterance"])
        s["previous_turns"] = [{"speaker": p["speaker"], "text": asrify(p["text"])} for p in s["previous_turns"]]
        j["questions"] = {k: v for k, v in j["questions"].items() if k in ("act", "is_question", "is_offer", "is_suggestion")}
    rows, wall = asyncio.run(run_jobs(jobs, label="E1b"))
    ok = [r for r in rows if "answers" in r]
    base = json.loads((Path(__file__).resolve().parent / "results" / "exp1_da_tagging.json").read_text(encoding="utf-8"))
    punct = {r["id"]: r for r in base["rows"] if "answers" in r}
    out = {"n": len(ok), "act_accuracy_no_punct": round(sum(r["answers"]["act"]["choice"] == r["gold"]["label"] for r in ok) / len(ok), 4),
           "act_accuracy_with_punct_same_items": round(sum(punct[r["id"]]["answers"]["act"]["choice"] == r["gold"]["label"] for r in ok if r["id"] in punct) / len(ok), 4)}
    for q, gold_fn in (("is_question", lambda r: r["gold"]["label"] == "elicit"), ("is_offer", lambda r: r["gold"]["type"] == "off"), ("is_suggestion", lambda r: r["gold"]["type"] == "sug")):
        pairs = [(r["answers"][q]["noul"], int(gold_fn(r))) for r in ok]
        ppairs = [(punct[r["id"]]["answers"][q]["noul"], int(gold_fn(r))) for r in ok if r["id"] in punct]
        out[q] = {"auroc_no_punct": auroc(pairs), "auroc_with_punct": auroc(ppairs),
                  "f1@0.5_no_punct": prf([p >= 0.5 for p, _ in pairs], [bool(l) for _, l in pairs])["f1"],
                  "f1@0.5_with_punct": prf([p >= 0.5 for p, _ in ppairs], [bool(l) for _, l in ppairs])["f1"]}
    out["rule_question_?_no_punct_f1"] = 0.0
    print(json.dumps({"summary": summarize(rows, wall), "metrics": out}, indent=1))
    print("saved", save("exp1b_da_nopunct.json", {"summary": summarize(rows, wall), "metrics": out, "rows": rows}))


if __name__ == "__main__":
    main()
