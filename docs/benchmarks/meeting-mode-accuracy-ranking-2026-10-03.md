# Meeting Mode accuracy pilots and ranking — 2026-10-03

**Follow-up:** The [full real-meeting implementation decision](meeting-mode-implementation-decision-2026-10-03.md)
supersedes the priority order below. It includes four complete held-out ASR
meetings and records the two changes implemented after these initial pilots.

## Decision

Prioritize **selective ASR retry** for the next product-scale evaluation. Its
hard-cut retry reduced word error on two held-out meetings' selected chunks,
and a separate cached fallback policy made a small improvement across all ten
AMI meetings. None of the five proposals has yet demonstrated an end-to-end
gain in the shipping Meeting Mode workflow. The numbers below measure different
outcomes and should not be added or compared as if they were the same metric.

The ranking is **priority for the next implementation experiment**, based on
observed accuracy signal, relevance to the current workflow, and the remaining
validation gap. A lower-ranked proposal may still help a specific failure case.

| Rank | Proposal | Measured change | Interpretation |
|---:|---|---|---|
| 1 | Selective ASR retry at hard cuts | Held-out selected-chunk WER **47.46% → 41.12%** (−6.34 percentage points; 552 reference words). Separately, a cached second-model selector changed ten-meeting tcWER **28.83% → 28.76%** (−0.07 points; 90,284 words). | Best direct transcription signal. The retry used CPU Whisper base, while Meeting Mode uses an auto-selected Whisper model. The cached fallback does not measure selective decode latency. |
| 2 | Capture-health probe and guided source check | Detected **100%** of injected mutes and 1%-gain faults, **71.1%** of 5%-gain faults, with **0/9,059** false alerts on annotated healthy speech windows. Detected **0%** of level-matched wrong-source swaps. | Reliable detection for dead or weak audio. No actual device correction or transcript gain was measured. |
| 3 | Source-aware speaker attribution | On one meeting's 737 manual word midpoints, a reference-blind 0.5-second source selector got **84.40%** channel attribution, versus **42.88%** for a constant majority-speaker baseline. Yet the same 300 seconds scored **33.20%** WER on the mono mix, **33.74%** on selected audio, and **35.90%** after parallel-headset ASR with an audio-only dominance filter. | Useful speaker signal, but tested source strategies worsened word accuracy. Four AMI close-talk headsets are unlike the product's mic and system-audio channels, and the majority baseline is not the existing diarizer. |
| 4 | Pre-meeting glossary and scoped term correction | Two term-dense CPU slices changed exact target-term counts **1/7 → 7/7**, while aggregate WER worsened **42.52% → 47.51%** (+4.98 points; 301 reference words). | Potentially useful for a high-value name or acronym, but the unconditional prompt hurt surrounding words. The separate human scoped-correction part was not measurable with the available corpus. |
| 5 | Claim evidence check before final minutes | On 26 existing action/decision cards, a threshold chosen on two meetings retained all ten held-out cards and matched the default's **8/10 (80%)** agreement with a prior model audit; it flagged **0/2** held-out overstated cards. | No held-out accuracy gain. Labels are model judgments, not human gold, and the test does not cover omitted actions or decisions. |

## Workflow and experiment detail

Meeting Mode currently starts mic and loopback capture, checks whether streams
report active, and restarts failed or changed sources in its watchdog
(`meeting/engine.py`). It transcribes channel chunks into the durable draft
(`meeting/asr/engine.py`, `meeting/agent/scheduler.py`), optionally runs a
post-meeting clean pass (`meeting/asr/offline.py`), assigns loopback speakers
(`meeting/diarize/assign.py`), carries meeting-scoped term corrections
(`meeting/corrections.py`), and has optional citation and insight reviews
(`meeting/citation_verifier.py`, `meeting/insight_review.py`). The established
ten-meeting AMI benchmark documents **28.83%** live-draft tcWER across **8.16
audio hours**, with a 5-minute-window exact word metric
(`benchmarks/meeting_mode/README.md`). Its blanket offline clean pass was worse
at **33.66%** tcWER.

### 1. Selective ASR retry

The retry selector used draft word density, audio RMS, and hard-cut position,
without reading the manual transcript. Ordinary decode and padded/VAD-off retry
used the same locally cached CPU Whisper base model and the same preceding
draft prompt. On the development meeting IN1009, a naive word-count acceptance
gate regressed; a **hard-cut-only** replacement rule was then frozen before
testing IN1007 and IN1014. It reduced held-out errors from **262/552 to 227/552**
on **12 selected chunks (198.4 seconds)**; **one chunk regressed**. This is a
conditional selected-interval WER, not the overall meeting tcWER. The treatment
also changes padding, VAD, and timestamp clipping together, so this pilot
cannot isolate which change helped.

