# Maintaining a more accurate live meeting state

September 18, 2026. **A continuously maintained account of tasks, unresolved issues and decisions is a more useful target than repeatedly verifying the final prose. The new probe supports an evidence-based hybrid, but does not establish a production improvement from TypeSafe.**

The intended live behavior is concrete: a conditional paper offer becomes an accepted task when accepted; repeating the promise updates the same task; an evaluation concern becomes resolved within an agreed scope; an unrelated performance improvement does not close an unresolved correctness question. The LLM can then write notes from that evolving record, rather than reconstruct every relationship at the end.

## What I tested

The new `benchmarks/meeting_mode/typesafe_live_state_probe.py` replays selected chronological ASR updates for four manually named candidate records from public AMI meetings:

- IN1009: offering, accepting and repeating the promise to send paper links, with unrelated speech in between.
- IN1005: a concern about whether directory classification is a fair task-specific model comparison, the eventual acceptance, and later discussion of other weaknesses.
- IN1007: an unresolved correctness question in earlier FDLP experiments, followed by available code, faster execution and handoff plans that do not answer that question.
- IN1005: a mathematical PageRank explanation that must not become an after-meeting task.

Each lane consumes its own earlier predictions. Expected states are excluded from requests. Initial state is absent/unknown; candidates are manually supplied, so this does not test candidate discovery. There are 16 checkpoints, one of which is a deliberately incomplete utterance with two permissible states. The table scores the remaining 15 definite checkpoints. Labels were authored from the previously inspected human transcripts, not supplied by the previous Gemini judge.

| Method | Matching definite checkpoints | Median request |
|---|---:|---:|
| TypeSafe, feeding back previous state and evidence behind applied changes | 9/15 | 175 ms |
| Same approach, only applying predictions with confidence >= 0.8 | 3/15 | 185 ms |
| Gemini on the same small state-update task | 14/15 | 1.73 s |
| TypeSafe follow-up: retained source observations, no previous predicted label | 12/15 | 172 ms |

The confidence-gated method mostly stayed unknown or absent: its 12 misses are unavailable updates, not 12 false factual assertions. Its policy also retained evidence only when a state update was applied, so uncertainty caused useful observations to fall out of memory. It is a failed combination of model, threshold and memory policy, not a general TypeSafe capability score.

The evidence-first follow-up changes both instructions and history retention. It is an adaptive development experiment, not an isolated causal comparison or held-out test. Across all lanes there were 64 requests and zero API errors. The saved TypeSafe calls used 128,814 input tokens, approximately $0.0054 at the previously checked price; Gemini reported $0.0292. See the [exact results and trajectories](benchmarks/typesafe-live-state-2026-09-18.json).

## The important findings

**An old conclusion can become self-reinforcing.** The first TypeSafe lane left the papers at proposed even after acceptance. It subsequently became highly confident in the stale proposed status during unrelated speech. It also kept the evaluation concern open after the agreement. More calls alone do not create a more accurate live environment.

**Remembering observations works better than trusting generated labels.** The follow-up TypeSafe run handled all five paper checkpoints, all four correctness-concern checkpoints and both mathematical-explanation checkpoints as intended. The acceptance initially had only 0.53 confidence, however; a 0.8 threshold would still delay it. Preserving the evidence matters even when no update is applied.

**Subtle resolution still needs stronger interpretation.** For the evaluation debate, evidence-first TypeSafe resolved too early, recognized the eventual acceptance, then reopened the issue during later discussion. Gemini also resolved it one checkpoint early, but retained the right scope afterward. This is exactly the kind of state change where I would use an LLM to review the supporting and conflicting evidence.

**Small LLM updates are already reasonably fast.** The measured 1.73-second median was for one candidate's structured status. It is not the 12-14-second full-draft task or the multi-minute production-agent consolidation. This gives us another way to improve responsiveness even if TypeSafe only earns a supporting role.

