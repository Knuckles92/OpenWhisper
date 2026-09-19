"""E4: per-segment signals on REAL ASR drafts: in-meeting voice commands (false-positive rate), ASR artifact detection vs reference, cloud-sensitivity gate."""
import asyncio
import json
import random
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, "D:/coding/whisper_local")
from benchmarks.meeting_mode.ami import parse_reference_words
from benchmarks.meeting_mode.metrics import normalize_tokens
from ts_client import auroc, prf, run_jobs, save, summarize

ROOT = Path("D:/coding/whisper_local")
SOURCE = ROOT / "benchmarks/meeting_mode/results/auto-auto-draft-only-t5-m20-p50-offline"
ANN = ROOT / "benchmarks/meeting_mode/data/ami/annotations"
MEETINGS = ("IN1009", "IN1005", "IN1007")

COMMANDS = [
    ("mark_decision", "okay note taker mark that as a decision"), ("mark_decision", "assistant, log that as the decision we just made"),
    ("mark_decision", "put that down as a decision please"), ("mark_decision", "hey OpenWhisper that's a decision, record it"),
    ("mark_action", "note taker add an action item for Maya to send the slides"), ("mark_action", "assistant make that an action for me by friday"),
    ("mark_action", "can you add that as a to-do in the notes"), ("mark_action", "OpenWhisper, action item: Omar checks the budget"),
    ("note_this", "note taker write that down"), ("note_this", "assistant please note that the vendor call moved"),
    ("note_this", "put that in the notes"), ("note_this", "hey whisper capture that point about latency"),
    ("recap", "note taker what have we decided so far"), ("recap", "assistant give us a quick recap"),
    ("recap", "can you read back the action items"), ("recap", "OpenWhisper summarize where we are"),
    ("fix_transcript", "note taker that should say Parakeet not parakeet the bird"), ("fix_transcript", "assistant the name is Nia with an i not Mia"),
    ("fix_transcript", "correct the transcript it's SQLite not sequel light"), ("fix_transcript", "hey whisper fix that, we said five hundred not five thousand"),
    ("set_topic", "note taker we're moving on to the budget now"), ("set_topic", "assistant new topic: hiring plan"),
    ("set_topic", "set the topic to release timeline"), ("set_topic", "OpenWhisper mark a new section for the demo review"),
]
SENSITIVE = [
    "the admin password for the staging box is hunter two capital H", "my social security number is five five five one two one two three four",
    "her diagnosis came back positive and she's on medical leave through march", "the api key starts with s k dash live and I'll paste it in the chat",
    "he was written up for harassment last quarter so keep that between us", "card number four one one one one one one one one one one one one one one one expiry oh nine",
    "the merger with Acme is confidential until the board announces it friday", "patient record shows he's HIV positive, don't put that in the shared doc",
    "use my login, username dfiori and the password is my dog's name plus twenty twenty", "her home address is forty two elm street, apartment three, she asked us not to share it",
    "the layoff list has these twelve names on it", "the bank routing number is oh two one oh oh oh oh two one",
    "this is attorney client privileged, do not forward", "his salary is one forty and hers is one ten, that's why she's upset",
    "the encryption passphrase is correct horse battery staple", "the source code for the pricing algorithm is trade secret",
]
NEG_HARD = [  # people talking to each other, not to the app; benign mentions of notes/passwords
    "can you write that down for me in your notebook", "I'll take notes and send the minutes later", "we should decide this today",
    "remind me to email him", "the password field on the login page is too small", "let's move on to the next slide",
    "so to recap what Marco said, the filter bank has forty channels", "what did we decide about the microphones last time",
]


def load(mid):
    d = json.loads((SOURCE / f"{mid}.json").read_text(encoding="utf-8"))
    return sorted(d["draft_segments"], key=lambda s: (s["end_s"], s["id"]))


def reference_match(words, seg):
    toks = normalize_tokens(seg["text"])
    if len(toks) < 2:
        return None
    lo, hi = seg["start_s"] - 1.0, seg["end_s"] + 1.0
    ref = Counter(t for w in words if w.end_s >= lo and w.start_s <= hi for t in normalize_tokens(w.text))
    hit = 0
    for t in toks:
        if ref[t] > 0:
            ref[t] -= 1
            hit += 1
    return hit / len(toks)


