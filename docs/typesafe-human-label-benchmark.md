# TypeSafe in OpenWhisper: new use cases benchmarked against human labels

Run: September 18, 2026. Model `jev-1.13.0` via `POST https://api.typesafe.ai/v1/systemone`. The harness lives in `benchmarks/typesafe_human_labels/`; per-experiment metrics and summaries are in `benchmarks/typesafe_human_labels/results/summary.json` (per-request rows were kept out of the repository). Every number below comes from those artifacts. The features that followed from this report are described in [typesafe-fast-judgments.md](typesafe-fast-judgments.md).

## Why this round is different from the earlier reports

The two earlier reports (`docs/typesafe-experiments.md`, `docs/typesafe-api-benchmark.md`) and the untracked `typesafe_real_*` probes used synthetic fixtures or LLM judges as ground truth. The AMI annotation release already downloaded under `benchmarks/meeting_mode/data/ami/annotations` also contains **human** annotations for about 140 scenario meetings that the ten IN meetings lack: dialogue acts with addressees, adjacency pairs (which utterance responds to which, and with what stance), abstractive summaries linked to the exact supporting utterances, extractive summaries, and topic segmentation. This round scores TypeSafe against those human labels, and uses the real Whisper and Parakeet drafts of the IN meetings for the ASR-noise questions.

Meetings used: `ES2008a`, `ES2008b`, `IS1008b`, `TS3005a` (four-person scenario meetings, human transcripts) and `IN1009`, `IN1005`, `IN1007` plus the other IN meetings for the transcript-selection test (real ASR drafts).

## Totals

| | |
|---|---:|
| TypeSafe requests | 6,457 across 10 experiments |
| Errors | 0 |
| Median request latency | 0.143 to 0.172 s per experiment |
| p95 | 0.24 to 0.29 s |
| Input tokens | 6,053,359 |
| Estimated TypeSafe cost, all experiments | $0.254 |
| LLM comparison arms (OpenRouter, provider-reported) | Gemini $0.078, DeepSeek E1 $0.025, for 270 and 150 items |

## Scorecard

