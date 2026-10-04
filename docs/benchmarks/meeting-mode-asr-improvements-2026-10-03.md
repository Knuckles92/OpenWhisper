# Meeting Mode ASR improvement pilots — 2026-10-03

These pilots measure proposals #3 (selective retry) and #4 (pre-meeting
glossary) from the Meeting Mode accuracy review. All accuracy selectors avoid
using manual reference transcripts. The glossary *evaluation slices* were
chosen from the reference because they contain the prespecified target terms;
their WER does not estimate whole-meeting performance.

## Selective second-model fallback on cached full-meeting hypotheses

`venv/Scripts/python.exe -m benchmarks.meeting_mode.selective_fallback_eval`

The selector replaces a Whisper live-draft five-minute window with the cached
Parakeet v3 offline hypothesis only when the latter has at least 20 and 15%
more words. It selected 1 of 103 windows (five minutes of 8.16 audio hours)
in the ten-meeting AMI suite, without consulting the reference.

| Hypothesis | Errors / reference words | tcWER | Meeting regressions |
|---|---:|---:|---:|
| Whisper live draft | 26,025 / 90,284 | 28.83% | — |
| Selective fallback | 25,963 / 90,284 | 28.76% | 0 / 10 |
| Parakeet offline everywhere | 25,682 / 90,284 | 28.45% | not a selective policy |

The 0.07 percentage-point selective gain is small. The candidate was already
decoded from the full recording; this test does not measure selective compute,
candidate quality from short retries, or deployable latency. The cached output
and selector rule are in
`benchmarks/meeting_mode/results/selective_parakeet_fallback_eval.json`.

## Actual padded retry on suspect chunks

`venv/Scripts/python.exe -m benchmarks.meeting_mode.targeted_retry_cpu_slice_eval --meeting IN1009 --max-retries 6`

Repeat with `--meeting IN1007` and `--meeting IN1014`, using distinct `--output`
paths as in `benchmarks/meeting_mode/targeted_retry_summary.py`; then run
`venv/Scripts/python.exe -m benchmarks.meeting_mode.targeted_retry_summary`.

The selector uses the saved production draft word density, RMS, and 20-second
hard cuts to choose up to six chunks per meeting. Each selected interval is
decoded afresh by the **same locally cached Whisper base CPU int8 model** in
both conditions: ordinary beam-5 VAD-on chunk decode, and ±2-second padded
beam-5 VAD-off decode with word timestamps clipped to that interval. Both get
the same preceding 50 production draft words as prompt. This tests the retry
mechanism on selected intervals, not the production auto/turbo model or the
whole meeting. The metric is exact word-level Levenshtein error scored
separately on each selected chunk; empty-reference chunks retain insertions.

The IN1009 development run showed that a retry can hallucinate on voiced
chunks with no reference speech. Before looking at IN1007/IN1014, we froze a
hard-cut-only replacement rule. A separate word-gain rule (accept when retry
adds at least two words) was also fixed before the runs.

| Rule | IN1009 development, 144 words | IN1007 + IN1014 holdout, 552 words | Holdout regressed chunks |
|---|---:|---:|---:|
| Ordinary decode | 90 errors, 62.50% | 262 errors, 47.46% | — |
| Replace every suspect chunk | 71, 49.31% | 233, 42.21% | 2 / 12 |
| Accept only word-gaining retries | 93, 64.58% | 247, 44.75% | 1 / 12 |
| Replace only hard-cut chunks | 85, 59.03% | 227, 41.12% | 1 / 12 |

The frozen hard-cut rule improved held-out selected-interval error by 6.34
percentage points, but one of 12 chunks regressed. It selected nine hard-cut
and three sparse chunks across the holdout meetings and replaced only the
hard cuts. Ordinary decode took 35.6 seconds and padded retries took 95.1
seconds for 198.4 selected audio seconds. Another user application and a
concurrent model experiment were using this machine, so those raw times are
not clean latency estimates. The retry changes padding, VAD, and word timing
together; this pilot cannot attribute the gain to one of them. The paired
results and per-chunk errors are in
`benchmarks/meeting_mode/results/targeted_retry_cpu_summary.json` and its
three input files.

## Frozen retry rule on four complete natural meetings