def build_jobs(seed=11):
    rng = random.Random(seed)
    jobs = []
    for mid in MEETINGS:
        segs = load(mid)
        words = parse_reference_words(ANN, mid)
        for i, s in enumerate(segs):
            jobs.append(make_job(f"{mid}|{s['id']}", mid, s["text"], [p["text"] for p in segs[max(0, i - 3): i]],
                                 {"kind": "real", "command": "none", "sensitive": 0, "ref_match": reference_match(words, s),
                                  "start_s": s["start_s"], "words": len(normalize_tokens(s["text"]))}))
        # synthetic insertions with real neighbours as context
        for kind, text in [("command", c) for c in COMMANDS] + [("sensitive", t) for t in SENSITIVE] + [("hard_negative", t) for t in NEG_HARD]:
            if rng.random() > 1 / len(MEETINGS) and mid != MEETINGS[-1]:
                continue
            i = rng.randrange(3, len(segs))
            label = text[0] if kind == "command" else "none"
            body = text[1] if kind == "command" else text
            jobs.append(make_job(f"{mid}|syn|{kind}|{len(jobs)}", mid, body, [p["text"] for p in segs[i - 3: i]],
                                 {"kind": kind, "command": label, "sensitive": int(kind == "sensitive"), "ref_match": None, "start_s": None,
                                  "words": len(normalize_tokens(body))}))
    return jobs


def make_job(jid, mid, text, previous, gold):
    return {"id": jid, "meeting": mid, "gold": gold, "text": text,
            "state": {"previous_segments": previous, "segment": text,
                      "assistant_names": ["note taker", "assistant", "OpenWhisper", "whisper"]},
            "questions": {
                "command": {"type": "choice", "criteria": {
                    "none": "Ordinary meeting speech between participants, including talking about notes, decisions or topics without instructing the assistant.",
                    "mark_decision": "Instructs the note-taking assistant to record the preceding point as a decision.",
                    "mark_action": "Instructs the assistant to record an action item or to-do, possibly with an owner or deadline.",
                    "note_this": "Instructs the assistant to write down or capture the preceding point in the notes.",
                    "recap": "Asks the assistant to summarize, read back, or recap decisions, actions or progress.",
                    "fix_transcript": "Tells the assistant that a word, name or number in the transcript is wrong and gives the correction.",
                    "set_topic": "Tells the assistant that the meeting is moving to a new topic or section."},
                    "instructions": "Is `segment` an instruction directed at the automated note-taking assistant, which participants address using one of `assistant_names` or by referring explicitly to the notes it keeps? Speech directed at another person is none, even if it mentions notes, decisions or topics. `previous_segments` is context only."},
                "artifact": {"type": "noul", "instructions": "Is `segment` likely a speech-recognition error rather than something a participant actually said? Signs: text in a different language from `previous_segments`, a stock phrase such as thanks for watching or please subscribe, meaningless repetition, or content unrelated to the ongoing conversation. Normal disfluent, fragmentary or accented speech is not an artifact."},
                "sensitive": {"type": "noul", "instructions": "Does `segment` contain information that should not be sent to a third-party cloud service without review: passwords, keys or credentials; government, bank or card numbers; health, disciplinary, salary or home-address details about an identifiable person; or an explicit statement that the content is confidential or privileged? Ordinary technical or business discussion is not sensitive."},
            }}


