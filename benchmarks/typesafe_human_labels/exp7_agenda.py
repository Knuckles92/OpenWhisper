"""E7: live agenda tracking: which agenda item is being discussed in each 30 s window, vs AMI human topic segmentation."""
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ami_nxt import load_dialogue_acts, load_topics, load_words, roles, speaker_tag
from ts_client import run_jobs, save, summarize, prf

MEETINGS = ("ES2008a", "IS1008b", "TS3005a", "ES2008b")
WINDOW = 30.0
SKIP = {"chitchat", "agenda/equipment issues", "other"}


def build_jobs():
    jobs = []
    for mid in MEETINGS:
        words = load_words(mid)
        acts = load_dialogue_acts(mid, words)
        role_map = roles(mid)
        top = [t for t in load_topics(mid, words) if t["depth"] == 0]
        # agenda = distinct top-level labels in order of first appearance, excluding housekeeping labels
        agenda = []
        for t in sorted(top, key=lambda t: t["start"]):
            if t["label"] not in agenda and t["label"] not in SKIP:
                agenda.append(t["label"])
        end = max(a["end"] for a in acts)
        prev_text = ""
        prev_gold = None
        for w in range(int(end // WINDOW) + 1):
            lo, hi = w * WINDOW, (w + 1) * WINDOW
            inside = [a for a in acts if lo <= a["start"] < hi]
            text = "\n".join(f"{speaker_tag(a['speaker'], role_map)}: {a['text']}" for a in inside)
            mid_t = (lo + hi) / 2
            cover = [t for t in top if t["start"] <= mid_t <= (t["end"] or 1e9)]
            gold = min(cover, key=lambda t: (t["end"] or 1e9) - t["start"])["label"] if cover else None
            if sum(a["words"] for a in inside) < 12 or gold is None:
                prev_text, prev_gold = text, gold
                continue
            criteria = {f"item{i+1}": f"{label}" for i, label in enumerate(agenda)}
            criteria["off_agenda"] = "Small talk, equipment or scheduling logistics, or a topic not on the agenda."
            jobs.append({"id": f"{mid}|{w}", "meeting": mid, "window": w, "agenda": agenda,
                         "gold": gold, "gold_item": f"item{agenda.index(gold)+1}" if gold in agenda else "off_agenda",
                         "prev_gold": prev_gold, "boundary": bool(prev_gold and gold != prev_gold),
                         "state": {"agenda": {f"item{i+1}": label for i, label in enumerate(agenda)}, "previous_window": prev_text, "window": text},
                         "questions": {"current_item": {"type": "choice", "criteria": criteria,
                                                        "instructions": "Which entry of `agenda` is the group discussing in `window`, the latest 30 seconds of the meeting transcript? Use `previous_window` for continuity. Choose off_agenda for small talk, logistics, or topics outside the agenda."}}})
            prev_text, prev_gold = text, gold
    return jobs


def evaluate(rows):
    ok = [r for r in rows if "answers" in r]
    pick = lambda r: r["answers"]["current_item"]["choice"]
    out = {"n_windows": len(ok), "accuracy": round(sum(pick(r) == r["gold_item"] for r in ok) / len(ok), 4)}
    on = [r for r in ok if r["gold_item"] != "off_agenda"]
    out["accuracy_on_agenda_windows"] = round(sum(pick(r) == r["gold_item"] for r in on) / len(on), 4)
    out["off_agenda_gold_n"] = len(ok) - len(on)
    out["off_agenda_prf"] = prf([pick(r) == "off_agenda" for r in ok], [r["gold_item"] == "off_agenda" for r in ok])
    for thr in (0.5, 0.7):
        sub = [r for r in ok if r["answers"]["current_item"]["confidence"] >= thr]
        out[f"conf>={thr}"] = {"coverage": round(len(sub) / len(ok), 3), "accuracy": round(sum(pick(r) == r["gold_item"] for r in sub) / len(sub), 4) if sub else None}
    out["per_meeting"] = {}
    for mid in MEETINGS:
        sub = [r for r in ok if r["meeting"] == mid]
        agenda = sub[0]["agenda"] if sub else []
        # sticky (previous-window smoothing): only switch when the new choice repeats twice
        smoothed, cur, pending = [], None, None
        for r in sorted(sub, key=lambda r: r["window"]):
            ch = pick(r)
            if cur is None:
                cur = ch
            elif ch != cur:
                if pending == ch:
                    cur = ch
                pending = ch
            else:
                pending = None
            smoothed.append(cur == r["gold_item"])
        out["per_meeting"][mid] = {"n": len(sub), "agenda_items": len(agenda),
                                   "chance": round(1 / (len(agenda) + 1), 3),
                                   "accuracy": round(sum(pick(r) == r["gold_item"] for r in sub) / max(1, len(sub)), 4),
                                   "accuracy_smoothed_2_window": round(sum(smoothed) / max(1, len(smoothed)), 4),
                                   "gold_distribution": dict(Counter(r["gold"] for r in sub).most_common(6))}
    # boundary detection: predicted change of item vs gold boundary (within same window)
    by_m = {}
    for r in ok:
        by_m.setdefault(r["meeting"], []).append(r)
    pred_b, gold_b = [], []
    for mid, sub in by_m.items():
        sub.sort(key=lambda r: r["window"])
        for i in range(1, len(sub)):
            pred_b.append(pick(sub[i]) != pick(sub[i - 1]))
            gold_b.append(sub[i]["gold_item"] != sub[i - 1]["gold_item"])
    out["boundary_detection_same_window"] = {**prf(pred_b, gold_b), "gold_boundaries": sum(gold_b)}
    # items never reached: compare predicted covered set vs gold covered set per meeting
    out["coverage_sets"] = {}
    for mid, sub in by_m.items():
        gold_set = set(r["gold_item"] for r in sub) - {"off_agenda"}
        pred_set = set(pick(r) for r in sub) - {"off_agenda"}
        out["coverage_sets"][mid] = {"gold_items_discussed": len(gold_set), "predicted": len(pred_set),
                                     "jaccard": round(len(gold_set & pred_set) / max(1, len(gold_set | pred_set)), 3)}
    return out


def main():
    jobs = build_jobs()
    print("jobs", len(jobs), {m: jobs[[j["meeting"] for j in jobs].index(m)]["agenda"] for m in MEETINGS if any(j["meeting"] == m for j in jobs)})
    rows, wall = asyncio.run(run_jobs(jobs, label="E7", progress_every=100))
    save("exp7_agenda.raw.json", {"rows": rows})
    summary = summarize(rows, wall)
    metrics = evaluate(rows)
    print(json.dumps({"summary": summary, "metrics": metrics}, indent=1))
    print("saved", save("exp7_agenda.json", {"meetings": MEETINGS, "window_s": WINDOW, "summary": summary, "metrics": metrics, "rows": rows}))


if __name__ == "__main__":
    main()
