# Insight review after a meeting

**Experimental:** insight review is available for testing. Its judgments, thresholds, and question selection may change.

Enable **Settings → Meeting Mode → Intelligence → TypeSafe fast judgments**, then **After End → Review uncertain insights at the end** for future meetings. Accept the TypeSafe sharing disclosure, supply a TypeSafe key in **Settings → API keys** or `TYPESAFE_API_KEY` in the environment or `.env`, and enable cloud intelligence for the meeting. Review is off by default. Changing this setting does not retroactively authorize sending older meetings to TypeSafe.

Normal sensitivity selects up to three questions by default. Thorough sensitivity selects more borderline cases; the maximum is configurable from one to five. Recording and saving do not wait for the service or for your answers.

## What the host sees

After final insights are saved, the dashboard checks action items, decisions, and risks. It selects consequential ambiguities about agreement, responsibility, deadlines, claim support, or risk resolution. Each question links to source excerpts and timestamps. Use **Review later** to collapse the panel, **Skip** to leave a question unresolved, or reopen it from **Reviewed or skipped**. The same controls work in Past Meetings.

Answers update the linked insight and create a protected user note. Source-linked meeting notes receive an explicit clarification. The transcript remains intact. Reports and Markdown exports place user clarifications before the earlier narrative and label remaining provisional or unsupported insights. Corrections supplement the narrative; they do not silently rewrite every sentence of the original summary.

You can revise an answer. A later separate edit to the insight supersedes its previous review and labels the earlier correction accordingly. Stale questions cannot overwrite newer wording. Human corrections are protected from subsequent agent edits using the existing item protection rules.

## Data and availability

The separate TypeSafe opt-in covers relevant transcript excerpts, original transcript text when available, grounded speaker names, and generated insights. Audio, recordings, other meetings, and API keys are not sent as model state. Requests use the fixed TypeSafe HTTPS endpoint; redirects are disabled. The key stays on the Python server and is never included in dashboard state or exports.

A missing key, timeout, service error, interrupted application, or changed transcript leaves the saved meeting available. The review panel explains the failure and supports retry. Reopening a meeting does not automatically restart an interrupted network request. Turning the TypeSafe master switch off prevents further requests. Turning cloud intelligence off prevents queued checks from starting and prevents their results from being applied; an already in-flight request cannot be recalled.

## Scoring policy

The meeting LLM still produces the insights. Jev checks each insight in a separate request, with up to four requests in parallel. Independent questions for that insight share one coherent evidence context. Context includes cited passages, neighboring speech, and other relevant passages in chronological order, so later corrections can affect the judgment.

The implementation stores separate Noul probabilities for applicable fields. These are probabilities of individual yes/no judgments, not a measured probability that an entire insight is true. Near zero means evidence against that proposition; it is not generic uncertainty. Strong contradiction, unreliable transcript evidence, and missing ownership can override otherwise high support. No score marks an insight as human-confirmed.

Normal and Thorough use initial field-support thresholds of 0.85 and 0.95 respectively. These are routing policies, not calibrated meeting-accuracy guarantees. Review priorities combine the field issue with whether it affects an action or decision. Questions are deduplicated and capped. Routine key points and free-form notes are not separately scored.

One pass checks at most 40 unprotected action items, decisions, and risks. Each request has a 12-second HTTP timeout and a context budget of 16,000 serialized characters. Larger meetings or partial failures are explicitly reported as partial. Retries check the current eligible items; user-corrected items remain protected. Lexical context retrieval can miss paraphrased later resolutions, and ASR errors can still fool the checker. User review and representative meeting benchmarks remain necessary when tuning these thresholds.

## Implementation and validation

- `meeting/insight_review.py`: consent-gated client, evidence selection, parallel checks, question selection, background worker.
- `meeting/state/review.py`: atomic operations, revision checks, answer provenance, linked notes, and correction freshness.
- `meeting/web/api.py`: authenticated review actions for live and archived dashboards.
- `webui/src/components/InsightReview.tsx`: review controls, source excerpts, and report clarifications.
- `tests/test_meeting_insight_review.py` and `webui/tests/insight-review.cjs`: synthetic provider, persistence, authorization, stale-result, failure, and interaction regressions.

The integration follows the [TypeSafe HTTP API](https://docs.typesafe.ai/api), [Noul semantics](https://docs.typesafe.ai/primitives/noul), and [citation-check pattern](https://docs.typesafe.ai/cookbooks/citation_check). The pinned model is `jev-1.13.0`.