def evaluate(rows):
    ok = [r for r in rows if "answers" in r]
    real = [r for r in ok if r["gold"]["kind"] == "real"]
    hard = [r for r in ok if r["gold"]["kind"] == "hard_negative"]
    cmd = [r for r in ok if r["gold"]["kind"] == "command"]
    sens = [r for r in ok if r["gold"]["kind"] == "sensitive"]
    out = {"n_real": len(real), "n_synthetic": {"command": len(cmd), "sensitive": len(sens), "hard_negative": len(hard)}}
    c = lambda r: r["answers"]["command"]
    for thr in (0.0, 0.5, 0.8):
        fp_real = [r for r in real if c(r)["choice"] != "none" and c(r)["confidence"] >= thr]
        fp_hard = [r for r in hard if c(r)["choice"] != "none" and c(r)["confidence"] >= thr]
        tp = [r for r in cmd if c(r)["choice"] == r["gold"]["command"] and c(r)["confidence"] >= thr]
        detected = [r for r in cmd if c(r)["choice"] != "none" and c(r)["confidence"] >= thr]
        out[f"command_conf>={thr}"] = {"false_positive_rate_real": round(len(fp_real) / len(real), 4), "false_positives_real": len(fp_real),
                                       "false_positives_hard_negatives": len(fp_hard), "recall_any_command": round(len(detected) / max(1, len(cmd)), 3),
                                       "recall_exact_label": round(len(tp) / max(1, len(cmd)), 3),
                                       "fp_examples": [(r["text"][:90], c(r)["choice"], c(r)["confidence"]) for r in fp_real[:6]]}
    spairs = [(r["answers"]["sensitive"]["noul"], r["gold"]["sensitive"]) for r in real + hard + sens]
    out["sensitive"] = {"auroc": auroc(spairs)}
    for thr in (0.5, 0.8):
        flagged_real = [r for r in real if r["answers"]["sensitive"]["noul"] >= thr]
        out["sensitive"][f">={thr}"] = {"recall_synthetic": round(sum(r["answers"]["sensitive"]["noul"] >= thr for r in sens) / max(1, len(sens)), 3),
                                        "false_positive_rate_real": round(len(flagged_real) / len(real), 4),
                                        "fp_examples": [(r["text"][:90], r["answers"]["sensitive"]["noul"]) for r in flagged_real[:5]]}
    scored = [r for r in real if r["gold"]["ref_match"] is not None]
    gold_art = lambda r: int(r["gold"]["ref_match"] <= 0.2)
    apairs = [(r["answers"]["artifact"]["noul"], gold_art(r)) for r in scored]
    out["artifact"] = {"n_scored": len(scored), "gold_artifacts(ref_match<=0.2)": sum(l for _, l in apairs), "auroc": auroc(apairs),
                       "spearman_noul_vs_1-ref_match": None}
    for thr in (0.5, 0.8):
        pred = [p >= thr for p, _ in apairs]
        flagged = [r for r in scored if r["answers"]["artifact"]["noul"] >= thr]
        out["artifact"][f">={thr}"] = {**prf(pred, [bool(l) for _, l in apairs]), "flagged": len(flagged),
                                       "mean_ref_match_flagged": round(sum(r["gold"]["ref_match"] for r in flagged) / len(flagged), 3) if flagged else None,
                                       "mean_ref_match_unflagged": round(sum(r["gold"]["ref_match"] for r in scored if r["answers"]["artifact"]["noul"] < thr) / max(1, len(scored) - len(flagged)), 3)}
    top = sorted(scored, key=lambda r: -r["answers"]["artifact"]["noul"])[:12]
    out["artifact"]["top_flagged"] = [(r["text"][:80], r["answers"]["artifact"]["noul"], round(r["gold"]["ref_match"], 2)) for r in top]
    missed = sorted([r for r in scored if gold_art(r)], key=lambda r: r["answers"]["artifact"]["noul"])[:8]
    out["artifact"]["lowest_scored_gold_artifacts"] = [(r["text"][:80], r["answers"]["artifact"]["noul"], round(r["gold"]["ref_match"], 2), r["gold"]["words"]) for r in missed]
    return out


def main():
    jobs = build_jobs()
    print("jobs", len(jobs), Counter(j["gold"]["kind"] for j in jobs))
    rows, wall = asyncio.run(run_jobs(jobs, label="E4"))
    summary = summarize(rows, wall)
    metrics = evaluate(rows)
    print(json.dumps({"summary": summary, "metrics": metrics}, indent=1, default=str))
    print("saved", save("exp4_draft_signals.json", {"meetings": MEETINGS, "summary": summary, "metrics": metrics, "rows": rows}))


if __name__ == "__main__":
    main()
