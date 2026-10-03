# Installed headless agent review — October 2, 2026

Keep Pi as the default for live meeting intelligence in this tested configuration. Offer installed Codex as an optional route for users who already have it signed in: it finished the short final consolidation faster, but did not improve delivered live notes. Do not replace all current engines on this evidence. Claude remains unranked because its OAuth login expired and refresh failed.

The saved implementation is on `claude/installed-agents` at `74bfc62b1d09427fb9b55018cda208886830894b`. It calls installed Claude Code and Codex headlessly, and installed OpenCode over ACP. It was never merged into main. This review used an isolated checkout and added a Codex MCP isolation fix there; nothing was merged or published.

## Real-model comparison

Three repetitions per available engine, shuffled with seed 42, sequential model calls. Each run exercised live cards, live notes, a human spelling correction, contextual transcript polish, and final consolidation through the real production cores and state validator. These are deterministic checks of delivered behavior, not a general language-model quality score.

| Mode and model | Median live pass | Median short final | Completed passes | Delivered checks |
|---|---:|---:|---:|---:|
| Pi / DeepSeek v4.1 Flash | 4.5s | 38.8s | 15/15 | 113/114 |
| Installed Codex / gpt-6.1-sol | 10.9s | 31.8s | 15/15 | 106/114 |
| Packaged OpenCode SDK / DeepSeek v4.1 Flash | 10.0s | 91.7s | 15/15 | 114/114 |
| Standard API / DeepSeek v4.1 Flash | 10.3s | 66.1s (1/3 completed) | 13/15 | 78/114 |

The live median pools 12 observations (four pass types × three repeats). Final medians use three attempts per engine except Standard, whose median excludes two stopped attempts. Do not interpret that successful-only median as reliable end-to-end finalization performance. Each completed run contributes 38 checks; unavailable Claude runs do not contribute to a quality denominator.

| Mode and model | Cards | Notes | Human correction | Polish | Initialization |
|---|---:|---:|---:|---:|---:|
| Pi / DeepSeek v4.1 Flash | 10.1s | 5.8s | 4.1s | 3.4s | 0.4s |
| Installed Codex / gpt-6.1-sol | 14.2s | 9.7s | 11.9s | 6.2s | 0.3s |
| Packaged OpenCode SDK / DeepSeek v4.1 Flash | 10.3s | 12.8s | 22.3s | 4.4s | 2.5s |
| Standard API / DeepSeek v4.1 Flash | 34.2s | 11.0s | 21.1s | 1.4s | 4.0s |

Per-phase medians use three observations; initialization uses nine observations per fully completed engine, seven for Standard (one core for the three live passes, one for polish, one for finalization). Pass timing excludes initialization and includes network and per-pass CLI process overhead. Timing ranges and raw observations are in the JSON companion.

Pi and the packaged OpenCode engine used the same configured OpenRouter model as Standard. Installed Codex used the user's available `gpt-6.1-sol` model, low effort for live passes and medium for finalization. The API engines retain their production defaults. This compares the available product configurations; it does not isolate transport from model, decoding settings, provider load, or caching. The first round partially overlapped Python tests; later rounds did not.

The scheduler also makes installed-agent meetings update less frequently: base checkpoint cadence is 45 seconds versus 15 seconds for the API engines; the notes minimum is 120 seconds versus 45 seconds, and polish minimum 180 seconds versus 45 seconds. A faster individual finalization does not imply faster live feedback.

## Blinded quality review

The quality panel reviewed every saved output against the original transcript. Candidate labels were randomized independently for each reviewer and repetition, and model/engine names, usage and timing were excluded from the inputs. The final panel comprises six valid requests using two model reviewers: DeepSeek v4.1 Flash through OpenRouter and gpt-6.1-sol through the isolated installed Codex driver. They evaluated each of the three output repetitions against the same rubric and weights.

The panel uses the archived report's chronological display order for notes/timeline, rather than raw insertion order. An earlier provisional panel exposed this presentation mismatch and is excluded from scoring. Three first API-review attempts truncated; they remain unjudged in `judge-failures/`. The final API reviewer uses a 20,000-token output budget and supported low reasoning effort; the Codex reviewer uses medium effort. Reasoning and visible output can share the API token budget, as described in [OpenRouter's reasoning documentation](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens). These reviewer settings do not alter any measured generation run.

