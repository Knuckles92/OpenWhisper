"""E6: per-minute choice between live draft and offline re-decode, scored as a hybrid transcript with the benchmark's tcWER."""
import asyncio
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "D:/coding/whisper_local")
from benchmarks.meeting_mode.ami import parse_reference_words
from benchmarks.meeting_mode.metrics import aggregate_scores, normalize_tokens, score_timed_transcript
from ts_client import run_jobs, save, summarize

ROOT = Path("D:/coding/whisper_local")
ANN = ROOT / "benchmarks/meeting_mode/data/ami/annotations"
RUNS = {"whisper": "auto-auto-draft-only-t5-m20-p50-offline", "parakeet": "parakeet-v3-en-draft-only-t5-m20-p50-offline"}
WINDOW = 60.0


def windows_of(segs, n):
    out = [[] for _ in range(n)]
    for s in segs:
        w = int(float(s["start_s"]) // WINDOW)
        if 0 <= w < n:
            out[w].append(s)
    return out


def build_jobs(seed=3):
    rng = random.Random(seed)
    jobs, meta = [], {}
    for family, run in RUNS.items():
        for path in sorted((ROOT / "benchmarks/meeting_mode/results" / run).glob("IN*.json")):
            d = json.loads(path.read_text(encoding="utf-8"))
            mid = d["meeting_id"]
            words = parse_reference_words(ANN, mid)
            n = int(max(w.end_s for w in words) // WINDOW) + 1
            dw, ow = windows_of(d["draft_segments"], n), windows_of(d["offline_segments"], n)
            # per-window truth for the oracle
            ds = score_timed_transcript(words, d["draft_segments"], WINDOW)["windows"]
            os_ = score_timed_transcript(words, d["offline_segments"], WINDOW)["windows"]
            meta[(family, mid)] = {"words": words, "draft": d["draft_segments"], "offline": d["offline_segments"], "n": n,
                                   "draft_windows": dw, "offline_windows": ow, "draft_err": [x["substitutions"] + x["deletions"] + x["insertions"] for x in ds],
                                   "offline_err": [x["substitutions"] + x["deletions"] + x["insertions"] for x in os_]}
            for w in range(n):
                dt = " ".join(s["text"] for s in dw[w]).strip()
                ot = " ".join(s["text"] for s in ow[w]).strip()
                if not dt and not ot:
                    continue
                draft_first = rng.random() < 0.5
                a, b = (dt, ot) if draft_first else (ot, dt)
                jobs.append({"id": f"{family}|{mid}|{w}", "family": family, "meeting": mid, "window": w, "draft_is_a": draft_first,
                             "draft_words": len(normalize_tokens(dt)), "offline_words": len(normalize_tokens(ot)),
                             "state": {"transcript_a": a or "(no speech recognized)", "transcript_b": b or "(no speech recognized)"},
                             "questions": {"better": {"type": "choice", "criteria": {
                                 "a": "transcript_a is more likely the accurate transcription of this minute.",
                                 "b": "transcript_b is more likely the accurate transcription of this minute.",
                                 "similar": "Both are essentially equivalent or differ only trivially."},
                                 "instructions": "`transcript_a` and `transcript_b` are two automatic transcripts of the same minute of a spoken research meeting with several speakers. Which is more likely to be the accurate one? Signs of a worse transcript: garbled or implausible word sequences, text in the wrong language, stock phrases such as thanks for watching, repeated fragments, or long stretches of speech obviously missing while the other transcript has them. Both may contain some errors."}}})
    return jobs, meta


def hybrid(meta, picks):
    """picks: window -> 'draft'|'offline'. Returns segment list."""
    segs = []
    for w in range(meta["n"]):
        segs.extend(meta["draft_windows" if picks.get(w, "draft") == "draft" else "offline_windows"][w])
    return segs


def evaluate(rows, meta):
    ok = [r for r in rows if "answers" in r]
    out = {}
    for family in RUNS:
        fam_rows = [r for r in ok if r["family"] == family]
        scores = {k: [] for k in ("draft", "offline", "typesafe", "longer", "oracle", "typesafe_len_tiebreak")}
        choice_stats = {"a": 0, "b": 0, "similar": 0, "picked_draft": 0, "picked_offline": 0, "agree_with_oracle": 0, "decided": 0, "conf>=0.6_agree": 0, "conf>=0.6_n": 0}
        for (fam, mid), m in meta.items():
            if fam != family:
                continue
            rws = {r["window"]: r for r in fam_rows if r["meeting"] == mid}
            ts_pick, long_pick, oracle_pick, tie_pick = {}, {}, {}, {}
            for w in range(m["n"]):
                de, oe = m["draft_err"][w], m["offline_err"][w]
                oracle_pick[w] = "draft" if de <= oe else "offline"
                r = rws.get(w)
                if not r:
                    continue
                long_pick[w] = "draft" if r["draft_words"] >= r["offline_words"] else "offline"
                ch = r["answers"]["better"]["choice"]
                choice_stats[ch] += 1
                if ch == "similar":
                    ts_pick[w] = "draft"  # keep the product default
                    tie_pick[w] = long_pick[w]
                else:
                    picked = "draft" if (ch == "a") == r["draft_is_a"] else "offline"
                    ts_pick[w] = tie_pick[w] = picked
                    choice_stats["picked_" + picked] += 1
                    choice_stats["decided"] += 1
                    if de != oe:
                        choice_stats["agree_with_oracle"] += int(picked == oracle_pick[w])
                        if r["answers"]["better"]["confidence"] >= 0.6:
                            choice_stats["conf>=0.6_n"] += 1
                            choice_stats["conf>=0.6_agree"] += int(picked == oracle_pick[w])
            words = m["words"]
            scores["draft"].append(score_timed_transcript(words, m["draft"]))
            scores["offline"].append(score_timed_transcript(words, m["offline"]))
            scores["typesafe"].append(score_timed_transcript(words, hybrid(m, ts_pick)))
            scores["typesafe_len_tiebreak"].append(score_timed_transcript(words, hybrid(m, tie_pick)))
            scores["longer"].append(score_timed_transcript(words, hybrid(m, long_pick)))
            scores["oracle"].append(score_timed_transcript(words, hybrid(m, oracle_pick)))
        out[family] = {"n_windows": len(fam_rows), "micro_wer": {k: round(aggregate_scores(v)["wer"], 4) for k, v in scores.items()},
                       "per_meeting_wer": {k: [round(s["wer"], 4) for s in v] for k, v in scores.items() if k in ("draft", "offline", "typesafe", "oracle")},
                       "choices": choice_stats,
                       "oracle_agreement_rate_when_decided": round(choice_stats["agree_with_oracle"] / max(1, choice_stats["decided"]), 3),
                       "oracle_agreement_conf>=0.6": round(choice_stats["conf>=0.6_agree"] / max(1, choice_stats["conf>=0.6_n"]), 3),
                       "position_bias_a_rate": round(choice_stats["a"] / max(1, choice_stats["a"] + choice_stats["b"]), 3)}
    return out


def main():
    jobs, meta = build_jobs()
    print("jobs", len(jobs))
    rows, wall = asyncio.run(run_jobs(jobs, label="E6"))
    summary = summarize(rows, wall)
    metrics = evaluate(rows, meta)
    print(json.dumps({"summary": summary, "metrics": metrics}, indent=1))
    print("saved", save("exp6_hybrid_select.json", {"runs": RUNS, "window_s": WINDOW, "summary": summary, "metrics": metrics, "rows": rows}))


if __name__ == "__main__":
    main()