| # | Judgment | Human gold | n | TypeSafe | Baseline | Verdict |
|---|---|---|---:|---|---|---|
| E1 | 9-way dialogue act (inform / suggest / offer / assess / elicit / social / understanding / minor / other) | AMI dialogue acts | 1,543 | 62.8% acc, macro-F1 0.50; 72.4% acc on the 64% with confidence >= 0.8 | rules 54.0% / majority 33.3%; Gemini 68.6% on a 140-item subset | Usable with a confidence gate |
| E1 | "speaker volunteers to do it" (offer) | AMI `off` | 53 pos | AUROC 0.918 | regex 0.52 F1 | Strong |
| E1 | "proposes a course of action" (suggest) | AMI `sug` | 167 pos | AUROC 0.928 | regex 0.16 F1 | Strong |
| E1 | "is a question" | AMI `el.*` | 108 pos | AUROC 0.850; F1 0.50 | "ends with ?" F1 0.54 | Weak on punctuated text; see E1b for ASR text |
| E1 | Note-worthiness (4-level Score) | in human extractive summary | 361 pos | AUROC 0.892 | word count 0.867 | Modest gain over length |
| E1 | Addressee (group vs a named participant) | AMI addressee attr | 1,113 | 72.3% | majority "group" 64.9% | Modest; 60% precision when naming an individual |
| E2 | Claim <-> segment support | AMI summary links | 332 pairs | AUROC 0.906 (0.94 vs far negatives, 0.85 vs adjacent negatives); precision 0.94 / recall 0.49 at 0.5 | none | Strong, conservative |
| E2 | Pick supporting passage from shortlist | AMI summary links | 46 | 82.6% top-1 | chance 29% | Strong |
| E3 | Checkpoint gate: minute has note-worthy content | extractive summary in window | 102 | AUROC 0.975 | word count 0.924; segment count 0.763 (what the scheduler uses) | Strong signal, small skip headroom on dense meetings |
| E3 | Topic boundary between consecutive minutes | AMI topic segmentation | 97 | AUROC 0.845; P 0.69 / R 0.78 | production Jaccard < 0.15 rule: P 0.35 / R 0.97 (fires on 89 of 96 windows) | Clear win over the shipped heuristic |
| E3 | Minute contains a decision or accepted task | summary links to actions/decisions | 26 pos | AUROC 0.735 | none | Weak at minute granularity |
| E4 | In-meeting voice command to the note taker | 884 real ASR segments + 41 synthetic | 967 | 0 false positives on real speech at any confidence; 41/41 recall | none | Strong, but 8/15 person-directed hard negatives fire: gate on a wake phrase in code |
| E4 | Cloud-sensitivity gate before sending text to a cloud LLM | 884 real + 27 synthetic | 911 | AUROC 0.9995; recall 0.96 and FPR 0.11% at 0.8 | none | Strong |
| E4 | ASR artifact / hallucination from text alone | reference-word overlap | 863 | AUROC 0.65 | none | Inconclusive: the "gold" artifacts were real speech outside the annotated span; flagged items were garbled real speech |
| E5 | Speaker change from text alone | AMI speaker labels | 305 balanced | AUROC 0.901; 81% acc; 100% on the decisive quarter | timing-overlap rule P 1.0 / R 0.57 | Useful complement to timing |
| E6 | Pick the more accurate of two ASR transcripts per minute | tcWER vs reference | 980 windows | Whisper: agrees with the WER oracle 27.5%; hybrid WER 34.8% vs draft 28.8%. Parakeet: 51.9% agreement | "pick the longer" beats both sources on Parakeet | Negative: fluency is not accuracy |
| E7 | Which agenda item is being discussed (30 s windows) | AMI top-level topics | 195 | 63.1% (7 to 9 items); 76.0% on the 62% with confidence >= 0.7; per meeting 44% to 81% | chance 10% to 12.5% | Promising for coverage tracking, too jittery for boundaries |
| E8 | Which later utterance answers a question | AMI adjacency pairs | 45 | 64.4% top-1; 83% at confidence >= 0.7 | "first utterance by another speaker" 84.4% | Negative vs the positional baseline |
| E8 | Response agrees with the source | adjacency-pair type POS | 324/369 | AUROC 0.892; P 0.97 / R 0.82 at 0.5 | majority 87.8% | Strong as a consensus signal |
| E8 | Response objects | type NEG | 25 pos | AUROC 0.857; P 0.42 / R 0.40 at 0.5 | none | Weak, few positives |
| E10 | Should this occurrence of a corrected term be rewritten? | authored | 30 | AUROC 0.964; 86.7% acc; recall 1.0 | blind replace-all 53.3% | Strong; errors are proper-noun homonyms |
| E1b | E1 detectors on lowercased, unpunctuated text | AMI dialogue acts | 1,543 | act 61.9% (vs 62.8%); offer AUROC 0.917 (vs 0.918); suggest 0.929 (vs 0.928); question 0.809 (vs 0.850) | "ends with ?" rule: F1 0.54 -> 0.00 | Robust to missing punctuation |

### LLM comparison on identical items

Same items, strict JSON schema, ten items per request, through the app's `services.text_generation` adapter with the configured models. TypeSafe numbers in this table are recomputed on exactly the items each LLM arm returned.