| Mode and model | Live quality | Final quality |
|---|---:|---:|
| Pi / DeepSeek v4.1 Flash | 4.77/5 (6 ratings) | 4.82/5 (6 ratings) |
| Installed Codex / gpt-6.1-sol | 4.08/5 (6 ratings) | 4.95/5 (6 ratings) |
| Packaged OpenCode SDK / DeepSeek v4.1 Flash | 4.88/5 (6 ratings) | 4.85/5 (6 ratings) |
| Standard API / DeepSeek v4.1 Flash | 3.56/5 (6 ratings) | 4.94/5 (2 ratings) |

These are weighted rubric ratings out of five, not measured accuracy percentages. Live weights: card fidelity 20%, card coverage 15%, notes coverage 20%, human-correction fidelity 20%, polish fidelity 10%, notes usefulness 15%. Final weights: factual accuracy 25%, coverage 25%, commitment precision 25%, organization 10%, usefulness 15%. Missing final artifacts remain unscored for quality and are counted in delivery failures; Standard's final quality therefore represents only its one returned record, rated by two reviewers.

The factual rubric checks actual vs proposed commitments, the corrected Tuesday deadline, $500 rather than $5,000, vendor status, SQLite/offline/on-device requirements, the unanswered legal question, and completed export work. It scores whether notes remain useful on their own and whether the Maia correction preserves the other facts. It does not reward length. Source inspection of the completed decisions/actions found the actual Maya/Tuesday commitment and no invented Nia migration or purchase approval. A supplemental clock audit checked unique stamped items against their cited transcript spans: Standard 29/29, Pi 73/73, Codex 40/40; packaged OpenCode 68/69. Its wrong timeline anchor was 56 seconds for a correction cited at 48–54 seconds. This range check does not prove complete semantic grounding. Detailed deductions, individual reviewer ratings, source references, blinding mappings and input fingerprints are in the JSON companion and local `quality-*.json` files.

Both reviewers' models also appear in the generation comparison; hiding labels reduces explicit self-preference but cannot remove style bias. Repeated outputs of a small synthetic challenge set are not a broad meeting corpus. Small score differences do not establish a universal quality winner. The notes validator's rejection behavior also limits causal comparisons of model quality. The short fixtures lacked participant mappings and were rendered to producers with microphone labels as Me; reviewers were explicitly told to credit Me/host/the speaker for the initial first-person commitment. Attribution inferences remain weaker evidence than explicit owner statements. A literal old-name mention such as 'Maia, not Maya' can fail the strict spelling check while still communicating a correction; delivered-check totals are not factual accuracy percentages.

## Long-transcript retrieval

One additional production custom-report request per available route, with 1,200 synthetic segments and the relevant facts hidden in the middle, beyond the 60,000-character inline limit. The API custom-report implementation is shared by the API engine choices; it is tested once. This is a retrieval smoke check, not a latency ranking from repeated samples.

| Report route | Elapsed | Returned report | Checks |
|---|---:|---:|---:|
| openrouter / deepseek/deepseek-v4.1-flash | 22.4s | yes | 9/9 |
| codex / gpt-6.1-sol | 14.2s | yes | 9/9 |

Raw report text, retrieval tools used, corpus size, expected facts, and citations are saved in `long-report-*.json` in the local evidence directory. Both named-speaker runs retrieved all target facts and passed 9/9 checks. Source inspection confirmed the owner, corrected deadline, cap, pending vendor, technical constraints, open legal question, rejection of the unaccepted migration assignment, and fictional-approval distinction. The API report is substantially longer and adds unnecessary absence claims; Codex gives a concise targeted report. The first probe lacked participant mappings and therefore displayed all speakers as Me, making its Maya-name check invalid; those outputs are preserved under `fixture-issues/` and excluded. The corrected corpus is 211,630 rendered characters, with named participant IDs. One successful corrected probe per route is not a robust speed or quality ranking.

## Confirmed findings

