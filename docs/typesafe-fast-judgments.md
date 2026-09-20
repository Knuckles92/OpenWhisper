# TypeSafe fast judgments

**Experimental:** semantic topic changes, spoken instructions, and sensitive-dictation screening are available for testing. Their behavior and thresholds may change.

Added September 18, 2026. Optional features share one small client for TypeSafe's System One model (`jev-1.13.0`): a decision service that answers narrow typed questions about short text in about 0.2 s and never generates prose. Everything here is off by default, needs a `TYPESAFE_API_KEY` (Settings → API keys → TypeSafe), and degrades to the previous deterministic behaviour whenever a judgment is disabled, unkeyed, or unanswered.

Evidence for the thresholds is in [the human-label benchmark](typesafe-human-label-benchmark.md); the earlier synthetic and LLM-judged work is in [typesafe-experiments.md](typesafe-experiments.md) and [typesafe-api-benchmark.md](typesafe-api-benchmark.md). The live ledger, insight verification and end-of-meeting review described in [typesafe-live-state.md](typesafe-live-state.md) are separate work and can reuse the client below.

## What ships

| Feature | Setting | Where it acts | Fallback |
|---|---|---|---|
| Master switch | Meeting Mode → Fast judgments → TypeSafe fast judgments | gates every judgment | everything below stays off |
| Semantic topic changes | same page (on once the master switch is on) | `CheckpointScheduler._detect_topic_shift` | content-word Jaccard < 0.15 |
| Spoken instructions | same page (off) | `MeetingEngine._on_chunk_result` → `meeting/voice_commands.py` | none; nothing is applied |
| Sensitive-dictation gate | Dictation → AI cleanup (off) | `TranscriptCleanup.cleanup` before the remote call | cleanup proceeds |

Meeting-side judgments additionally require the meeting's cloud intelligence to be on; the closures check `state.cloud_enabled` on every call, so turning cloud off mid-meeting stops them immediately.

## When there is no key

Quiet degradation is the right runtime policy — a dead network must not interrupt capture — but it makes "you never set a key" indistinguishable from "nobody said anything checkable." Settings therefore reports the configuration case, which runtime deliberately will not:

- **Meeting Mode → Fast judgments** shows a notice while no key resolves, with a button that opens API keys on the TypeSafe credential. The nav rail reads `No key` instead of a feature count.
- **The sensitive-dictation gate** states that it is screening nothing when it is switched on but TypeSafe is off or unkeyed, and that dictation is reaching the cloud endpoint unchecked. Cleanup still runs; only the screening step is missing. The warning is suppressed when cleanup is off or its endpoint is local, since nothing is being sent in those cases.
- **API keys → Test** verifies a TypeSafe key with one minimal judgment (`services.typesafe.verify_key`) and reports the status class. TypeSafe is not an OpenAI-compatible endpoint, so it cannot use the shared `verify_api_key` probe.

`services.typesafe.key_present()` answers "is a key resolvable" on its own, separate from `is_configured()`, which also requires the master switch — the two cases need different copy and a different next step.

## Privacy

Each judgment sends a short excerpt to `api.typesafe.ai`: about two minutes of transcript for a topic check, one segment plus three predecessors for a voice command, and the dictation itself for the sensitivity gate. The response is a probability or a label, not text. The sensitivity gate is therefore a trade, not a wall: flagged dictation is kept away from the cleanup model, but the gate itself has seen it. It is only consulted when the cleanup destination is remote, so a local endpoint never triggers a remote call, and it is described that way in Settings.

## Semantic topic changes

The shipped trigger fires an early agent checkpoint when the two most recent minutes share few content words. On 96 human-labelled minute pairs it fired on 89, for precision 0.35. The Noul "does `window` move to a different agenda topic than `previous_window`?" reaches precision 0.69 at recall 0.78 at the 0.5 threshold. The scheduler asks it at most every 10 s, only after both windows pass the existing eight-content-word guard, and treats `None` or any exception as "no answer", handing the decision back to Jaccard. Cost is roughly $0.02 per meeting hour.

## Spoken instructions to the note taker

Two gates, in this order:

