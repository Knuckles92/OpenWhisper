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
scored. Runtime factor (wall-clock seconds / audio seconds) is recorded beside
quality so an accuracy change cannot silently make Meeting Mode fall behind.
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
