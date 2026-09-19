# TypeSafe fast judgments

**Experimental:** semantic topic changes, spoken instructions, and sensitive-dictation screening are available for testing. Their behavior and thresholds may change.

Added September 18, 2026. Three optional features share one small client for TypeSafe's System One model (`jev-1.13.0`): a decision service that answers narrow typed questions about short text in about 0.2 s and never generates prose. Everything here is off by default, needs a `TYPESAFE_API_KEY` (Settings → API keys → TypeSafe), and degrades to the previous deterministic behaviour whenever a judgment is disabled, unkeyed, or unanswered.

Evidence for the thresholds is in [the human-label benchmark](typesafe-human-label-benchmark.md); the earlier synthetic and LLM-judged work is in [typesafe-experiments.md](typesafe-experiments.md) and [typesafe-api-benchmark.md](typesafe-api-benchmark.md). The live ledger, insight verification and end-of-meeting review described in [typesafe-live-state.md](typesafe-live-state.md) are separate work and can reuse the client below.

## What ships

| Feature | Setting | Where it acts | Fallback |
|---|---|---|---|
| Master switch | Meeting Mode → Intelligence → TypeSafe fast judgments | gates every judgment | everything below stays off |
| Semantic topic changes | same page (on once the master switch is on) | `CheckpointScheduler._detect_topic_shift` | content-word Jaccard < 0.15 |
| Spoken instructions | same page (off) | `MeetingEngine._on_chunk_result` → `meeting/voice_commands.py` | none; nothing is applied |
| Sensitive-dictation gate | Dictation → AI cleanup (off) | `TranscriptCleanup.cleanup` before the remote call | cleanup proceeds |

Meeting-side judgments additionally require the meeting's cloud intelligence to be on; the closures check `state.cloud_enabled` on every call, so turning cloud off mid-meeting stops them immediately.

## Privacy

Each judgment sends a short excerpt to `api.typesafe.ai`: about two minutes of transcript for a topic check, one segment plus three predecessors for a voice command, and the dictation itself for the sensitivity gate. The response is a probability or a label, not text. The sensitivity gate is therefore a trade, not a wall: flagged dictation is kept away from the cleanup model, but the gate itself has seen it. It is only consulted when the cleanup destination is remote, so a local endpoint never triggers a remote call, and it is described that way in Settings.

## Semantic topic changes

The shipped trigger fires an early agent checkpoint when the two most recent minutes share few content words. On 96 human-labelled minute pairs it fired on 89, for precision 0.35. The Noul "does `window` move to a different agenda topic than `previous_window`?" reaches precision 0.69 at recall 0.78 at the 0.5 threshold. The scheduler asks it at most every 10 s, only after both windows pass the existing eight-content-word guard, and treats `None` or any exception as "no answer", handing the decision back to Jaccard. Cost is roughly $0.02 per meeting hour.

## Spoken instructions to the note taker

Two gates, in this order:

1. **Code**: the segment must contain a wake name. Defaults are `note taker`, `notetaker`, `assistant`, `openwhisper`, `open whisper` (matched as whole words, hyphen or space). The bare word `whisper` is deliberately absent. The list is the `typesafe_voice_command_names` setting.
2. **TypeSafe**: a Choice over `none`, `mark_decision`, `mark_action`, `note_this`, `recap`, `fix_transcript`, `set_topic`, applied at confidence ≥ 0.6.

The first gate exists because the judge alone, while producing zero false positives on 884 real segments, does fire on person-directed requests such as "can you write that down for me in your notebook" (0.98). With the wake name required, 18 of the 24 benchmarked command phrasings still trigger and none of the person-directed ones do.

Applied ops use the `system` actor with `voice_command` attribution and land as `proposed` items, never as human-edited or confirmed, so a mistake is one click to remove and cannot masquerade as something the user typed. Decisions and action items copy the one or two segments spoken within 20 s before the command and cite them plus the command segment as evidence. `note_this` writes a key point. `set_topic` copies the phrase after a lead-in such as "new topic:", "moving on to", "set the topic to" and does nothing when no phrase follows. `recap` and `fix_transcript` are recognised and logged but not applied: both need generated text, which belongs to the LLM agent.

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