`venv/Scripts/python.exe -m benchmarks.meeting_mode.targeted_retry_full_cpu_eval --meetings IN1002,IN1012,IN1001,IN1016 --max-hardcuts 20`

The four meetings were new to the selected-chunk retry pilot. The rule was
frozen before their results: identify a production 20-second hard cut with an
active audio tail, retry up to 20 cuts per meeting (lowest draft word density
first), and replace those candidate spans. The original durable live draft
was kept separately. The baseline runs `decode_meeting()`, which uses the
production 5/20-second spool cuts, 50-word draft prompt, `_transcribe_chunk`,
and SQLite segment persistence. Both arms use the same cached Whisper base
CPU int8 model directly. The product's usual GPU auto/turbo model and its
isolated worker were not tested here because another application continued
to occupy almost all GPU capacity.

| Meeting | Audio minutes | Hard cuts retried | Live tcWER | Retry tcWER | Change | Added decode seconds |
|---|---:|---:|---:|---:|---:|---:|
| IN1002 | 41.2 | 16 | 43.44% | 43.40% | −0.04 pp | 53.7 |
| IN1012 | 51.8 | 17 | 41.58% | 40.98% | −0.61 pp | 50.1 |
| IN1001 | 57.7 | 15 | 40.35% | 40.07% | −0.29 pp | 38.0 |
| IN1016 | 60.1 | 20 of 29 eligible | 35.22% | 35.21% | −0.01 pp | 54.8 |
| **Micro total** | **210.8** | **68** | **39.72%** | **39.48%** | **−0.24 pp** | **196.6** |

Across 39,455 manual reference words, errors fell from 15,672 to 15,578.
There were no meeting-level regressions, but two meetings improved by at most
0.04 percentage points. The rule retried 68 of 1,346 chunks (5.1%). Live
decode took 2,057.0 seconds; retry added 196.6 seconds, raising total runtime
factor from 0.163 to 0.178. The data and per-meeting counts are in
`benchmarks/meeting_mode/results/targeted_retry_full_cpu/summary.json`.

**Decision:** do not enable automatic transcript replacement from this pilot.
The 0.24-point gain is modest, the product auto/turbo model is untested, and
post-meeting replacement must preserve speaker pins and evidence anchors.
Keep the scripts as a repeatable test for a future run when that model can be
measured without GPU contention.

## Pre-meeting glossary prompt on target-term slices

`venv/Scripts/python.exe -m benchmarks.meeting_mode.glossary_slice_eval --meetings IN1005 --duration-s 60 --cpu-base`

Repeat for `IN1012` with a different output path. Glossary terms were fixed
from meeting descriptions in `benchmarks/meeting_mode/ami.py`, independently
of ASR output and manual words. The reference was then used to select the
densest 60-second passage containing those terms. Both conditions decode the
same audio with the cached **Whisper base CPU int8** model, production 5/20
second chunking, and a rolling 50-word context. The treatment prepends the
short glossary to each chunk prompt.

| Meeting and target | Baseline | Glossary | Exact target instances in hypothesis / reference |
|---|---:|---:|---:|
| IN1005, “PLSA” | 67 / 171 errors, 39.18% | 57 / 171, 33.33% | 0 / 6 → 6 / 6 |
| IN1012, “speech recognition” | 61 / 130, 46.92% | 86 / 130, 66.15% | 1 / 1 → 1 / 1 |
| Combined | 128 / 301, 42.52% | 143 / 301, 47.51% | 1 / 7 → 7 / 7 |

The glossary improved exact target-term frequency but worsened total word
error by 4.98 percentage points across these two selected slices. IN1012 lost
more ordinary words (35 deletions baseline versus 63 with the glossary).
Target counts are exact phrase frequencies in each scored passage, not
time-aligned matches to individual mentions.
Observed decode time was 17.0 seconds baseline and 29.5 seconds glossary,
with variable machine load and a fixed baseline-first order; this is not a
reliable latency comparison. The prespecified IN1007 glossary had no exact
reference occurrence, so it gave no informative target-term slice. The
results are in `benchmarks/meeting_mode/results/glossary_slice_eval.json` and
`benchmarks/meeting_mode/results/glossary_slice_eval_IN1012_cpu.json`.

These pilots did not measure the separate *scoped correction* part of #4. AMI
has no timed human edit events to simulate that intervention faithfully.