| Task | Arm | n | Quality | Median request (10 items) | Reported cost | Notes |
|---|---|---:|---|---:|---:|---|
| E1 act labels | Gemini 3.8 Flash (cleanup model, low reasoning) | 140 | 68.6% acc; question F1 0.50; note-worthy AUROC 0.856 | 3.9 s (2.7 to 10.9) | $0.0477 / 150 items | 1 of 15 batches returned truncated JSON |
| E1 act labels | TypeSafe on the same 140 | 140 | 61.4% acc; question F1 0.30; note-worthy AUROC 0.876 | 0.154 s per item | about $0.0077 / 140 items | |
| E1 act labels | DeepSeek V4.1 Flash (meeting model) | 150 | 69.3% acc; question F1 0.40; note-worthy AUROC 0.858 | 39.9 s (15.9 to 142.4) | $0.0252 / 150 items | reasons internally even with reasoning off (about 3,600 reasoning tokens per batch) |
| E1 act labels | TypeSafe on the same 150 | 150 | 62.7% acc; question F1 0.29; note-worthy AUROC 0.885 | 0.154 s per item | about $0.0082 / 150 items | |
| E2 support | Gemini 3.8 Flash | 105 | F1 0.90 (P 0.92 / R 0.88); ordinal AUROC 0.934 | 2.9 s | $0.0299 / 120 pairs | |
| E2 support | TypeSafe on the same 105 | 105 | F1 0.79 at 0.5 (P 1.00 / R 0.65); AUROC 0.955 | 0.151 s per pair | about $0.0028 / 105 pairs | ranks better, conservative at a fixed threshold |
| E2 support | DeepSeek V4.1 Flash | 88 | F1 0.81 (P 0.85 / R 0.77); ordinal AUROC 0.872 | 29.2 s (17.1 to 164.2) | $0.0379 / 120 pairs | 1 of 12 batches hit the output-length limit |
| E2 support | TypeSafe on the same 88 | 88 | F1 0.78 at 0.5 (P 1.00 / R 0.64); AUROC 0.954 | 0.151 s per pair | about $0.0024 / 88 pairs | |

Reading: the flash LLM is a better nine-way labeler (+7 points) and a better fixed-threshold support classifier; TypeSafe is a better ranker for support and note-worthiness, roughly 6x to 9x cheaper per item, and 20x to 25x faster per live item. For "show a provisional chip within a second of the segment landing", only TypeSafe fits; for the end-of-meeting rewrite, the LLM still does the writing.

## What each result means for the product

### E1: a dialogue-act stream is a cheap live substrate
Every stable segment can carry `act`, `is_offer`, `is_suggestion`, `is_question`, `note_worthy` and `addressee` for about 1,300 input tokens ($0.00005) and one 150 ms call, before any LLM checkpoint. The offer and suggestion detectors are the interesting ones: they are exactly the "proposed vs accepted" distinction the commitment ledger needs, and they hit AUROC 0.92 to 0.93 against human labels with no tuning. Nine-way act labels are only reliable when confidence is high, which is fine for a UI hint and wrong for automatic writes. Confusions are inform->assess (89) and inform->suggest (56), which are genuinely fuzzy in AMI too.

The note-worthiness Score beats word count only slightly (0.892 vs 0.867). That is an honest limit: long utterances are usually the important ones, and a Score adds little on top. It is still useful as a per-item importance feature for the spotlight ranker, which today ranks by pinned, human-touched, then recency (`meeting/agent/prompts.py:538-616`).

Addressee detection at 72% vs 65% majority is not good enough to assign owners on its own, but 60% precision when it does name a person is a usable prior for "who was asked to do this".

E1b repeats the whole run on lowercased text with punctuation stripped, which is what Parakeet and Nemotron drafts look like. Offer and suggestion detection do not move (AUROC 0.917 and 0.929). Question detection loses four AUROC points (0.850 to 0.809) but the punctuation rule the product could otherwise use loses everything (F1 0.54 to 0.00). Nine-way accuracy drops one point.

### E2: evidence support is real-data ready
This is the first non-synthetic confirmation of the citation verifier proposed in the earlier reports. Human annotators linked summary sentences to supporting utterances; TypeSafe separates linked from unlinked utterances at AUROC 0.91, with 94% precision when it says "supported". Recall at 0.5 is only 49% because a single utterance often supports only part of a summary sentence (the three-way `degree` answer says `partial` for 68 of 105 positives), so the right integration is a gate on "none", which gives F1 0.75, not a gate on "fully supported". The shortlist form (pick the supporting passage from six candidates) is 83% top-1, which matches the "select instead of generate" pattern and would let the writer cite evidence that TypeSafe picked rather than evidence the LLM guessed. Decisions section AUROC is 0.96, actions 0.905.

Integration point remains `_check_evidence` in `meeting/state/patches.py:166-177`, which today only checks that the `sg_` id exists.

### E3: the topic-shift trigger is the clearest replacement
The shipped early-checkpoint trigger in `meeting/agent/scheduler.py:353-392` fires when content-word Jaccard between two 60 s halves drops below 0.15. On human-labeled topic boundaries it fires on 89 of 96 windows: 35% precision. TypeSafe's one-Noul judgment reaches 69% precision at 78% recall on the same windows (F1 0.74 vs 0.51). It costs one call every 10 s, roughly $0.02 per hour of meeting.

