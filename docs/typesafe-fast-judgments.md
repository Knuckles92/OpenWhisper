# TypeSafe fast judgments

**Experimental:** semantic topic changes and spoken instructions are available for testing. Their behavior and thresholds may change.

Added September 18, 2026. Optional features share one small client for TypeSafe's System One model (`jev-1.13.0`): a decision service that answers narrow typed questions about short text in about 0.2 s and never generates prose. Since October 15, 2026 the same questions can instead go to [your own text model](#your-text-model), which can run on this computer. Everything here is off by default, needs what the chosen [judge](#judge-typesafe-openrouter-or-your-text-model) needs (a `TYPESAFE_API_KEY`, the `OPENROUTER_API_KEY` when connecting through OpenRouter, or a configured text model), and degrades to the previous deterministic behaviour whenever a judgment is disabled, unkeyed, or unanswered.

Evidence for the thresholds is in [the human-label benchmark](typesafe-human-label-benchmark.md); the earlier synthetic and LLM-judged work is in [typesafe-experiments.md](typesafe-experiments.md) and [typesafe-api-benchmark.md](typesafe-api-benchmark.md). The live ledger, insight verification and end-of-meeting review described in [typesafe-live-state.md](typesafe-live-state.md) are separate work and can reuse the client below.

## What ships

| Feature | Setting | Where it acts | Fallback |
|---|---|---|---|
| Master switch | Meeting Mode → Fast judgments → Fast judgments | gates every judgment | everything below stays off |
| Semantic topic changes | same page (on once the master switch is on) | `CheckpointScheduler._detect_topic_shift` | content-word Jaccard < 0.15 |
| Spoken instructions | same page (off) | `MeetingEngine._on_chunk_result` → `meeting/voice_commands.py` | none; nothing is applied |

Meeting-side judgments on a judge that sends text off this computer additionally require the meeting's AI insights to be on. The engine decides on the judge that will answer (`MeetingEngine._consented_judge`), per question, so turning AI insights off mid-meeting, or switching to a hosted judge, stops them at the next judgment. A judge on this computer needs no such consent; see [The consent gate](#the-consent-gate).

## Judge: TypeSafe, OpenRouter, or your text model

Added October 6, 2026; the third route October 15, 2026. OpenRouter serves the same model through a System One API that implements TypeSafe's request and response shapes (`POST https://openrouter.ai/api/v1/systemone`; responses add `id`, `provider` and `usage.cost`). **Meeting Mode → Fast judgments → Judge** chooses the route, saved as `typesafe_provider`:

| Route | Endpoint | Key | Model id | Billed to |
|---|---|---|---|---|
| TypeSafe (default) | `api.typesafe.ai/v1/systemone` | `TYPESAFE_API_KEY` | `jev-1.13.0` | TypeSafe account |
| OpenRouter | `openrouter.ai/api/v1/systemone` | `OPENROUTER_API_KEY` | `typesafe/jev-1.13` | OpenRouter credits |
| Your text model (`text_model`) | the chosen text model's endpoint | that endpoint's key, none for a local server | the model chosen on the Models page | that provider, nothing for a local server |

- The OpenRouter id is OpenRouter's name for the same 1.13 release; its responses name the dated snapshot (`typesafe/jev-1.13-20260917`). `typesafe/jev-1.13.0` does not exist there and returns HTTP 400, so the id is spelled out rather than derived from `MODEL`.
- The route's base URL, key variable and `X-Title` header come from the built-in OpenRouter text profile, so they cannot drift from cleanup and AI insights.
- The route never changes by itself. Choosing it changes who receives excerpts, so TypeSafe stays selected until the user picks another, even when only an OpenRouter key exists. The missing-key notice offers the one-click switch between the two Jev routes instead.
- Every caller resolves the route when it asks. Meeting workers that outlive a Settings change (`LiveSignals`, `CitationVerifier`) hold a `FollowingJudge`, the voice-command listener asks the engine for its judge per segment, and the engine rebuilds its cached judge when the route, model or key changes, so a switch mid-meeting applies to the next judgment.
- **Test** on the Judge card sends the same one-word verification judgment as API keys → Test, over the chosen route, and reports the latency. OpenRouter's 402 (no credits) and 404 (no allowed provider for the model) get their own messages. On the text-model route it waits up to 30 s, since a local server may first load the model, and reports a reply that is not a usable judgment separately from a network or HTTP failure.

