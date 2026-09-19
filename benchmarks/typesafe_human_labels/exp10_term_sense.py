"""E10: per-occurrence term-correction sense check (should THIS occurrence be rewritten?), synthetic, authored before the call."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ts_client import auroc, prf, run_jobs, save, summarize

# (heard, meant, segment, previous, should_replace)
CASES = [
    ("entropic", "Anthropic", "so entropic released the new model last week and pricing changed", ["we were comparing the vendors"], 1),
    ("entropic", "Anthropic", "the entropic API rate limits are what's blocking the demo", ["Maya is on the integration"], 1),
    ("entropic", "Anthropic", "entropic forces dominate at that temperature so the polymer collapses", ["we're on the physics section"], 0),
    ("entropic", "Anthropic", "the system becomes more entropic as it heats up", ["thermodynamics recap"], 0),
    ("sequel light", "SQLite", "we decided to use sequel light for the local cache", ["storage options"], 1),
    ("sequel light", "SQLite", "sequel light doesn't handle concurrent writers well", ["Omar raised a concern"], 1),
    ("sequel", "SQL", "the sequel query times out on the big table", ["database performance"], 1),
    ("sequel", "SQL", "honestly the sequel was worse than the first movie", ["weekend chat"], 0),
    ("sequel", "SQL", "they're planning a sequel to the campaign next spring", ["marketing update"], 0),
    ("parakeet", "Parakeet", "parakeet v3 beats whisper on the offline pass", ["ASR benchmark"], 1),
    ("parakeet", "Parakeet", "my daughter wants a parakeet for her birthday", ["small talk before we start"], 0),
    ("john", "Jon", "john said he can review the PR tomorrow", ["Jon is the reviewer"], 1),
    ("john", "Jon", "we're meeting the client John Peterson at two", ["external meeting"], 0),
    ("cube control", "kubectl", "run cube control get pods to see if it's up", ["debugging the deploy"], 1),
    ("cube control", "kubectl", "the cube control room at the museum was closed", ["field trip planning"], 0),
    ("nemo", "NeMo", "the nemo toolkit needs cuda twelve", ["runtime install"], 1),
    ("nemo", "NeMo", "finding nemo is on at the kids' movie night", ["team social"], 0),
    ("mia", "Nia", "mia owns the docs task", ["Nia volunteered earlier"], 1),
    ("mia", "Nia", "our customer contact at Acme is Mia Torres", ["account review"], 0),
    ("whisper", "OpenWhisper", "whisper crashed on startup after the update", ["bug triage for our app"], 1),
    ("whisper", "OpenWhisper", "she had to whisper because the baby was asleep", ["story from the weekend"], 0),
    ("git hub", "GitHub", "push it to git hub before the standup", ["release prep"], 1),
    ("python", "Python", "the python script parses the log", ["tooling"], 1),
    ("python", "Python", "there was a python loose in the office park", ["news"], 0),
    ("ruby", "Ruby", "the ruby service still runs the billing job", ["legacy systems"], 1),
    ("ruby", "Ruby", "he gave her a ruby ring", ["gossip"], 0),
    ("windows", "Windows", "the windows build fails on arm", ["CI status"], 1),
    ("windows", "Windows", "open the windows, it's stuffy in here", ["room logistics"], 0),
    ("azure", "Azure", "we host the demo on azure", ["infra"], 1),
    ("azure", "Azure", "the sky was azure the whole trip", ["vacation talk"], 0),
]


def build_jobs():
    return [{"id": f"case{i}", "gold": g, "text": seg,
             "state": {"correction": {"heard_as": heard, "should_be": meant, "note": f"When participants say the name {meant}, the transcript writes {heard}."},
                       "previous_segments": prev, "segment": seg},
             "questions": {"replace_here": {"type": "noul", "instructions": "In `segment`, does the phrase `correction.heard_as` refer to the same thing as `correction.should_be`, so that this occurrence should be rewritten? If the phrase is used in its ordinary meaning or names something else, it should stay as heard."}}}
            for i, (heard, meant, seg, prev, g) in enumerate(CASES)]


def main():
    jobs = build_jobs()
    rows, wall = asyncio.run(run_jobs(jobs, label="E10", progress_every=0))
    ok = [r for r in rows if "answers" in r]
    pairs = [(r["answers"]["replace_here"]["noul"], r["gold"]) for r in ok]
    metrics = {"n": len(ok), "auroc": auroc(pairs), "prf@0.5": prf([p >= 0.5 for p, _ in pairs], [bool(g) for _, g in pairs]),
               "accuracy@0.5": round(sum((p >= 0.5) == bool(g) for p, g in pairs) / len(pairs), 4),
               "blind_replace_baseline_accuracy": round(sum(g for _, g in pairs) / len(pairs), 4),
               "errors": [(r["text"], r["answers"]["replace_here"]["noul"], r["gold"]) for r in ok if (r["answers"]["replace_here"]["noul"] >= 0.5) != bool(r["gold"])],
               "min_margin_cases": sorted([(round(r["answers"]["replace_here"]["noul"], 2), r["gold"], r["text"][:60]) for r in ok], key=lambda x: abs(x[0] - 0.5))[:5]}
    print(json.dumps({"summary": summarize(rows, wall), "metrics": metrics}, indent=1))
    print("saved", save("exp10_term_sense.json", {"summary": summarize(rows, wall), "metrics": metrics, "rows": rows}))


if __name__ == "__main__":
    main()