1. **Code**: the segment must contain a wake name. Defaults are `note taker`, `notetaker`, `assistant`, `openwhisper`, `open whisper` (matched as whole words, hyphen or space). The bare word `whisper` is deliberately absent. The list is the `typesafe_voice_command_names` setting.
2. **TypeSafe**: a Choice over `none`, `mark_decision`, `mark_action`, `note_this`, `recap`, `fix_transcript`, `set_topic`, applied at confidence ≥ 0.6.

The first gate exists because the judge alone, while producing zero false positives on 884 real segments, does fire on person-directed requests such as "can you write that down for me in your notebook" (0.98). With the wake name required, 18 of the 24 benchmarked command phrasings still trigger and none of the person-directed ones do.

Applied ops use the `system` actor with `voice_command` attribution and land as `proposed` items, never as human-edited or confirmed, so a mistake is one click to remove and cannot masquerade as something the user typed. Decisions and action items copy the one or two segments spoken within 20 s before the command and cite them plus the command segment as evidence. `note_this` writes a key point. `set_topic` copies the phrase after a lead-in such as "new topic:", "moving on to", "set the topic to" and does nothing when no phrase follows. `recap` queues a request on the existing note agent and adds a cited Recap block to live notes. The dashboard shows queued, completed, or unavailable feedback. `fix_transcript` accepts explicit “replace X with Y”, “change X to Y”, and “I said Y, not X” forms only when X occurs in recent speech. It adds a system-attributed, removable term-correction note; the raw transcript stays intact.

Judgment and application run on one background thread so a slow answer never delays a transcript commit. The engine emits a `voice_command` event with applied and rejected counts.

## Sensitive-dictation gate

Before `TranscriptCleanup` sends dictation to a remote model it asks one Noul: does the text hold passwords, keys or credentials; government, bank or card numbers; health, disciplinary, salary or home-address details about an identifiable person; or an explicit confidentiality statement. At the 0.8 threshold the benchmark measured recall 0.96 on authored positives and a 0.11% false-positive rate on 884 real research-meeting segments. Flagged text is returned unchanged with `last_error` set to `skipped: sensitive content kept local`, which the existing UI shows the way it shows other skip reasons. A missing or failed judgment lets cleanup proceed.

## Code map

- `services/typesafe.py`: pinned model, credential, `urllib` transport with the app's verified TLS context, response validation, failure policy, usage counters, and the sensitivity question.
- `meeting/agent/typesafe_signals.py`: meeting-side questions worded exactly as benchmarked, with thresholds.
- `meeting/agent/scheduler.py`: optional `topic_judge` and `_semantic_topic_shift`.
- `meeting/voice_commands.py`: wake pattern, referent selection, op construction, background listener.
- `meeting/engine.py`: `_typesafe_judge`, `_typesafe_topic_judge`, `_voice_command_listener`, shutdown in `_stop_asr`.
- `services/transcript_cleanup.py`: `sensitivity_gate` and `_sensitive_for_cloud`.
- `services/settings.py`: `typesafe_*` keys and resolvers; `ui_qt/dialogs/settings_dialog.py`: tiles and the API-key row.
- Tests: `tests/test_typesafe_service.py`, `tests/test_voice_commands.py`, `tests/test_scheduler_semantic_topic_shift.py`, `tests/test_transcript_cleanup_gate.py`, plus updated layout and API-key page tests.

## Not done, on purpose

- Per-occurrence gating of term corrections (measured 87% vs 53% for replace-all) is deferred: corrections are applied server-side in two places and mirrored in the browser, so a server-only gate would make the notes disagree with the live transcript.
- Agenda coverage tracking needs an agenda concept and a dashboard panel; the benchmark supports it (63% per-window accuracy, exact coverage sets on three of four meetings) but it is a feature, not a switch.
- Text-only choice between ASR hypotheses and text-only artifact detection were tested and rejected.

## Advisory citations, semantic search, radar, and pulses

These four controls are **off by default** on **Meeting Mode → Fast judgments**. Each describes the text sent to TypeSafe/Jev and also requires the master switch and a key. Live features additionally require meeting cloud intelligence. They do not enable themselves when a meeting starts.