1. **P1 — Codex inherited unrelated user MCP servers. Fixed only in this review checkout.** The original command added OpenWhisper without disabling the other configured servers. With the exact feature flags, both `fff` and `node_repl` remained enabled. The fix reads a fresh effective server inventory, explicitly disables unrelated servers, disables additional optional image/browser/worktree features, and refuses a pass if it cannot verify the inventory. The effective configuration after the fix contains only OpenWhisper enabled. The benchmark uses this corrected candidate. Source: `meeting/agent/installed/drivers.py:578`; evidence: `codex-mcp-isolation-after.json`.

2. **P2 — Descendant cleanup misses an already-exited parent. Still open.** `kill_process_tree` returns as soon as the parent has exited, so an owned helper can survive. A harmless parent-spawns-sleeping-child probe reproduced this, and the probe child was explicitly cleaned up. This does not claim actual Codex leaked during the normal benchmark. Windows job ownership / process-group handling needs to retain descendant cleanup after parent exit. Source: `services/installed_agents.py`; evidence: `process-cleanup-probe.json` and `cleanup_probe.py`.

3. **P2 — Existing cross-card duplicate detection drops legitimate minutes. Still open.** The `.90` containment threshold can reject a longer notes paragraph that contains a short existing card. Standard repeat 2 proposed four faithful note blocks, all rejected as `duplicate_item`; the live-notes card stayed empty despite a successful agent result. Replaying the exact operations rejected 4/4 with the existing cards and accepted 4/4 without them. Codex also produced the budget/deadline facts, but the validator rejected them. Its missing persisted facts therefore do not establish a weaker model. Pi passed most cases but lost the budget in repeat 3's first notes pass after three duplicate rejections. Source: `meeting/state/patches.py:302`; evidence: `dedup-probe.json`, `standard-2.json`, and Codex/Pi raw operations. Scope duplicate checks so live notes can restate card facts, and verify repair when every proposed write is rejected.

4. **P2 — Standard's request timeout is not a whole-pass wall limit. Still open.** Two finalization attempts, including initialization, returned no result and were externally stopped 473.9 and 347.6 seconds after their preceding pass completed. The exact internal phase could not be recovered, so there is no invented final-pass duration. Separately, a local HTTP endpoint trickled whitespace before returning JSON: the production tool-mode loop returned success after about 0.95 seconds with a 0.20-second pass deadline. SDK timeout limits inactivity; the core checks its deadline only between HTTP calls. Enforce a wall deadline around in-flight work as well as between rounds. Source: `meeting/agent/openrouter_direct.py:555`; evidence: `deadline-probe.json`, `deadline_probe.py`, and `standard-{2,3}-cutoff.json`.

## Validation

- Saved branch before the fix: 3,867 passed, 1 failed, 41 skipped, 3,271 passing subtests. The failure was the existing remote-upload damaged-file test; it passed on an isolated branch rerun and on the baseline. This is recorded as a timing-sensitive test observation, not a repaired defect.
- Focused installed-agent, picker, custom-report, live-workflow and rejection-repair checks after the fix: 191 passed.
- Integration snapshot of current main plus the candidate and fix: 4,532 passed, 42 skipped, 3,934 passing subtests, no failures/errors. The source merge had only a CHANGELOG conflict; no code conflict. This was a disposable archive, not a merge of either user checkout.
- Pi SDK: 9 tests passed and TypeScript check passed. Packaged OpenCode SDK: 13 tests / 99 assertions passed, TypeScript check passed, packaged self-test passed.
- Ruff undefined-name/unused-import checks for the affected code passed; `git diff --check` passed.
- Local probes reproduced MCP inheritance, orphan cleanup, cross-card notes rejection, and bypass of a request wall deadline. The latter two probes use no model API.

## Failed delivered checks