Measured October 6, 2026 from the development machine, three verification judgments per route over a reused connection: 0.08–0.11 s direct to TypeSafe, 0.12–0.16 s through OpenRouter (first request 0.53 s and 0.25 s respectively).

## Your text model

`services/text_judge.py` asks Jev's questions of any `services.text_llm` profile: Ollama, LM Studio, vLLM, llama.cpp, LocalAI or a custom endpoint on this computer, or a hosted provider. A second bar under **Judge** picks which model, saved as `typesafe_text_model`: the **AI insights model** (default, Meeting Mode → Intelligence) or the **AI cleanup model**. The profile, address, key and model are the ones already set up for that feature (Dictation → AI cleanup, or Meeting Mode → Intelligence); there is nothing separate to configure.

Callers see no difference: `TextModelJudge` has the same `ask` contract, returns the same validated answer shapes (`validate_answers`), and returns `None` on any failure, so every feature keeps degrading to "no judgment" on this route.

How a request is built:

- One chat request per judgment: a fixed system prompt, and a user message that is the JSON `{"state", "questions"}`. The whole message is held to the same 24,000-character bound as a Jev request.
- **Noul** (yes/no): the model states the probability of yes, 0 to 1.
- **Choice**: options are numbered (an option can be a segment id or a span of dictation, and a number is short and hard to copy wrong); the model returns the option number and the probability that it is right. Jev's confidence formula, `(p - 1/n) / (1 - 1/n)` over `n` options, turns that probability into `confidence`. There is no distribution, so `probabilities` is empty, which callers already read as "not available".
- **Score** is refused; no feature asks it.
- Temperature 0, `max_tokens` of 64 plus 40 per question, and a strict JSON schema in `response_format`. A server that rejects the schema (HTTP 400 or 422 naming `response_format` or `json_schema`) is retried once within the same budget with plain JSON instructions, and that endpoint and model skip the schema for the rest of the session. `<think>…</think>` blocks are stripped before parsing.
- No client retries: a failed judgment is no judgment. One OpenAI client per endpoint and key is shared across judges (at most four kept).

Budgets. A text model writes its answer token by token, so it gets more time than Jev, while each feature keeps its own ceiling. A caller can only shorten the judge's budget, never extend it:

| Where | Jev | Text model |
|---|---|---|
| One judgment (default) | 4 s | 30 s, so live highlights, asked once per completed minute, cannot fall behind the meeting |
| Route by voice (the dictation waits) | 1.5 s | 4 s; a slower answer pastes the words as dictated |
| Semantic topic change | 5 s | 5 s; word overlap decides when no answer arrives |
| Semantic history search, whole ranking | 20 s | 20 s, then keyword results |
| Insight review, per judgment | 12 s | 30 s |
| Final highlight pass, total | 30 s | 30 s, newest minutes first so a slow judge covers the last, never-checked minute |