The note-worthiness gate is a very strong signal (AUROC 0.975 vs 0.763 for the segment count the scheduler actually uses for cadence), but these scenario meetings are dense: 92 of 102 minutes contain human-extracted content, so the gate can only skip about 19% of checkpoints at a 1.0 threshold while keeping 96% of extractive words and 100% of decision/action minutes. Real meetings with more small talk would skip more. Decision-or-commitment detection at minute granularity is weak (AUROC 0.74); the per-utterance offer detector in E1 is the better place for that judgment.

### E4: two new workflows for the live meeting, one that needs a code gate
**Talking to the note taker.** Zero false positives on 884 real ASR segments while catching all 41 synthetic commands is the headline, but 8 of 15 person-directed requests ("can you write that down for me in your notebook", "remind me to email him") also fire, at up to 0.98. TypeSafe reads "note this" literally regardless of who is being addressed. The robust design is: code requires a wake phrase or an explicit reference to the notes (regex), TypeSafe labels the intent and consumes the slot from candidate values (which card, which owner among known participants). With the wake-phrase rule, 18 of the 24 command templates still trigger and none of the hard negatives do.

**Local-first privacy gate.** Before any text is sent to the cloud cleanup or meeting model, a single Noul flags credentials, identifiers, health or HR details about a named person, or confidentiality statements: AUROC 0.9995, 96% recall, 0.1% false positives on real research-meeting speech. That maps onto the app's cloud-consent model: route flagged utterances to the local model or hold them for review.

**Artifact detection from text is not viable** here, and the test could not even be scored properly: the 15 "gold" artifacts were nearly all speech outside the AMI annotation span (before 32 s or after the reference ended), and the segments TypeSafe flagged most strongly were garbled but real speech. Whisper with a prompt tail produced almost no true hallucinations in these drafts.

### E5: text-only speaker change complements timing
On balanced pairs, TypeSafe reaches 81% accuracy and is 100% correct on the quarter of pairs where it is decisive (p <= 0.2 or >= 0.8). The timing rule "the next utterance overlapped the previous one" has perfect precision but misses 43% of changes. Combined, they would group draft segments into turns before diarization has enough embeddings, and would give the "I'll take it" owner-attribution logic a turn boundary to work with.

### E6: do not let a text model pick between ASR hypotheses
This idea would have been a novel use of the offline re-decode (per-minute best-of-two). It fails clearly: on Whisper TypeSafe picked the offline re-decode in 369 of 484 decided minutes and agreed with the WER oracle only 27.5% of the time, so the hybrid was 6 points worse than the plain draft. The offline pass produces cleaner casing and punctuation but deletes more words, and fluency reads as accuracy to a text-only judge. On Parakeet the choices were at chance (51.9%) and the trivial "pick the longer transcript" rule beat both sources (27.6% vs 28.5% and 29.1%). Position bias was small (44% to 45% picked `a`). Keep transcript selection in code, using length and confidence, not semantics.

### E7: agenda tracking works as coverage, not as a live cursor
Given the meeting's agenda as named items, TypeSafe picks the current item in 63% of 30 s windows against 7 to 9 alternatives, and 76% when confident. Per-meeting spread is wide: 79% and 81% on two meetings, 44% on IS1008b where three items are "the X presentation" and content alone does not say which specialist is presenting. Boundary detection from window-to-window flicker is poor (precision 0.34). But the set of items it saw discussed matched the human set exactly in three of four meetings (Jaccard 1.0, 1.0, 0.857, 1.0), which is what "we never got to item 3" needs. The product has no agenda concept today (grep finds only prompt prose), so this is a new feature, not a replacement.

### E8: consensus yes, answer-pairing no
"Does this response agree with what it responds to?" is strong: AUROC 0.89, 97% precision. Layered on the offer detector it gives a live "was that actually agreed?" signal for decisions, addressing the gap that decisions can be recorded with nobody having agreed. Objection detection is weak with only 25 human-labeled objections. The five-way stance Choice underperformed the majority class because I included an `elaboration` option that never occurs in the labels and it absorbed 48 predictions; a reminder that Jev reads criteria literally and unused options are not free.