A distinct cached-output test replaced a five-minute Whisper draft window only
if a full-session Parakeet candidate had at least 20 and 15% more words. It
selected **1/103** windows and reduced the ten-meeting error count from
**26,025 to 25,963**. Parakeet had already decoded whole recordings; the result
is a quality proxy, not a measured runtime benefit. See
[`meeting-mode-asr-improvements-2026-10-03.md`](meeting-mode-asr-improvements-2026-10-03.md),
`benchmarks/meeting_mode/targeted_retry_cpu_slice_eval.py`,
`benchmarks/meeting_mode/targeted_retry_summary.py`, and
`benchmarks/meeting_mode/selective_fallback_eval.py`.

### 2. Capture-health probe

A fixed short-window RMS threshold was applied to **9,059** annotated
three-second speech windows from the ten AMI mixes. Injected silence and severe
attenuation were detected without healthy-speech false alerts. A voiced,
level-matched source from another meeting passed the level check every time.
This demonstrates a measurable signal beyond stream-active status for dead
capture, while showing that level alone cannot prove the correct device.
Synthetic faults do not establish the rate of real meeting failures, whether a
user would act on an alert, or recovered transcript words. See
`benchmarks/meeting_mode/capture_source_eval.py` and ignored local result
`benchmarks/meeting_mode/results/capture-source-20261003/health.json`.

### 3. Source-aware attribution

The AMI IN1009 proxy used the four official individual headset tracks and
manual speaker tags for scoring. A reference-blind, highest-energy selector
picked a headset every 0.5 seconds. It matched the manual speaker on
**622/737** scored words, compared with **316/737** for always choosing the
majority speaker. This does not compare against Meeting Mode's diarizer.

On the same meeting's first 300 seconds, CPU Moonshine Small produced
**246/741** errors on the mix. Splicing the loudest headset into a single
waveform produced **250/741** errors; independent headset transcription plus
an audio-only dominance filter produced **266/741**. A raw four-headset union
produced **1,640/741** errors from duplicated cross-talk. None of these source
strategies improved word accuracy. See
`benchmarks/meeting_mode/capture_source_eval.py` and ignored local results
`benchmarks/meeting_mode/results/capture-source-20261003/speaker_energy.json`
and `sources-moonshine-small-300s-parallel.json` in that folder.

### 4. Glossary and corrections

The glossary terms came from meeting descriptions, independently of ASR output
and reference words. The manual reference was then used to choose two
term-dense one-minute slices for scoring, so the result cannot estimate
whole-meeting performance. Both conditions used CPU Whisper base with the
same chunking; the treatment prepended the terms to the prompt. IN1005's
“PLSA” count rose **0/6 → 6/6**, and slice WER fell **39.18% → 33.33%**.
IN1012 already recognized its one “speech recognition” occurrence, while
slice WER rose **46.92% → 66.15%**. Together, word error increased.

Meeting Mode already stores human term corrections and can use corrected
spellings in an ASR prompt (`meeting/corrections.py`). AMI provides no timed
human edit events, so this pilot did not estimate the benefit or false-replace
rate of a user applying one correction during a meeting. See
[`meeting-mode-asr-improvements-2026-10-03.md`](meeting-mode-asr-improvements-2026-10-03.md)
and `benchmarks/meeting_mode/glossary_slice_eval.py`.

### 5. Evidence checks

The 26 cards came from three AMI meetings and two previously generated package
arms. A prior independent model audit of the manual transcript windows labeled
20 supported, 5 overstated, and 1 unclear. The current default retained all
26. Existing optional checks flagged all 26, including every supported card.
A score threshold chosen on two meetings retained all ten cards in the held-out
third meeting, matching default audit agreement. The run used **52 model
requests** and **110,423 input tokens**. It did not test human acceptance,
missed action/decision recall, or final-record accuracy after edits. See
[`meeting-evidence-check-2026-10-03.json`](meeting-evidence-check-2026-10-03.json),
`benchmarks/meeting_mode/evidence_check_eval.py`, and
`benchmarks/meeting_mode/evidence_check_calibration.py`.

## Next measurement gate

Re-run the top-ranked hard-cut retry with the shipping ASR model and identical
five-minute-window scoring over all ten meetings. Record total WER, per-meeting
regressions, selected-chunk counts, end-to-end latency, and cost. For capture
health and source attribution, record real mic/system-audio failures and
human-corrected source labels before claiming product accuracy gains. Keep
glossary prompts and automatic evidence gating out of a default path until a
held-out evaluation shows a net benefit on their target metric.
