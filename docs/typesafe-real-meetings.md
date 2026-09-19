# TypeSafe on real meetings: findings and proposed changes

Run: September 18, 2026. **The strongest result is a fast provisional LLM draft combined with a narrowly scoped commitment ledger. Generic TypeSafe event flags and a blanket verify-and-repair loop did not improve the observed meeting records.** This revises the broader optimism from the [synthetic API benchmark](typesafe-api-benchmark.md).

**Direct output review added:** I subsequently read all 12 records against the complete available human transcripts. [The detailed comparison](typesafe-direct-output-review.md) finds small citation/qualification improvements, but no meaningful overall improvement, and identifies omissions and speaker-attribution errors missed by the item-level audit.

**Live-state follow-up:** [A chronological state-update probe](typesafe-live-state.md) tested the newer idea of continuously maintaining tasks and concerns. Retaining observations improved the TypeSafe prototype, but ambiguous issue resolution still favored small LLM calls; a combined live-product improvement remains unproven.

All experiments used APIs. Production code and settings were not changed. The [compact machine-readable results](benchmarks/typesafe-real-meetings-2026-09-18.json) contain exact measurements and input hashes.

## What was actually tested

Three actual, publicly released AMI research meetings supplied **107.8 minutes and 884 cached ASR segments**. These are real conversations, not authored scenarios. They are not the user's private meetings. The local human annotations identify release 1.7 and are licensed CC BY 4.0. Credit: [AMI Meeting Corpus](https://groups.inf.ed.ac.uk/ami/corpus/); the local `annotations/LICENCE.txt` and `00README_MANUAL.txt` are hashed in the results.

| Meeting | Duration | ASR segments | Existing ASR word error rate |
|---|---:|---:|---:|
| IN1009 | 20.9 min | 177 | 25.3% |
| IN1005 | 46.6 min | 395 | 22.0% |
| IN1007 | 40.2 min | 312 | 32.8% |

Writers and TypeSafe saw the same cached ASR, with its errors and limited speaker identity. Human word transcripts were reserved for evaluation. Audio transcription was not rerun.

The production comparison invoked the actual configured **Pi meeting agent with OpenRouter `deepseek/deepseek-v4.1-flash`**, using its production consolidation prompt and an in-memory host initialized identically for each arm. It compared the normal agent with the same agent given fallible TypeSafe advice. This tests the meeting-record path, not just cleanup. It does **not** measure microphone-to-screen latency, the live scheduler, or consolidation after a full session of accumulated live notes.

Separate controls used a concise JSON writer for topic, summary, key points, decisions, actions and risks. The configured cleanup API model, **`google/gemini-3.8-flash` with low reasoning**, was reused as a candidate meeting writer. This has fewer duties than the complete production agent. Earlier synthetic work covered the available DeepSeek, Gemini and GPT-4o-mini APIs and Direct, Pi and OpenCode engines; this real-meeting follow-up concentrated on the configured production path.

## Speed and the LLM combination

| Meeting | Current Pi | Pi + TypeSafe advice | Concise Gemini draft | Gemini + TypeSafe checks and repair |
|---|---:|---:|---:|---:|
| IN1009 | 223.2 s | 164.0 s | 12.2 s | 22.6 s |
| IN1005 | cancelled at 240 s | cancelled at 240 s | 11.9 s | 22.4 s |
| IN1007 | 208.7 s | cancelled at 240 s | 14.2 s | 26.5 s |

Cancellations leave partial records; they are not completed results. Only one run was made per pair, some runs overlapped, and provider load/cache state were uncontrolled. These are measured observations, not a production latency guarantee or a feature-equivalent speedup.

A concise writer with the **same DeepSeek model** still took 203.5 seconds for IN1009. Its assisted draft took 222.1 seconds before a later failure; an IN1005 assisted draft took 510.6 seconds. The remaining DeepSeek ablation was stopped and marked incomplete. This points to model/provider generation latency as an important factor, not just agent tool overhead. The HTTP client's 90-second timeout was not an elapsed-time deadline; responses can remain active longer. Future benchmark runs need a process-level deadline as well as request timeouts.