Calibration. The probabilities are the model's own statement, asked at temperature 0 under a JSON schema, not token log-probabilities: Ollama's OpenAI-compatible API does not return log-probabilities and the other local servers do not document them on chat completions. Stated probabilities are not calibrated the way Jev's are. The features' thresholds (topic change 0.5, voice commands 0.6, citations 0.7, highlights 0.8, radar 0.85, Route by voice's route confidence) were measured or set on Jev; on this route they are policy, not measured operating points. Settings says so on the Judge card: slower than Jev and not benchmarked like it.

Models that follow JSON instructions with thinking off work best. The judge sends no reasoning setting, so a thinking model thinks as its server defaults, and its thoughts can use up the small token budget before the answer; Test's failure message suggests turning thinking off.

### The consent gate

Where the judge runs decides whether a meeting's AI insights consent is needed. `JudgeRoute.is_local` is true only for the text-model route on a loopback address (`localhost`, `127.0.0.1`, `::1`); a server on another computer counts as remote.

- **Judge on this computer**: live highlights, the open questions radar and voice commands run in meetings with AI insights off, and nothing is sent anywhere. Semantic history search ranks passages from every saved meeting, not only those that had AI insights on (`search_candidates(shared_only=False)`), and says "Ranked on this computer". Route by voice says the words are "checked on this computer".
- **Judge elsewhere** (Jev, or a hosted text model): unchanged. Meeting judgments wait for AI insights to be on for the meeting, and search ranks only meetings that had AI insights on, rechecked before every batch. The highlights lane reports `needs_insights` instead of running, and voice commands say they need AI insights on, or a judge on this computer.
- Some features need AI insights regardless of the judge, because what they check comes from AI insights: citation checks look at generated cards and live notes, insight review checks the final insights (and still needs its own opt-in), semantic topic changes fire early agent checkpoints, and the `recap` voice command queues on the note agent.
- The AI insights consent dialog names a hosted judge as a second recipient of excerpts ("Fast judgments are on, so short transcript excerpts also go to …"), and the meeting tab only says transcript text stays on this computer when both the meeting model and the judge are local.
- The master switch and each feature's own switch still apply on every route.

### Checking a text model against Jev

The thresholds above are Jev's. Before relying on a text model, score it on the same human labels:

- `benchmarks/typesafe_human_labels/` sends its jobs straight to `api.typesafe.ai` (`ts_client.run_jobs`). To score a text model, run the same jobs through `TextModelJudge.request(state, questions, timeout_s)`, which raises instead of returning `None`, so failures can be counted. Build the judge from the saved settings with `typesafe_provider` set to `text_model` (`text_model_route(settings)` and `route_key(route)`); a partial settings dict resolves the default text model, not yours.
- Drop Score questions (E1 `note_worthy`, E3 `note_worthiness`); the text route refuses them. Code that reads Choice `probabilities` gets an empty mapping; `confidence` is filled.
- Run a local server's jobs one at a time; the harness's concurrency was set for TypeSafe.
- The experiments closest to shipped features are E2 (citation support), E3 (topic boundary), E4 (voice commands on real ASR segments, including the false-positive count) and E8 (which later utterance answers a question). Compare each metric, at the shipped threshold, with Jev's in `benchmarks/typesafe_human_labels/results/summary.json` and [the human-label benchmark](typesafe-human-label-benchmark.md), and record latency, since the budgets above cut off slow answers.
- `benchmarks/voice_routing/run.py --live` measures Route by voice through `judge_from_settings`, but its `--provider` accepts only `typesafe` and `openrouter` so far.

## When there is no key

Quiet degradation is the right runtime policy — a dead network must not interrupt capture — but it makes "you never set a key" indistinguishable from "nobody said anything checkable." Settings therefore reports the configuration case, which runtime deliberately will not:

- **Meeting Mode → Fast judgments** shows a notice while the chosen route's key does not resolve, with a button that opens API keys on that credential, and a **Use OpenRouter** / **Use TypeSafe** button when the other Jev route has a key. The nav rail reads `No key` instead of a feature count, and appends `· OpenRouter` to the count on that route. A text model whose endpoint takes no key (a local server) always counts as keyed (`route_key` returns the placeholder key the OpenAI client needs).
- **API keys → Test** verifies a TypeSafe key with one minimal judgment (`services.typesafe.verify_key`) and reports the status class. TypeSafe is not an OpenAI-compatible endpoint, so it cannot use the shared `verify_api_key` probe.

`services.typesafe.key_present()` answers "is the chosen route's key resolvable" on its own, separate from `is_configured()`, which also requires the master switch — the two cases need different copy and a different next step.

## Privacy

Each judgment sends a short excerpt to the judge: about two minutes of transcript for a topic check, and one segment plus three predecessors for a voice command. On the TypeSafe route it goes to `api.typesafe.ai`; on the OpenRouter route to `openrouter.ai`, which passes it to TypeSafe; on the text-model route to that model's endpoint, which for a local server means it stays on this computer. The response is a probability or a label, not text. Dictation is never judged, except the words Route by voice is asked to route. See [Network and privacy](network-and-privacy.md) for every destination.

## Semantic topic changes

The shipped trigger fires an early agent checkpoint when the two most recent minutes share few content words. On 96 human-labelled minute pairs it fired on 89, for precision 0.35. The Noul "does `window` move to a different agenda topic than `previous_window`?" reaches precision 0.69 at recall 0.78 at the 0.5 threshold. The scheduler asks it at most every 10 s, only after both windows pass the existing eight-content-word guard, and treats `None` or any exception as "no answer", handing the decision back to Jaccard. Cost is roughly $0.02 per meeting hour.

## Spoken instructions to the note taker

Two gates, in this order:

1. **Code**: the segment must contain a wake name. Defaults are `note taker`, `notetaker`, `assistant`, `openwhisper`, `open whisper` (matched as whole words, hyphen or space). The bare word `whisper` is deliberately absent. The list is the `typesafe_voice_command_names` setting.
2. **The judge** (Jev, or your text model): a Choice over `none`, `mark_decision`, `mark_action`, `note_this`, `recap`, `fix_transcript`, `set_topic`, applied at confidence ≥ 0.6.

The first gate exists because the judge alone, while producing zero false positives on 884 real segments, does fire on person-directed requests such as "can you write that down for me in your notebook" (0.98). With the wake name required, 18 of the 24 benchmarked command phrasings still trigger and none of the person-directed ones do.

Applied ops use the `system` actor with `voice_command` attribution and land as `proposed` items, never as human-edited or confirmed, so a mistake is one click to remove and cannot masquerade as something the user typed. Decisions and action items copy the one or two segments spoken within 20 s before the command and cite them plus the command segment as evidence. `note_this` writes a key point. `set_topic` copies the phrase after a lead-in such as "new topic:", "moving on to", "set the topic to" and does nothing when no phrase follows. `recap` queues a request on the existing note agent and adds a cited Recap block to live notes. The dashboard shows queued, completed, or unavailable feedback. `fix_transcript` accepts explicit “replace X with Y”, “change X to Y”, and “I said Y, not X” forms only when X occurs in recent speech. It adds a system-attributed, removable term-correction note; the raw transcript stays intact.

Judgment and application run on one background thread so a slow answer never delays a transcript commit. The engine emits a `voice_command` event with applied and rejected counts.

## Code map

- `services/typesafe.py`: pinned model, the `JudgeRoute`s (TypeSafe, OpenRouter, and `text_model_route` built from Settings), `route_is_local`, `route_key`, credential, `urllib` transport with the app's verified TLS context, response validation, failure policy, usage counters, per-route budgets, and `FollowingJudge`.
- `services/text_judge.py`: `TextModelJudge` (prompt, numbered options, JSON schema with fallback, parsing into Jev's answer shapes) and `verify_text_model`.
- `meeting/agent/typesafe_signals.py`: meeting-side questions worded exactly as benchmarked, with thresholds.
- `meeting/agent/scheduler.py`: optional `topic_judge` and `_semantic_topic_shift`.
- `meeting/voice_commands.py`: wake pattern, referent selection, op construction, background listener.
- `meeting/engine.py`: `_typesafe_judge`, `_consented_judge`, `_judgments_consented`, `_typesafe_topic_judge`, `_voice_command_listener`, shutdown in `_stop_asr`.
- `services/settings.py`: `typesafe_*` keys and resolvers, including `TypeSafeProvider.TEXT_MODEL` and `TypeSafeTextModel`; `ui_qt/dialogs/settings_dialog.py`: tiles, the Judge bars, and the API-key row.
- Tests: `tests/test_typesafe_service.py`, `tests/test_text_judge.py`, `tests/test_voice_commands.py`, `tests/test_scheduler_semantic_topic_shift.py`, plus updated layout and API-key page tests.

## Not done, on purpose

- A sensitive-dictation gate in front of cloud cleanup shipped on September 18, 2026 and was removed on September 22. Screening sent every cloud-bound dictation to TypeSafe first, so the text still left the machine, and it added a network round trip before each cleanup.

- Per-occurrence gating of term corrections (measured 87% vs 53% for replace-all) is deferred: corrections are applied server-side in two places and mirrored in the browser, so a server-only gate would make the notes disagree with the live transcript.
- Agenda coverage tracking needs an agenda concept and a dashboard panel; the benchmark supports it (63% per-window accuracy, exact coverage sets on three of four meetings) but it is a feature, not a switch.
- Text-only choice between ASR hypotheses and text-only artifact detection were tested and rejected.

## Advisory citations, semantic search, radar, and pulses

These four controls are **off by default** on **Meeting Mode → Fast judgments**. Each describes the text it gives the judge and also requires the master switch and the chosen judge's key. Live features additionally require the meeting's AI insights unless the judge runs on this computer ([The consent gate](#the-consent-gate)). They do not enable themselves when a meeting starts.

- **Advisory citation checks** annotate generated cards and live notes as supported, conflicting, unsupported, uncertain, missing, or unavailable. They check the cited speech, not external truth. Checks never reject, rewrite, confirm, or remove insights. Edits and source changes invalidate old checks; provider failures leave the insight intact. This is separate from the optional end-of-meeting clarification questionnaire.
- **Semantic history search** adds Meaning / Keywords to Past Meetings. Broad local keyword matches and diverse recent passages form a shortlist of at most 48 passages; the judge ranks relevance, including paraphrases, in batches of eight within a 20-second budget. This is bounded reranking, not an exhaustive embedding index: older passages with no lexical overlap can be omitted. Only cloud-enabled saved meetings enter remote ranking (Jev or a hosted text model); a judge on this computer ranks every saved meeting. Results retain meeting and timestamp links. Unavailable or disabled ranking visibly falls back to keyword results. Agent recall uses the same ranking when its separate past-recall opt-in is enabled.
- **Open questions radar** copies substantive unanswered questions from each completed transcript minute into the existing question inbox. The judge filters rhetorical, already answered, and duplicate questions. It respects the seven-question cap and dismissed questions. Later source-selected answers are suggestions for the host to accept; the radar does not automatically close questions.
- **Live highlight pulses** mark decisions, disagreements, dated commitments, meaningful numbers, and **Takeaways** in five labelled, colored lanes. Takeaways capture explicitly stated key insights, lessons learned, and substantive conclusions; routine status updates, isolated facts, and requests to generate takeaways do not qualify. One request per completed minute batches five Noul judgments and source-anchor Choices, plus radar questions when enabled. Every pulse copies its source passage and timestamp and requires a detection probability of at least 0.8 and a valid source anchor. Clicking a pulse refreshes the live audio snapshot, seeks to that timestamp, and highlights the transcript. Pulses persist in history and exports; playback requires retained audio. Completed minutes queue behind a busy worker, transcript revisions replace that minute's pulses, and a bounded final pass checks remaining speech, including the last partial minute. Adding a new pulse type does not regenerate previously saved meetings.

Requests are bounded and run outside audio capture. A slow service queues completed minutes for later checks; the final pass has a 30-second total request budget and works from the newest minute back. Highlight thresholds (0.8), radar thresholds (0.85), and citation confidence (0.7) are initial product policies, **not newly benchmarked accuracy guarantees**, on Jev and even more so on a text model. The new combined workflow's live accuracy, latency, and meeting-hour cost have not been measured; the proposed $0.02/hour is a target, not a verified price for this implementation.

Offline coverage includes consent and revocation, source/revision invalidation, persistence, queue bounds, literal corrections and undo, recap routing, semantic fallback, pulse click anchors, and playback metadata races.


## Live voice-command acknowledgements

Parakeet meeting previews check a rolling eight-second audio window at roughly
one-second intervals when the shared model is available. Preview audio is copied
into a bounded queue after the recorder receives it. Queued durable transcription
has priority; slow preview inference reduces preview frequency to leave capacity
for the transcript. Recording and its recoverable WAV chunks keep their normal
cadence. Native streaming engines also forward their preview utterances.

Wake-named previews are classified by the Fast judgments judge on a background worker. Only the latest
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
store succeeds; a missing key, missing AI insights consent for a judge off this computer, failed
judgment, or rejected write cannot claim success. Restart a source-launched app to load code changes.

The voice-command tests use synthetic text and fake judges; model quality and the
one-second target still depend on the configured hardware and service latency.
