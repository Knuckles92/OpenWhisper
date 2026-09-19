"""E3: per-window checkpoint gate and topic-boundary detection vs human extractive summary and topic segmentation."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "D:/coding/whisper_local")
from ami_nxt import load_abstractive, load_dialogue_acts, load_extractive, load_summlinks, load_topics, load_words, roles, speaker_tag
from ts_client import auroc, prf, run_jobs, save, summarize
from meeting.agent.scheduler import _SHIFT_JACCARD_THRESHOLD, _SHIFT_MIN_WORDS, _content_words

MEETINGS = ("ES2008a", "IS1008b", "TS3005a", "ES2008b")
WINDOW = 60.0


def jaccard_shift(prev_text, cur_text):
    a, b = _content_words(prev_text), _content_words(cur_text)
    if len(a) < _SHIFT_MIN_WORDS or len(b) < _SHIFT_MIN_WORDS:
        return None
    return len(a & b) / len(a | b)


def topic_at(topics, t):
    cover = [x for x in topics if x["depth"] == 0 and x["start"] <= t <= (x["end"] or 1e9)]
    return min(cover, key=lambda x: (x["end"] or 1e9) - x["start"])["label"] if cover else None


def build_jobs():
    jobs = []
    for mid in MEETINGS:
        words = load_words(mid)
        acts = load_dialogue_acts(mid, words)
        ext = load_extractive(mid, acts)
        links = load_summlinks(mid)
        sections = {s["id"]: s["section"] for s in load_abstractive(mid)}
        commit_ids = set()
        for sid, dids in links.items():
            if sections.get(sid) in ("actions", "decisions"):
                commit_ids |= dids
        topics = load_topics(mid, words)
        role_map = roles(mid)
        end = max(a["end"] for a in acts)
        n = int(end // WINDOW) + 1
        prev_text = None
        prev_topic = None
        for w in range(n):
            lo, hi = w * WINDOW, (w + 1) * WINDOW
            inside = [a for a in acts if lo <= a["start"] < hi]
            text = "\n".join(f"{speaker_tag(a['speaker'], role_map)}: {a['text']}" for a in inside)
            nwords = sum(a["words"] for a in inside)
            topic = topic_at(topics, (lo + hi) / 2)
            if nwords < 15 or prev_text is None:
                prev_text, prev_topic = text, topic
                continue
            ext_in = [a for a in inside if a["id"] in ext]
            gold = {"n_segments": len(inside), "words": nwords, "ext_acts": len(ext_in), "ext_words": sum(a["words"] for a in ext_in),
                    "has_commitment_or_decision": any(a["id"] in commit_ids for a in inside),
                    "topic": topic, "prev_topic": prev_topic, "topic_changed": bool(topic and prev_topic and topic != prev_topic)}
            jobs.append({
                "id": f"{mid}|{w}", "meeting": mid, "window_start": lo, "gold": gold, "jaccard": jaccard_shift(prev_text, text),
                "state": {"previous_window": prev_text, "window": text},
                "questions": {
                    "note_worthiness": {"type": "score", "instructions": "How much new content in `window` (the latest minute of a meeting transcript) belongs in written meeting notes? Consider decisions, commitments, concrete proposals, requirements, findings, problems and open questions. `previous_window` is context only.",
                                        "criteria": ["Nothing to note: small talk, backchannels, logistics, or repetition of what is already in previous_window.",
                                                     "Mostly discussion with at most one minor point worth a brief note.",
                                                     "At least one clear note-worthy item, such as a concrete proposal, requirement, finding, or open question.",
                                                     "Contains a decision, an accepted action item, or several substantive points that notes must include."]},
                    "commitment_or_decision": {"type": "noul", "instructions": "Does `window` contain a decision the group adopts or a concrete task that someone accepts or is assigned? Suggestions, hypotheticals and information alone do not count."},
                    "topic_changed": {"type": "noul", "instructions": "Does the discussion in `window` move on to a different agenda topic or activity than the one in `previous_window`? Elaborating the same topic, or a brief aside that returns to it, is not a change."},
                }})
            prev_text, prev_topic = text, topic
    return jobs


def evaluate(rows):
    ok = [r for r in rows if "answers" in r]
    out = {"n_windows": len(ok)}
    score = lambda r: r["answers"]["note_worthiness"]["score"]
    any_ext = lambda r: int(r["gold"]["ext_acts"] > 0)
    out["gate"] = {
        "windows_with_extractive_content": sum(any_ext(r) for r in ok),
        "auroc_score_vs_any_extractive": auroc([(score(r), any_ext(r)) for r in ok]),
        "auroc_word_count_vs_any_extractive": auroc([(r["gold"]["words"], any_ext(r)) for r in ok]),
        "auroc_segment_count_vs_any_extractive": auroc([(r["gold"]["n_segments"], any_ext(r)) for r in ok]),
        "spearman_score_vs_ext_words": spearman([score(r) for r in ok], [r["gold"]["ext_words"] for r in ok]),
        "spearman_words_vs_ext_words": spearman([r["gold"]["words"] for r in ok], [r["gold"]["ext_words"] for r in ok]),
    }
    commit = lambda r: int(r["gold"]["has_commitment_or_decision"])
    out["commitment_windows"] = sum(commit(r) for r in ok)
    for thr in (0.5, 1.0, 1.5, 2.0):
        fire = [score(r) >= thr for r in ok]
        out["gate"][f"fire_if_score>={thr}"] = {
            "fires_fraction": round(sum(fire) / len(ok), 3),
            "recall_windows_with_extractive": round(sum(f and any_ext(r) for f, r in zip(fire, ok)) / max(1, sum(any_ext(r) for r in ok)), 3),
            "recall_windows_with_commitment_or_decision": round(sum(f and commit(r) for f, r in zip(fire, ok)) / max(1, sum(commit(r) for r in ok)), 3),
            "extractive_words_covered": round(sum(r["gold"]["ext_words"] for f, r in zip(fire, ok) if f) / max(1, sum(r["gold"]["ext_words"] for r in ok)), 3),
        }
    # what the current scheduler does: pending >= 8 segments -> urgent. Use segments as its fire rule.
    for thr in (3, 8):
        fire = [r["gold"]["n_segments"] >= thr for r in ok]
        out["gate"][f"current_rule_segments>={thr}"] = {
            "fires_fraction": round(sum(fire) / len(ok), 3),
            "recall_windows_with_extractive": round(sum(f and any_ext(r) for f, r in zip(fire, ok)) / max(1, sum(any_ext(r) for r in ok)), 3),
            "recall_windows_with_commitment_or_decision": round(sum(f and commit(r) for f, r in zip(fire, ok)) / max(1, sum(commit(r) for r in ok)), 3),
        }
    cpairs = [(r["answers"]["commitment_or_decision"]["noul"], commit(r)) for r in ok]
    out["commitment_or_decision"] = {"auroc": auroc(cpairs), "f1@0.5": prf([p >= 0.5 for p, _ in cpairs], [bool(l) for _, l in cpairs]),
                                     "f1@0.8": prf([p >= 0.8 for p, _ in cpairs], [bool(l) for _, l in cpairs])}
    tw = [r for r in ok if r["gold"]["topic"] and r["gold"]["prev_topic"]]
    tpairs = [(r["answers"]["topic_changed"]["noul"], int(r["gold"]["topic_changed"])) for r in tw]
    jw = [r for r in tw if r["jaccard"] is not None]
    out["topic_change"] = {
        "n": len(tw), "positives": sum(l for _, l in tpairs), "auroc_typesafe": auroc(tpairs),
        "f1@0.5": prf([p >= 0.5 for p, _ in tpairs], [bool(l) for _, l in tpairs]),
        "f1@0.8": prf([p >= 0.8 for p, _ in tpairs], [bool(l) for _, l in tpairs]),
        "jaccard_windows_scored": len(jw),
        "auroc_jaccard_heuristic": auroc([(1 - r["jaccard"], int(r["gold"]["topic_changed"])) for r in jw]),
        "jaccard<0.15_rule": prf([r["jaccard"] < _SHIFT_JACCARD_THRESHOLD for r in jw], [r["gold"]["topic_changed"] for r in jw]),
        "typesafe_on_same_windows_f1@0.5": prf([r["answers"]["topic_changed"]["noul"] >= 0.5 for r in jw], [r["gold"]["topic_changed"] for r in jw]),
    }
    return out


def spearman(xs, ys):
    def rank(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        ranks = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                ranks[order[k]] = (i + j) / 2 + 1
            i = j + 1
        return ranks
    rx, ry = rank(xs), rank(ys)
    n = len(xs)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx) ** 0.5
    vy = sum((b - my) ** 2 for b in ry) ** 0.5
    return round(cov / (vx * vy), 4) if vx and vy else None


def main():
    jobs = build_jobs()
    print("windows", len(jobs), "topic changes", sum(j["gold"]["topic_changed"] for j in jobs),
          "commitment windows", sum(j["gold"]["has_commitment_or_decision"] for j in jobs))
    rows, wall = asyncio.run(run_jobs(jobs, label="E3", progress_every=50))
    summary = summarize(rows, wall)
    metrics = evaluate(rows)
    print(json.dumps({"summary": summary, "metrics": metrics}, indent=1))
    print("saved", save("exp3_content_gate.json", {"meetings": MEETINGS, "window_s": WINDOW, "summary": summary, "metrics": metrics, "rows": rows}))


if __name__ == "__main__":
    main()