- Pi / DeepSeek v4.1 Flash, repeat 3, `live_notes`: budget_500.
- Installed Codex / gpt-6.1-sol, repeat 1, `live_notes`: budget_500, deadline.
- Installed Codex / gpt-6.1-sol, repeat 2, `live_notes`: budget_500, deadline.
- Installed Codex / gpt-6.1-sol, repeat 2, `human_guidance`: budget_preserved.
- Installed Codex / gpt-6.1-sol, repeat 3, `live_notes`: budget_500, deadline.
- Installed Codex / gpt-6.1-sol, repeat 3, `human_guidance`: budget_preserved.
- Standard API / DeepSeek v4.1 Flash, repeat 2, `live_notes`: applied_operations, nonempty_notes, budget_500, deadline.
- Standard API / DeepSeek v4.1 Flash, repeat 2, `human_guidance`: applied_operations, corrected_owner, budget_preserved, deadline_preserved.
- Standard API / DeepSeek v4.1 Flash, repeat 2, `consolidation`: transport_ok, applied_operations, corrected_deadline, correct_owner, real_decision, correct_budget, vendor_undecided, no_unaccepted_task, no_fictional_decision, completed_work_not_reassigned, offline_requirement, valid_evidence. Externally stopped: no result returned after the production request budget.
- Standard API / DeepSeek v4.1 Flash, repeat 3, `live_notes`: budget_500, deadline.
- Standard API / DeepSeek v4.1 Flash, repeat 3, `human_guidance`: obsolete_spelling_removed, budget_preserved.
- Standard API / DeepSeek v4.1 Flash, repeat 3, `consolidation`: transport_ok, applied_operations, corrected_deadline, correct_owner, real_decision, correct_budget, vendor_undecided, no_unaccepted_task, no_fictional_decision, completed_work_not_reassigned, offline_requirement, valid_evidence. Externally stopped: no result returned after the production request budget.

Failures caused by a transport cutoff are counted as undelivered checks, not factual model errors. All recorded operations and resulting state are retained to distinguish rejections from omitted or invented output.

## Scope and reproducibility

Baseline: released main `363734b2ba5cc0c952c93b925bd098d0c395304e`, archived before testing. The main working tree's ongoing uncommitted work was excluded. Candidate: `74bfc62b1d09427fb9b55018cda208886830894b` plus the two-file isolation fix. Integration validates compatibility with newer main; the real-model candidate runs use its own saved branch code plus that fix.

Runtime: Windows 11 / Python 3.12.10 / OpenAI SDK 3.6.0 / httpx 0.28.1; Pi SDK 0.84.1; OpenCode SDK 2.0.18 on Bun 1.3.14; Codex CLI 0.160.0; Claude Code 2.1.281. Installed OpenCode CLI was absent, so the live comparison covers the packaged SDK engine and fake ACP protocol tests, not a native installed OpenCode run.

Claude's local auth status initially said signed in, but the first real call failed with an expired OAuth session that could not refresh. It then reported signed out. Neither default Opus nor Haiku is ranked. Login must be restored with `claude auth login` before their live comparison can be completed.

Only synthetic transcript inputs were sent. No saved meetings or recordings were used. This does not benchmark ASR, audio capture, UI responsiveness, installer packaging, macOS/Linux, or the full real-meeting corpus. Three runs of a small challenge set support a configuration decision and defect discovery, not a statistically broad quality ranking. Raw token/usage fields are retained; differing harness accounting and unavailable price data prevent a reliable dollar-cost comparison. Pi's zero SDK cost fields do not mean free API use.

Local evidence: `.tmp/headless-agent-review-20261002/` in the primary repository contains `bench.py`, `matrix.py`, `long_report.py`, `quality.py`, source archives, manifests, test logs/XML, and raw JSON. `quality_summary.py` and `analyze.py` regenerate the summary; `write_report.py` regenerates this artifact after all four available arms have 15 recorded passes / 114 checks and six valid panel judgments. `bench.py` is a diagnostic collector: its process exit code alone is not a quality verdict; inspect `ok`, checks, operation rejections and state. The stopped Standard runs have nonzero matrix exits and explicit censored results. The JSON companion fingerprints each raw run and panel input. Model calls were sequential; `followups.py` records bounded execution of retrieval and quality requests after the timing matrix.

Rerun a single arm with the existing configured credential or CLI sign-in and the archived source:

```powershell
python .tmp/headless-agent-review-20261002/bench.py --source <source-directory> --harness <direct|pi|opencode|codex|claude_code> --model <model-id> --output <result.json>
```

Pi and packaged OpenCode also require `--sidecar-dir` pointing to the freshly built archived payload. The harness uses isolated writable application data. Keep provider credentials out of reports and command lines. All fixtures, scorer code, runtime versions and raw checks are available locally for audit.