- **Advisory citation checks** annotate generated cards and live notes as supported, conflicting, unsupported, uncertain, missing, or unavailable. They check the cited speech, not external truth. Checks never reject, rewrite, confirm, or remove insights. Edits and source changes invalidate old checks; provider failures leave the insight intact. This is separate from the optional end-of-meeting clarification questionnaire.
- **Semantic history search** adds Meaning / Keywords to Past Meetings. Broad local keyword matches and diverse recent passages form a shortlist of at most 48 passages; Jev ranks relevance, including paraphrases. This is bounded reranking, not an exhaustive embedding index: older passages with no lexical overlap can be omitted. Only cloud-enabled saved meetings enter remote ranking. Results retain meeting and timestamp links. Unavailable or disabled ranking visibly falls back to keyword results. Agent recall uses the same ranking when its separate past-recall opt-in is enabled.
- **Open questions radar** copies substantive unanswered questions from each completed transcript minute into the existing question inbox. Jev filters rhetorical, already answered, and duplicate questions. It respects the seven-question cap and dismissed questions. Later source-selected answers are suggestions for the host to accept; the radar does not automatically close questions.
- **Live highlight pulses** mark decisions, disagreements, dated commitments, meaningful numbers, and **Takeaways** in five labelled, colored lanes. Takeaways capture explicitly stated key insights, lessons learned, and substantive conclusions; routine status updates, isolated facts, and requests to generate takeaways do not qualify. One request per completed minute batches five Noul judgments and source-anchor Choices, plus radar questions when enabled. Every pulse copies its source passage and timestamp and requires a detection probability of at least 0.8 and a valid source anchor. Clicking a pulse refreshes the live audio snapshot, seeks to that timestamp, and highlights the transcript. Pulses persist in history and exports; playback requires retained audio. Completed minutes queue behind a busy worker, transcript revisions replace that minute's pulses, and a bounded final pass checks remaining speech, including the last partial minute. Adding a new pulse type does not regenerate previously saved meetings.

Requests are bounded and run outside audio capture. A slow service queues completed minutes for later checks; the final pass has a 30-second total request budget. Highlight thresholds (0.8), radar thresholds (0.85), and citation confidence (0.7) are initial product policies, **not newly benchmarked accuracy guarantees**. The new combined workflow's live accuracy, latency, and meeting-hour cost have not been measured; the proposed $0.02/hour is a target, not a verified price for this implementation.

Offline coverage includes consent and revocation, source/revision invalidation, persistence, queue bounds, literal corrections and undo, recap routing, semantic fallback, pulse click anchors, and playback metadata races.


## Live voice-command acknowledgements

Parakeet meeting previews check a rolling eight-second audio window at roughly
one-second intervals when the shared model is available. Preview audio is copied
into a bounded queue after the recorder receives it. Queued durable transcription
has priority; slow preview inference reduces preview frequency to leave capacity
for the transcript. Recording and its recoverable WAV chunks keep their normal
cadence. Native streaming engines also forward their preview utterances.

Wake-named previews are classified with Jev on a background worker. Only the latest
pending preview per audio channel is retained. A preview can acknowledge a command
but cannot create or change a meeting item: the committed transcript is classified
again before applying an action with real source IDs. This prevents rolling windows
from saving duplicate notes. Short wake preambles split across adjacent segments on
the same channel are joined. Explicit “assistant, note that …” content and a point
spoken before the wake name in the same segment can be captured directly.

A bottom-center assistant bubble appears in the live dashboard, which opens in the
browser when a meeting starts. It shows heard, recognized, working, saved,
uncertain, and unavailable states. It can be dismissed and disappears after the
acknowledgement. The desktop app deliberately shows no bubble of its own: it drew
the same design at the same screen position as the dashboard's, always on top, so
every acknowledgement appeared twice and the two drifted out of phase. The dashboard
honors reduced-motion preferences. Saving a note is acknowledged only after the
store succeeds; a missing key, disabled cloud consent, failed judgment, or rejected
write cannot claim success. Restart a source-launched app to load code changes.

The voice-command tests use synthetic text and fake judges; model quality and the
one-second target still depend on the configured hardware and service latency.