Picking which later utterance answers a question was worse than the trivial "first utterance by another speaker" rule (64% vs 84%), because in real conversation the answer usually is the next thing said. The product's question inbox should keep position as the primary cue and use the `agrees`/support Nouls only to grade the answer.

### E10: scope term corrections per occurrence
The correction feature in `meeting/corrections.py:95-108` rewrites every whole-word match for the rest of the meeting, which the agent prompt itself warns about ("entropic forces" is valid physics). A per-occurrence Noul gets 87% accuracy vs 53% for replace-all, never misses a case that should be replaced, and its four errors are all proper-noun homonyms it over-replaced ("the client John Peterson", "Mia Torres", "a ruby ring", "a parakeet for her birthday"). Two of the physics/movie cases sat within 0.03 of the threshold. As a gate it removes the worst behavior; it should not be trusted for names of other people.

## Ranked recommendations

1. **Replace the Jaccard topic-shift trigger** with the E3 boundary Noul (`scheduler.py:353`). Same cadence, one call per 10 s, doubles precision on human boundaries.
2. **Add the cloud-sensitivity gate** in front of `TranscriptCleanup.cleanup` and the meeting agent's outbound text, defaulting to local-only for flagged text. It fits the existing consent model and the local-first positioning.
3. **Per-segment offer/suggestion/question/agree stream** as advisory chips and as the input to the commitment ledger you are already building; it is the substrate the earlier reports lacked human validation for.
4. **Evidence support gate on `degree != none`**, recorded first, enforced later, at `_check_evidence`, plus shortlist-based citation selection for the writer.
5. **Wake-phrase voice commands** with TypeSafe intent labels (zero real-speech false positives once gated in code).
6. **Per-occurrence gate for term corrections** and speaker-change grouping for pre-diarization turns.
7. **Agenda coverage panel** (items discussed vs not), shown as coverage, not as a live position indicator.
8. Do not pursue text-only ASR hypothesis selection or artifact detection.

## Novel workflow ideas not tested here

- "You were asked": per-segment Noul "does this ask or assign something to the host?" as a discreet nudge; needs the host's name.
- Cross-card contradiction sweep after each checkpoint: pairwise Noul "does the newest segment contradict this card?" over the cards' cited evidence (the pipeline map found no cross-card consistency check).
- Semantic revise-matching: when the 45 s rolling re-decode replaces segments, match old to new by meaning rather than time IoU >= 0.25 so evidence anchors survive edits.
- Human-note satisfaction: after a note-agent request or `agent_insight`, ask "does the current card now reflect this guidance?" so guidance can expire instead of being re-sent forever.
- Risk severity Score and "decision without agreement" flags on the report views.

## Caveats

- Scenario meetings are scripted role-play with clean human transcripts; E1, E2, E3, E5, E7, E8 measure the judgment, not ASR robustness. E1b addresses punctuation only.
- Human links (E2) and topic labels (E7) are one annotator's view; AMI inter-annotator agreement is moderate for both.
- Class imbalance is severe for objections (25) and some dialogue acts; treat those numbers as directional.
- All thresholds were fixed before the runs; no tuning was done between runs, but each experiment was run once.
- Cost figures use the documented $0.042 per million input tokens and free output tokens.

## Files

All paths are under `benchmarks/typesafe_human_labels/`. Scripts: `ami_nxt.py` (NXT loaders), `ts_client.py` (bounded async caller), `exp1_da_tagging.py`, `exp1b_da_nopunct.py`, `exp2_support_links.py`, `exp3_content_gate.py`, `exp4_draft_signals.py`, `exp5_speaker_change.py`, `exp6_hybrid_select.py`, `exp7_agenda.py`, `exp8_responses.py`, `exp10_term_sense.py`, `llm_arm.py`. Run from the repo root with `venv\Scripts\python.exe benchmarks/typesafe_human_labels/<script>`; each script makes paid TypeSafe calls (the LLM arm makes OpenRouter calls), needs the AMI annotations already downloaded by the meeting benchmark, and writes to `results/` next to itself, overwriting its own output name, so rename before re-running to keep a prior artifact.