These four selected episodes do not establish a live-product accuracy rate. They omit automatic candidate discovery, the complete stream, microphone/ASR latency, speaker identification, revisions to previous ASR, user edits and the actual production scheduler. The candidate records can only represent things supplied to the experiment. No changed-owner or cancelled-task case was tested.

## Proposed behavior in the app

1. **Keep a small evidence-backed record for each active matter.** Preserve the original source segments and their revisions, the candidate task/issue, the current interpretation, and observations that remain uncertain. Keep speaker/participant IDs when actually established; leave ownership unknown otherwise. Do not rewrite uncertain ASR into a confident promise.
2. **Inspect new speech against relevant records.** A separate bounded queue can run independent TypeSafe checks in parallel. Results should suggest a new proposal, confirmation, duplicate, possible contradiction or possible resolution. The app must discard stale results and preserve human corrections.
3. **Use a short LLM reconciliation when meaning changes or is ambiguous.** Give it the relevant source history, the previous accepted interpretation and the new evidence. Have it update the specific record, including scope and supporting source IDs. Merely waiting longer or receiving an unrelated upbeat remark should not close a concern. A dispute with settled state should request reconciliation, not immediately flip the visible record.
4. **Let notes and the final summary read from the evolving state.** Periodic LLM review still needs to discover missing topics, research ideas and outcomes. TypeSafe cannot track a candidate that no component noticed. The state record helps retain accepted commitments and resolved concerns, but does not replace coverage checks.

The fast UI could show one task moving from proposed to accepted, or an issue moving from open to resolved-within-scope, with a source link and a short explanation of what changed. Ambiguous transitions can stay pending. This is a proposed interaction, not a newly installed feature.

The approach is consistent with TypeSafe's [explicit-state design](https://docs.typesafe.ai/concepts/state) and [independent parallel questions](https://docs.typesafe.ai/patterns/fan-out). Its [confidence documentation](https://docs.typesafe.ai/confidence) also distinguishes distribution concentration from domain-specific action thresholds; 0.8 should not be treated as a universal correctness rule.

## Fit with the existing code

The current `meeting/agent/scheduler.py` waits for 120 seconds of initial context and runs checkpoints on a shared worker, with adaptive intervals of 5-20 seconds. Notes and transcript polish also use the agent. A low-latency signal path must avoid waiting behind that same queue, then serialize accepted updates through the existing state store. We have not measured this path end to end.

`meeting/state/schema.py` already gives card items stable IDs, revisions, source evidence and protection for human-edited, confirmed or pinned content. `meeting/state/patches.py` validates targeted operations, and `meeting/state/store.py` serializes and broadcasts them. Those are useful foundations.

A task's semantic status must remain separate from `CardItem.status`: the latter includes human confirmation and controls protection from agent edits. A model must not mark an item human-confirmed merely because its confidence is high. A dedicated semantic-state field and a separately tracked pending interpretation would be the appropriate design, with revision checks before applying asynchronous results.

My preferred next prototype is a feature-flagged live ledger that initially lets the LLM reconcile meaningful state changes while TypeSafe runs in observation mode. It should be promoted to applying narrow updates only after a full-stream comparison shows fewer stale/incorrect states and acceptable update delay. This leaves open a useful TypeSafe role without assuming that its faster calls necessarily improve the product.

Measure time until an accepted task appears, time until a genuinely settled concern clears, duplicate tasks, premature resolutions, status reversals, wrong owners, missing candidates and user corrections. Also compare the final delivered records so that apparent live improvements are not purchased by losing important content.

## Artifacts and validation

- Probe: `benchmarks/meeting_mode/typesafe_live_state_probe.py`.
- Frozen original loop: `benchmarks/meeting_mode/results/typesafe-real-20260918/live-state-loop.json`.
- Adaptive follow-up: `benchmarks/meeting_mode/results/typesafe-real-20260918/live-state-evidence-first.json`.
- Compact versionable results: linked above.
- Offline tests: `tests/test_typesafe_live_state_probe.py`; expected labels cannot enter requests, memory is copied, future evidence is rejected, and the confidence gate cannot silently promote an uncertain result.