The broad TypeSafe scan made **884 calls with four simultaneous requests**, asking four related questions per segment. Median request latency was **158 ms**, p95 **270 ms**, with **zero API errors**. Each request saw one focus segment and at most 20 previous segments from the last 90 seconds, never future context. Replaying the ready backlog took 10.3, 26.3 and 18.9 seconds respectively; that is offline throughput, not real-time detection delay. The 1,712,726 input tokens cost approximately **$0.0719**, using the [documented model price](https://docs.typesafe.ai/models).

Your suggestion to make one call per thing is well supported: make the thing one coherent task or claim, give it enough evidence, and process independent things concurrently. There is no need to wait for eight live events to accumulate. Independent questions about the same state can share a request. Four concurrent calls worked here; unbounded parallelism was not tested.

## Did the records improve?

A blinded, randomized, one-item-per-request Gemini audit compared generated claims with human-reference windows around their citations. It saw neither the producer nor the TypeSafe scores.

| Arm | Items audited | Supported | Overstated | Contradicted | Unclear |
|---|---:|---:|---:|---:|---:|
| Current Pi | 53 | 49 | 3 | 0 | 1 |
| Pi + TypeSafe advice | 42 | 33 | 9 | 0 | 0 |
| Concise Gemini | 34 | 33 | 0 | 1 | 0 |
| Gemini + checks and repair | 30 | 28 | 2 | 0 | 0 |

These are **model judgments about produced items, not human accuracy scores or completeness scores**. Counts include partial Pi records. A model can produce fewer safe items while missing important information. Gemini also judged its own writer family, so this table cannot establish that Gemini is the better meeting model.

Manual spot checks show useful failure modes: suggestions became agreed decisions; a conditional paper-link offer became an assigned task; "wouldn't trust" MATLAB for real-time operation became "cannot"; and a technical explanation gained a rationale not actually established in the cited discussion. Some flags concern missing citation coverage rather than a globally false claim. One purported contradiction about QuickNet expertise rests on awkward reference wording and remains ambiguous without listening to audio. The frozen judge labels are retained, not silently corrected.

The whole-record completeness audit was unsuccessful. A multi-record attempt returned empty rating objects; an isolated retry failed output validation for all 12 records. **No coverage score is reported.** The experiments therefore support trying faster provisional drafts, but do not justify replacing the current full record on quality grounds.

The TypeSafe verifier also needs a different design. With its experimental 0.8 threshold, **all 30 assisted Gemini draft items were sent to repair, and all 30 final items still failed the acceptance gate**. Some were broadly supported but uncertain; others contained several factual clauses with incomplete citation context. The resulting repair loop almost doubled runtime without a demonstrated quality gain. A confidence score is not an externally calibrated probability that the whole claim is correct. Do not deploy this blanket gate or automatically discard everything below it.

## What the streaming signals taught us

The four questions covered commitment changes, broken assumptions, newly actionable discussion and decisions. At the frozen 0.8 threshold, only **five segments** were selected across all three meetings. No actionable or decision probability crossed the threshold.

For evaluation, 60 uniformly sampled segments were combined with all positives and selected difficult/deferred cases, giving 101 unique segments. The human-reference model judge labeled all 60 uniform samples as non-events. The enriched set contained six accepted commitments, one proposal, six actionable moments and two decisions. This sparse sample cannot support a useful population recall estimate; an aggregate accuracy dominated by non-events would mislead.

Two concrete high-confidence failures matter:

- In IN1005, ASR changed **"off hand, PageRank" into "I'll find PageRank"**. TypeSafe confidently classified a technical explanation as a new promise. Fast judgments cannot restore missing acoustic evidence.
- A qualification of a technical metric was flagged as a broken assumption without a clearly established prior plan being invalidated. A general conflict detector does not establish the relationship needed for a useful alert.

The judge itself classified a conditional "if you want" paper-link offer as accepted. Manual text review and the later focused probe retain it as proposed. This disagreement is recorded explicitly. No reliable new-assumption alert or changed/withdrawn commitment was validated in this small real-meeting set.

## The promising joint use: remember the task, then judge the change

A follow-up supplied one candidate task, its previous status, the evidence behind that status, recent context and the new speech. Seven development episodes gave:

| Episode | Expected / returned | Confidence |
|---|---|---:|
| Offer to share papers | proposed | 0.57 |
| Subsequent acceptance | accepted | 0.21 |
| Repeated promise about four minutes later | unchanged | 0.93 |
| Conditional link offer | proposed | 0.81 |
| PageRank explanation mistaken by ASR for a task | none | 0.43 |
| Paper handoff | accepted | 0.86 |
| Code handoff | accepted | 0.78 |

All seven returned the intended leading label, but **only three exceeded 0.8 confidence**. The task candidates and remembered state were manually seeded after inspecting initial results. This is evidence that the representation is promising, not a held-out 100% accuracy result or an automatic task-discovery benchmark.

The repeated promise is especially useful: a 90-second transcript window cannot know that a task was accepted four minutes earlier. Explicit memory lets TypeSafe recognize a repeat instead of creating a duplicate. The LLM can discover and phrase candidate tasks; TypeSafe can compare each candidate with fresh speech; application code can retain evidence and control the state transition. Low-confidence acceptance still needs context or review.

## Changes I propose

1. **Add an optional fast provisional meeting draft.** Reuse the existing API abstraction with a separately configurable meeting-draft model, initially testing the configured Gemini model. Generate concise source-linked content while the full agent works separately. Measure time to first useful content and missing outcomes before choosing a default. The current scheduler has a 120-second initial context gate and shared notes/polish scheduling; simply inserting a 158 ms call behind that gate will not make the experience feel immediate.

2. **Prototype a commitment ledger under a feature flag.** Have the LLM emit candidate task IDs and supporting source IDs. For each relevant settled ASR update, ask TypeSafe about one candidate plus its remembered evidence. Run up to four independent calls concurrently. Keep proposed, accepted, changed, withdrawn and repeated/unchanged states distinct. Store anonymous owners when identity is not established. Apply results only to the state revision they examined; user edits and corrected ASR invalidate stale judgments. Present uncertain cases provisionally instead of silently asserting acceptance.

3. **Use targeted verification for changed facts.** Split compound generated items into independently checkable claims, retrieve enough adjacent evidence, and verify only new/changed claims. Validate exact source IDs in code. Send a specific disagreement to the LLM for a small repair, preserving supported content and user edits. Calibrate thresholds on labeled real examples; do not reuse 0.8 as a universal truth threshold. Preserve raw ASR so a suspicious "I'll" can remain unresolved instead of becoming an invented action.

4. **Explore an assumption ledger after the commitment path.** Store the actual assumption behind a decision, then compare relevant new facts against that specific premise. A failed premise could cause the LLM to revisit only affected notes and explain the consequence. This is a hypothesis suggested by the successful memory representation; this run did not validate it. Likewise, multi-turn action discovery still needs an automatic candidate generator and held-out evaluation.

Other useful extensions follow from the same design: deduplicate repeated promises, expire unresolved proposals, show which decisions depend on an uncertain assumption, and let source corrections trigger review of only the affected insights. These are proposed capabilities, not features already implemented.

## Artifacts, checks and reproduction

New benchmark modules are `benchmarks/meeting_mode/typesafe_real_{eval,audit,writer,ledger,coverage,coverage_isolated,report}.py`. Prompts, responses, timings and failures are retained in `benchmarks/meeting_mode/results/typesafe-real-20260918/` (the repository ignores this results directory). Exact source/reference hashes and compact results are preserved in the linked JSON summary.

Offline aggregation, with no API calls:

```powershell
. .\venv\Scripts\Activate.ps1
python -m benchmarks.meeting_mode.typesafe_real_report
python -m pytest tests/test_typesafe_real_eval.py tests/test_typesafe_experiments.py tests/test_typesafe_comparison.py tests/test_typesafe_granularity.py -q
```

The focused suite passed **27 tests**. Ruff's undefined-name/unused-import checks passed for the new real-meeting modules. Tests cover chronological context, bounded history, threshold behavior, invalid evidence, suggestion status, Windows atomic-save retry, and rejection of incomplete judge objects.

Live modules require `--live` and the configured credentials. The evaluator supports `--mode signals`, or `--mode package --meetings IN1009 --arm baseline`; the writer supports `--model-role meeting|cleanup`. Most runners refuse an existing output. Preserve this frozen run and choose a new destination before rerunning. Do not blindly rerun the failed coverage protocol or slow writer ablation.

Saved writer stages reported $0.2615; saved TypeSafe verification/ledger calls add about $0.0033 to the $0.0719 broad scan. These are component costs, **not the complete experiment bill**: judge calls, missing/cancelled responses and unreported production-agent charges are excluded. Zero Pi cost metadata is not evidence of free inference.

The integration design follows TypeSafe's guidance on [explicit state](https://docs.typesafe.ai/concepts/state), [independent fan-out](https://docs.typesafe.ai/patterns/fan-out), [citation checks](https://docs.typesafe.ai/cookbooks/citation_check), and [confidence](https://docs.typesafe.ai/confidence). The next acceptance test should use held-out meetings with human-labeled commitments and coverage, then replay the complete live scheduler to measure user-visible latency. Production behavior remains unchanged until those proposals are implemented and validated.
