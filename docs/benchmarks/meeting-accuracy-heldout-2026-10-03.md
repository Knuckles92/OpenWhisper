# Real-meeting follow-up: source cues, glossary, and corrections

All audio here comes from natural AMI research meetings with timed human word
annotations. These are limited probes of three accuracy ideas, not measured
end-to-end Meeting Mode gains.

## Distinct audio sources for speaker attribution (#2)

The [source-cue script](../../benchmarks/meeting_mode/speaker_source_cue_eval.py)
used four individual AMI headsets. It learned the speaker-to-headset mapping
from the first quarter of IN1009 and froze it for the separate IN1005 meeting.
The dominant headset matched the human speaker at 7,467 of 8,358 eligible
word midpoints (89.33%). Accuracy was 95.43% for isolated words (6,855) and
61.48% for words overlapping another speaker (1,503).

A more product-like [two-source proxy](../../benchmarks/meeting_mode/two_source_proxy_eval.py)
treated headset 0 as the local mic and the mean of the other three headset
tracks as remote audio. A +0.5 dB mic/remote threshold was selected from the
first quarter of IN1009, then frozen for all of IN1005. On 8,358 held-out
word midpoints, accuracy was 93.07%; local-speaker recall was 80.03%, remote
recall 94.08%, and balanced accuracy 87.06%. For isolated words balanced
accuracy was 96.92%; for overlap it fell to 73.41%, with local-speaker recall
60.07%.

These are source-identity proxies, not diarization or ASR comparisons. Four
close-talking headsets and their synthetic mix differ from a consumer mic and
OS loopback. The current mono diarizer was not run as a matched baseline.
Source-aware attribution merits a focused product prototype and real dual-
stream capture evaluation; these numbers do not justify claiming a shipped
speaker-accuracy gain.

## Pre-meeting glossary (#4)

The prior [target-term pilot](meeting-mode-asr-improvements-2026-10-03.md)
selected term-dense slices with human annotations after fixing the glossary
terms. Exact target counts rose from 1/7 to 7/7, but WER worsened from
42.52% to 47.51% across 301 reference words.

The new [fixed-time script](../../benchmarks/meeting_mode/glossary_fixed_time_eval.py)
decoded 90-second windows at 25% and 75% of natural IN1013 and IN1014
meetings, without consulting the reference to choose windows or glossary
terms. None of the predeclared target phrases appeared in the 1,211 scored
reference words, so this holdout cannot estimate term recall. Under automatic
language selection, aggregate WER was 53.51% without the prompt and 42.20%
with it. This apparent gain was driven by one IN1014 baseline decode that
produced mostly punctuation and scored 100% WER. With English pinned on the
same two IN1014 clips, baseline WER was 159/577 = 27.56% and glossary WER
was 188/577 = 32.58%, a 5.03-point regression. On two fixed-time IN1005
clips, where the development glossary had already been studied, pinned-
English WER improved from 192/526 = 36.50% to 179/526 = 34.03%; exact target
count was 1/1 in both arms. These small, inconsistent results do not support
unconditional glossary prompting. The CPU base model is not the product's
auto/turbo model.

## One-occurrence versus meeting-wide term corrections (#4)

The [correction proxy](../../benchmarks/meeting_mode/scoped_correction_proxy_eval.py)
used real cached Whisper drafts and AMI human transcripts for IN1005, IN1009,
and IN1012. It found 17 high-confidence one-word substitution pairs in the
first quarter of each meeting, then applied each as a separate hypothetical
meeting-wide rule to later speech. Two rules improved later 15-second-window
word errors, 13 worsened them, and two tied. For example, changing every
later `the` to `there` changed 286 occurrences and added 201 errors; changing
`labeling` to `labelling` changed two and removed two errors. This is an
oracle-derived proxy, not a study of real user edits. It demonstrates the
collateral-edit risk in the current global replacement behavior and supports
offering a one-occurrence scope by default, with an explicit meeting-wide
choice for genuine recurring terms. It does not quantify the net gain users
would realize.

## Claim evidence checking (#5)

The separate [evidence-check report](meeting-evidence-check-2026-10-03.json)
compared 26 generated action and decision cards from three natural AMI
meetings with a prior independent model audit of human transcript windows.
The production-strength reviewer flagged all 26 cards, including 20 audited
as supported. A threshold chosen on two meetings retained all ten cards in
the held-out third meeting, including both overstated cards. It matched the
all-pass baseline's 80% agreement with that audit. These model labels are
not human adjudication, and omitted decisions/actions were not evaluated.
Automatic final-record gating has no demonstrated gain and should remain
unshipped pending human-labeled precision and coverage testing.
