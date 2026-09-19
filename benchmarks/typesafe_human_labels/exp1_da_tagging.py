"""E1: dialogue-act tagging, addressee, and note-worthiness vs AMI human labels (text-only, live-style context)."""
import asyncio
import json
import re
import sys
from collections import Counter

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from ami_nxt import load_dialogue_acts, load_extractive, load_words, roles, speaker_tag
from ts_client import auroc, prf, run_jobs, save, summarize

MEETINGS = ("ES2008a", "IS1008b", "TS3005a")
ROLE_NAMES = {"PM": "project manager", "ID": "industrial designer", "UI": "user interface designer", "ME": "marketing expert"}
ACT_CRITERIA = {
    "inform": "States facts, explanations, plans already fixed, or the speaker's own situation; not proposing, evaluating, or asking.",
    "suggest": "Proposes an idea, option, plan, or course of action for the group or another person to consider or adopt.",
    "offer": "The speaker volunteers or commits to do something themselves (I'll..., I can..., let me...).",
    "assess": "Evaluates or reacts to something said or shown: agreement, disagreement, judgment, opinion of quality or value.",
    "elicit": "Asks the others for information, an opinion, a suggestion or offer, or checks whether they understood or agree.",
    "social": "Positive or negative interpersonal expression: greetings, thanks, jokes, praise, apology, frustration; not task content.",
    "understanding": "Comments on whether the speaker themselves heard or understood, e.g. 'sorry, what?', 'oh I see', 'right'.",
    "minor": "Backchannel, stall, or unfinished fragment carrying no standalone content, e.g. 'mm-hmm', 'um', 'so the...'.",
    "other": "Utterance with task content that fits none of the other categories.",
}


def rule_act(text):
    t = text.lower().strip()
    words = re.findall(r"[a-z']+", t)
    if len(words) <= 2 and not t.endswith("?"):
        return "minor"
    if t.endswith("?"):
        return "elicit"
    if re.search(r"\b(i'll|i will|i can|let me|i could|i'm going to|i'm gonna)\b", t):
        return "offer"
    if re.search(r"\b(we could|we should|maybe we|let's|how about|what about|you could|why don't)\b", t):
        return "suggest"
    if re.search(r"\b(good|great|nice|bad|agree|right|exactly|true|okay yeah|i think that's|that's a)\b", t):
        return "assess"
    return "inform"


def build_jobs():
    jobs = []
    gold_ext = {}
    for mid in MEETINGS:
        words = load_words(mid)
        acts = load_dialogue_acts(mid, words)
        ext = load_extractive(mid, acts)
        role_map = roles(mid)
        participants = {k: f"{v} ({ROLE_NAMES.get(v, v)})" for k, v in role_map.items()}
        others = sorted(role_map)
        for a in acts:
            prev = [{"speaker": speaker_tag(p["speaker"], role_map), "text": p["text"]} for p in acts[max(0, a["index"] - 6): a["index"]]]
            addr_criteria = {"group": "Addressed to the whole group or to no one in particular."}
            for o in others:
                if o != a["speaker"]:
                    addr_criteria[o] = f"Addressed specifically to participant {o}, the {ROLE_NAMES.get(role_map[o], role_map[o])}."
            addr_criteria["unclear"] = "Cannot tell from the text who is addressed."
            state = {"participants": participants, "speaker": speaker_tag(a["speaker"], role_map),
                     "utterance": a["text"], "previous_turns": prev}
            questions = {
                "act": {"type": "choice", "criteria": ACT_CRITERIA,
                        "instructions": "Classify the communicative function of `utterance`, spoken by `speaker`, using `previous_turns` only for context. Choose the single best category."},
                "is_question": {"type": "noul", "instructions": "Is `utterance` asking the other participants for information, an opinion, a suggestion, an offer, or confirmation that they understood or agree?"},
                "is_offer": {"type": "noul", "instructions": "Does `speaker` in `utterance` volunteer or commit to do something themselves, now or later?"},
                "is_suggestion": {"type": "noul", "instructions": "Does `utterance` propose an idea, option, plan or action for the group or another person to consider or adopt? A question, an offer to do it oneself, or a plain statement of fact is not a suggestion."},
                "note_worthy": {"type": "score", "instructions": "How much does `utterance`, in the context of `previous_turns`, deserve a place in written notes for this meeting?",
                                "criteria": ["Nothing to note: filler, backchannel, social talk, or a fragment.",
                                             "Minor: adds a small detail or reaction that notes would normally omit.",
                                             "Useful: a concrete fact, proposal, question, or opinion that a good note-taker might record.",
                                             "Essential: a decision, commitment, key requirement, or important finding that notes must include."]},
                "addressee": {"type": "choice", "criteria": addr_criteria,
                              "instructions": "Who is `utterance` addressed to? Use `previous_turns` to see who `speaker` is responding to. Use group unless the text or context clearly singles out one participant."},
            }
            gold_addr = None
            if a["addressee"]:
                parts = [p.strip() for p in a["addressee"].split(",") if p.strip()]
                gold_addr = parts[0] if len(parts) == 1 else "group"
            jobs.append({"id": a["id"], "meeting": mid, "state": state, "questions": questions,
                         "gold": {"type": a["type"], "label": a["label"], "extractive": a["id"] in ext, "addressee": gold_addr,
                                  "words": a["words"], "speaker": a["speaker"]}, "rule_act": rule_act(a["text"]),
                         "rule_question": a["text"].strip().endswith("?")})
        gold_ext[mid] = len(ext)
    return jobs


