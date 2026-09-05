# Meeting Mode dogfood benchmark

This benchmark runs the production Meeting Mode chunking, faster-whisper
decode, rolling revision, and SQLite segment persistence paths against ten
complete, naturally occurring AMI research meetings with manual word-level
transcripts.

The curated meetings are deliberately difficult: multiple speakers,
interruptions, overlap, strong accents, conversational disfluencies, and
specialist speech-recognition vocabulary. This is a product dogfood suite,
not training data and not a fast unit test.

## Corpus and license

The [AMI Meeting Corpus](https://groups.inf.ed.ac.uk/ami/corpus/) contains
about 100 hours of recorded meetings. Audio and manual annotations are
released by the AMI Consortium / University of Edinburgh under CC BY 4.0.
The benchmark downloads the official manual annotation release 1.6.2 and
official headset-mix WAV files; downloaded data stays under the ignored
`data/` directory.

The default suite uses `IN1001`, `IN1002`, `IN1005`, `IN1007`, `IN1008`,
`IN1009`, `IN1012`, `IN1013`, `IN1014`, and `IN1016`. The AMI corpus describes
these as non-scenario meetings among research colleagues.

## Run

From the repository root in the required Windows virtual environment:

```bash
./venv/Scripts/python.exe -m benchmarks.meeting_mode.run --download --model auto
```

Run a one-meeting pilot:

```bash
./venv/Scripts/python.exe -m benchmarks.meeting_mode.run \
  --download --meetings IN1009 --model auto
```

Results are resumable per meeting and written below the ignored `results/`
directory. Pass `--force` to discard cached results for selected meetings.
The default run pins no language; pass `--language en` when the language is
known, as a user can in Meeting settings. Use `--draft-prompt-words 0` only to
reproduce the context-free ablation. Rolling rewrites are off by default;
`--enable-revisions` opts into that experimental research path.

## Metric

The primary metric is exact word-level Levenshtein error scored in fixed
five-minute meeting-time windows (`tcWER-300s`). It reports substitutions,
deletions, and insertions separately. Windowing prevents one early omission
from shifting the alignment for the remainder of an hour and avoids arbitrary
whole-meeting alignment across overlapping speakers. Case and punctuation are
ignored; fillers and spoken lexical content are retained.

Both the initial live draft and the final rolling-revised transcript are
scored. When `--offline-pass` is on (the default), a post-meeting clean
re-decode of the continuous session audio is scored beside them. Runtime
factor (wall-clock seconds / audio seconds) is recorded for the live path,
the offline pass, and the combined end-of-meeting cost.
The report also records the share of human reference words concurrent with a
different speaker; these remain in strict WER, but expose the difficulty of
recognizing overlapping voices from one mono mix.

The exceptional-quality gate requires at least 10 meetings and 8 audio hours,
micro tcWER at or below 30%, every meeting at or below 35%, and runtime factor
at or below 0.50. These checks are emitted in `summary.json`; the much lower
observed RTF leaves headroom for ordinary desktop contention.

## Validated production profile

The August 2026 dogfood run used all ten curated meetings (8.16 audio hours),
the GPU `auto` model, production 5/20-second chunking, a bounded 50-word prompt
tail, and no rolling rewrites. The default automatic-language profile scored
28.83% micro tcWER at a 0.068 runtime factor; known English scored 28.86% at
0.040. The same pinned-English profile without the prompt tail scored 31.52%;
context improved every meeting and reduced micro tcWER by 2.66 absolute points
(8.44% relative) without a throughput penalty. Because 34.04% of manual
reference words overlap another speaker, this is intentionally much harsher
than clean single-speaker dictation.

## Offline clean pass (not the product final)

The same ten meetings with `--offline-pass` scored **33.66%** micro tcWER on
the continuous-session re-decode versus **28.83%** live draft (+4.83 absolute,
+16.8% relative). Offline lost on every meeting; extra deletions were the
main gap (25.13% vs 21.45%). Combined RTF was 0.114. The exceptional-quality
gate fails for offline as the product final (micro above 30%, worst meeting
39.89% on IN1007). Live draft remains the durable transcript. Session WAVs
are still captured so a later cut/decode can be scored without changing the
live path.

## Optional local backends on the same ten meetings

The September 2026 runs decoded the same ten meetings with the optional
Windows x64 backends through the identical chunking and offline-pass profile,
pinned to `--language en` (the NeMo-Speech.cpp CUDA runtime on an RTX 2060;
Moonshine is CPU-only). None of these backends accepts a prompt or a beam
size, so their drafts are context-free decodes.

| Path | Whisper `auto` | Parakeet v3 | Nemotron 3.5 |
|---|---:|---:|---:|
| Live draft tcWER | 28.83% | 29.07% | 33.85% |
| Offline pass tcWER | 33.66% | 28.45% | 33.74% |
| Live draft, fillers removed | 24.98% | 27.16% | 30.58% |
| Offline pass, fillers removed | 29.46% | 26.38% | 30.38% |
| Live RTF | 0.073 | 0.010 | 0.014 |
| Combined RTF | 0.114 | 0.022 | 0.030 |
| Worst meeting (offline) | 39.89% | 35.69% | 39.66% |

Whisper and Parakeet tie on the strict live draft only because Parakeet
transcribes most `um` / `uh` fillers that the AMI reference keeps, while
Whisper suppresses most of them; with fillers removed from both sides
Whisper's draft still leads by about two points. The offline pass reverses
the order: Parakeet's clean re-decode improves on its own draft on nine of
ten meetings, so the transcript a user keeps after End is better with
Parakeet by five points strict or three points on lexical content, at one
fifth of the compute. Parakeet's draft deficit against Whisper grows with the
share of reference words spoken over another speaker; IN1012 (48.5%
overlap) and IN1013 (45.3%) are the two most overlapped meetings and the only
ones where Parakeet's offline pass is not clearly ahead. Parakeet passes
every gate check except the worst meeting (IN1012, 35.69% against the 35%
ceiling); Whisper's and Nemotron's offline products fail both the micro and
the worst-meeting checks. Nemotron is about five points behind Parakeet on
every path and offers no offline gain.

Moonshine Small and Medium are CPU-only, so they ran the `IN1009,IN1005,IN1007`
slice (1.8 audio hours). On that slice the offline pass scored 31.15% (Small)
and 31.98% (Medium) against 27.67% for Parakeet, 32.79% for Nemotron and
33.75% for Whisper, at live RTF 0.10 and 0.15 on this CPU. Medium did not
beat Small. Exact counts for every run are in
[the recorded evidence](../../docs/benchmarks/meeting-mode-parakeet-vs-whisper-2026-09-04.json).

The LLM-judged product eval on the Parakeet run split the default slice:
the clean End package won IN1007, the unpolished draft package won IN1005,
and the IN1009 judge call returned an empty body twice and is recorded as
unjudged. The deterministic signal behind the IN1005 loss is polish volume:
Parakeet's offline transcript arrives as short, punctuated segments (647 on
IN1005 against Whisper's 234), and the polish agent rewrote 34% of them
where it rewrites about 6% of Whisper's, and the judge rated the polished
excerpts less faithful than the draft. Reducing polish aggressiveness or
merging optional-backend segments before polish is the open follow-up.

ASR word error is not the product question. To compare the package a user
keeps after End (topic, summary, cards, live notes, questions, readable
transcript), the product eval first *simulates the live meeting*: rolling
copilot checkpoints and the dedicated note-taker pass replay the draft
transcript over meeting-time windows, accumulating the dashboard exactly the
way the product does. Both End paths then start from that same live state —
legacy runs consolidation directly on the draft; clean swaps in the offline
re-decode (remapping evidence anchors, stripping only items whose anchors
died, mirroring the engine), polishes, and consolidates. An LLM judge scores
both packages against the AMI reference:

```bash
./venv/Scripts/python.exe -m benchmarks.meeting_mode.product_eval
```

Default slice is `IN1009,IN1005,IN1007` (short / best-draft / worst-offline).
That needs the same OpenRouter key Meeting Mode uses. Results land under
`results/.../product_eval/`. `--live-window-s` controls the simulated
checkpoint cadence (default 120 meeting-seconds); `--no-live` reproduces the
pre-notes ablation where consolidation starts from an empty dashboard. Judge
scores are stochastic — single-meeting flips between runs are normal; look at
the per-op `op_stats` in each result for the deterministic signals (applied
vs rejected ops and their reasons).