The focused new and existing real-meeting suite passed 12 tests; Ruff checks passed. Live execution requires `--live`; `--evidence-first` selects the follow-up, and existing results cannot be overwritten. A 180-second process deadline bounds this research runner. All data sent was from the previously verified public AMI corpus. Production code, UI and settings remain unchanged.

## Optional review of uncertain insights

Follow-up design from the user's suggestion: use TypeSafe probabilities and confidence to route uncertain insights to a short, optional end-of-meeting review. This could recover useful ambiguous information that a strict automatic acceptance threshold would otherwise hide. It has not yet been implemented or measured with users.

Proposed setting: **Review uncertain insights at the end** (opt-in), with a Normal/Thorough sensitivity setting and a default cap of three questions. Ending the recording and saving the meeting must not depend on completing the review. Review can be skipped or reopened later. With the setting off, uncertainty remains represented; it does not silently become confirmation.

Assess each material dimension independently: support for the claim, whether a task was accepted, the owner, the deadline, or whether a concern was resolved. A known task with an unknown owner needs an ownership question, not another vote on the whole task. A confident prediction of "proposed" is still not an accepted task. For a yes/no Noul, a value near zero is evidence for no, not necessarily uncertainty; values near the middle are ambiguous. Choice confidence describes how concentrated the alternatives are, not a universal percentage that the insight is true. See [TypeSafe confidence](https://docs.typesafe.ai/confidence) and [Noul probabilities](https://docs.typesafe.ai/primitives/noul).

Use three product outcomes:

- Well-supported content can appear in the record with source evidence, labeled as inferred from the meeting rather than user-confirmed.
- Material ambiguity remains provisional. At the end, ask about the most consequential uncertainties that remain after considering later evidence.
- Unsupported or contradicted claims are not presented as established facts. Ask only if clarifying them would materially change an action, decision, owner or deadline; otherwise omit or retain as a clearly identified proposal.

Example review card: **Was sending the paper links agreed, or only offered?** Show the relevant excerpt and timestamp, with **Agreed / Only offered / Incorrect / Skip**. Another card could ask who accepted a task, offering only grounded participant choices plus **Unassigned / Someone else / Skip**. The LLM can phrase the narrow question from source evidence; TypeSafe scores help select the question.

Prioritize by consequence, unresolved ambiguity and expected value of an answer, rather than sending every score below a threshold to the user. Deduplicate related questions. Reconsider earlier uncertainties when later speech settles them. A source contradiction, unreliable ASR or missing ownership should be able to trigger review even when a model's label confidence is high. Do not multiply correlated scores into a fictitious overall truth percentage.

Implementation foundations already exist in `webui/src/components/QuestionInbox.tsx`, `meeting/state/schema.py` and `meeting/state/patches.py`: suggested answers, evidence links, user answers, dismiss/reopen, and an existing question cap. Existing 0.4/0.8 question bands are application policy, not calibrated TypeSafe thresholds. The new flow needs an explicit link from each question to the insight field and revision it can update; recording an inbox answer alone does not automatically repair the corresponding card and final report.

Apply a user's answer as user-provided information, protect the corrected field from later agent overwrite, and refresh the affected record and derived notes. Preserve what came from the transcript separately from what the user clarified afterward. Keep recording/transcript facts intact. Model confidence must never masquerade as user confirmation.

Validate thresholds separately for task acceptance, ownership and other judgments using labeled examples. Measure incorrect facts left in the final record, useful insights rescued, questions per meeting, review time, skip rate, and how often an answer changes the final deliverable. This is a concrete way the proposed human review could improve quality even when TypeSafe alone does not beat the LLM.