def evaluate(rows):
    ok = [r for r in rows if "answers" in r]
    out = {}
    labels = sorted(ACT_CRITERIA)
    for name, pred_fn in (("typesafe_act", lambda r: r["answers"]["act"]["choice"]), ("rule_act", lambda r: r["rule_act"])):
        conf = Counter((r["gold"]["label"], pred_fn(r)) for r in ok)
        acc = sum(v for (g, p), v in conf.items() if g == p) / len(ok)
        per = {}
        for lab in labels:
            gold = [r["gold"]["label"] == lab for r in ok]
            pred = [pred_fn(r) == lab for r in ok]
            per[lab] = {**prf(pred, gold), "support": sum(gold)}
        macro = sum(v["f1"] or 0 for v in per.values()) / len(labels)
        top_conf = sorted(((g, p, v) for (g, p), v in conf.items() if g != p), key=lambda x: -x[2])[:8]
        out[name] = {"accuracy": round(acc, 4), "macro_f1": round(macro, 4), "per_class": per, "top_confusions": top_conf}
    majority = Counter(r["gold"]["label"] for r in ok).most_common(1)[0]
    out["majority_baseline"] = {"label": majority[0], "accuracy": round(majority[1] / len(ok), 4)}
    # confidence-gated accuracy
    for thr in (0.5, 0.7, 0.8):
        sub = [r for r in ok if r["answers"]["act"]["confidence"] >= thr]
        out[f"act_accuracy_conf>={thr}"] = {"coverage": round(len(sub) / len(ok), 3),
                                            "accuracy": round(sum(r["answers"]["act"]["choice"] == r["gold"]["label"] for r in sub) / len(sub), 4) if sub else None}
    # nouls
    for q, gold_fn in (("is_question", lambda r: r["gold"]["label"] == "elicit"), ("is_offer", lambda r: r["gold"]["type"] == "off"),
                       ("is_suggestion", lambda r: r["gold"]["type"] == "sug")):
        pairs = [(r["answers"][q]["noul"], int(gold_fn(r))) for r in ok]
        out[q] = {"auroc": auroc(pairs), "positives": sum(l for _, l in pairs),
                  "f1@0.5": prf([p >= 0.5 for p, _ in pairs], [bool(l) for _, l in pairs]),
                  "f1@0.8": prf([p >= 0.8 for p, _ in pairs], [bool(l) for _, l in pairs])}
    out["rule_question_?"] = prf([r["rule_question"] for r in ok], [r["gold"]["label"] == "elicit" for r in ok])
    # note-worthiness vs extractive summary membership
    pairs = [(r["answers"]["note_worthy"]["score"], int(r["gold"]["extractive"])) for r in ok]
    length_pairs = [(r["gold"]["words"], int(r["gold"]["extractive"])) for r in ok]
    out["note_worthy_vs_extractive"] = {"auroc_score": auroc(pairs), "auroc_word_count_baseline": auroc(length_pairs),
                                        "positives": sum(l for _, l in pairs), "n": len(pairs)}
    for thr in (1.0, 1.5, 2.0, 2.5):
        pred = [s >= thr for s, _ in pairs]
        out["note_worthy_vs_extractive"][f"score>={thr}"] = {**prf(pred, [bool(l) for _, l in pairs]), "flagged_fraction": round(sum(pred) / len(pred), 3)}
    # addressee
    addr = [r for r in ok if r["gold"]["addressee"]]
    if addr:
        acc = sum(r["answers"]["addressee"]["choice"] == r["gold"]["addressee"] for r in addr) / len(addr)
        maj = Counter(r["gold"]["addressee"] for r in addr).most_common(1)[0]
        indiv = [r for r in addr if r["gold"]["addressee"] != "group"]
        indiv_acc = sum(r["answers"]["addressee"]["choice"] == r["gold"]["addressee"] for r in indiv) / len(indiv) if indiv else None
        pred_indiv = [r for r in addr if r["answers"]["addressee"]["choice"] not in ("group", "unclear")]
        indiv_prec = sum(r["answers"]["addressee"]["choice"] == r["gold"]["addressee"] for r in pred_indiv) / len(pred_indiv) if pred_indiv else None
        out["addressee"] = {"n": len(addr), "accuracy": round(acc, 4), "majority": {"label": maj[0], "accuracy": round(maj[1] / len(addr), 4)},
                            "individual_gold_n": len(indiv), "recall_of_individual_addressee": round(indiv_acc, 4) if indiv_acc is not None else None,
                            "precision_when_naming_individual": round(indiv_prec, 4) if indiv_prec is not None else None,
                            "predicted_individual_n": len(pred_indiv)}
    out["per_meeting_act_accuracy"] = {m: round(sum(r["answers"]["act"]["choice"] == r["gold"]["label"] for r in ok if r["meeting"] == m) /
                                                max(1, sum(r["meeting"] == m for r in ok)), 4) for m in MEETINGS}
    return out


def main():
    jobs = build_jobs()
    print("jobs", len(jobs), Counter(j["gold"]["label"] for j in jobs))
    rows, wall = asyncio.run(run_jobs(jobs, label="E1"))
    summary = summarize(rows, wall)
    metrics = evaluate(rows)
    print(json.dumps({"summary": summary, **{k: v for k, v in metrics.items() if k not in ("typesafe_act", "rule_act")}}, indent=1))
    for k in ("typesafe_act", "rule_act"):
        print(k, {kk: vv for kk, vv in metrics[k].items() if kk != "per_class"})
        print("  per_class f1:", {c: v["f1"] for c, v in metrics[k]["per_class"].items()})
    path = save("exp1_da_tagging.json", {"meetings": MEETINGS, "summary": summary, "metrics": metrics, "rows": rows})
    print("saved", path)


if __name__ == "__main__":
    main()
