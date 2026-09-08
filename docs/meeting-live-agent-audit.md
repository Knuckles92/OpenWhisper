# Live meeting-agent audit

The live workflow now corrects context-supported transcription mistakes, revisits
notes after human guidance, and gives queued note requests priority over follow-on
background work. Restart the source-run OpenWhisper application to load the Python
changes. Compatibility handling supports the currently installed meeting-agent
bundle; it does not require replacing the installed component.

## Findings and repairs

| Finding | Repair |
| --- | --- |
| The installed sidecar did not read the newer human-guidance or note-request fields. A valid request could therefore reach the worker without reaching the model. | Current bundles advertise support for host-built prompts. Older bundles receive the complete notes prompt through their supported per-pass persona field, including user context and explicit requests. |
| The first polish pass required six successful dashboard checkpoints; the worker skipped all background work during silence. | Normal checkpoints correct clear ASR errors immediately. A separate polish review can run after the first checkpoint and a 15-second delay, including during silence; later reviews become due after six checkpoints or 45 seconds with fresh work. Clean, unchanged transcript is not repeatedly sent. |
| Notes started only after two dashboard passes. | Notes seed after the first successful dashboard checkpoint. |
| Guidance refreshed dashboard cards but consumed transcript ids prevented notes from reconsidering the same speech. | Guidance explicitly triggers a notes review, even without new speech, with bounded retries. Reviews do not advance past unprocessed note backlog. New guidance arriving during a pass remains pending. |
| New transcript batches omitted the earlier lines needed to recognize a contextual mishearing. | Checkpoints carry up to 24 recent, already-seen transcript lines with exact evidence ids, separately from the new-speech delivery cursor. |
| Cleanup edits did not reliably refresh their dependent claims and notes. Sidecar responses discarded the actual operations and retained only counts. | The Python sidecar bridge retains actual patch results. Transcript revisions trigger reconciliation; both backends preserve evidence and human-edit protections. |
| Follow-on notes/polish could run before a queued user request, and a sidecar pass could occupy the shared worker for five minutes. | Queued requests precede follow-on background work. Sidecar live requests have a 60-second hard deadline and 45-second stall limit; final consolidation retains its separate limits. New human guidance gets an immediate attempt despite prior automatic backoff. |
| JSON fallback accepted rejected edits as a completed pass without presenting validation failures to the model. | It now returns rejection reasons and revisions for bounded repair attempts, avoids replaying successful edits, respects protected human items, and reports unresolved failures. |

The correction policy distinguishes phonetic resemblance from supporting context:
“Entropic makes Claude” in an AI-vendor discussion should become “Anthropic makes
Claude”; “entropic forces” in thermodynamics should remain unchanged. Numbers,
negation, uncertainty, and speaker meaning must be preserved. Unclear names remain
unchanged until context supports a correction.

## Verification

- Complete meeting regression suite plus text-generation tests: 909 passed,
  4 skipped, and 4 subtests passed.
- After the final compatibility change: 139 targeted Python tests passed.
- Sidecar TypeScript typecheck, 8 tool/provider tests, and bundle build passed.
- The real configured model was exercised with synthetic meeting text through
  both the rebuilt sidecar and the existing installed component. The installed
  run exposed the dropped note-request problem and was repeated after its repair.
  All six final checks passed using active meeting state on the installed bundle.

The reusable model evaluation is `benchmarks/meeting_mode/live_agent_eval.py`.
It tests automatic name correction and initial notes, human guidance applied to
existing notes, explicit requests without new speech, valid use of “entropic”,
ambiguous names, and correction from later context. It uses the configured
provider and can incur normal model charges; it does not read actual meetings.

```powershell
.\venv\Scripts\python.exe -m benchmarks.meeting_mode.live_agent_eval
# Test a rebuilt component instead:
.\venv\Scripts\python.exe -m benchmarks.meeting_mode.live_agent_eval --sidecar-dir sidecar/dist
```

Output defaults to `.tmp/live_agent_eval.json`, including synthetic transcript,
resulting state, scenario outcomes, and elapsed time. These are bounded behavioral
checks, not a claim of perfect transcription or a full real-audio meeting trial.
Cloud-model response time still determines how quickly an in-flight pass finishes.

## Final model evidence

The final run used the configured `google/gemini-3.8-flash` model through
OpenRouter and the installed Pi sidecar.

| Scenario | Passed | Elapsed seconds |
| --- | --- | ---: |
| automatic name correction and live notes | Yes | 21.12 |
| human guidance updates existing notes | Yes | 16.84 |
| explicit note request without speech | Yes | 8.12 |
| valid entropic is preserved | Yes | 13.64 |
| ambiguous name is not guessed | Yes | 78.03 |
| later context repairs earlier line | Yes | 22.02 |

Scenario times include all synchronous work in that scenario, often both the
dashboard and notes pass. The explicit user request completed in 8.12 seconds.
The ambiguous-name scenario took 78.03 seconds across successive passes, so
this is not a guarantee of instantaneous model output. Queued requests take
priority between passes, and each live sidecar pass has its own 60-second limit.

Machine-readable outcomes: [meeting-live-agent-audit.json](benchmarks/meeting-live-agent-audit.json).
