# Preview optimizations — September 11, 2026

## Completed plan

1. Inspect the benchmark findings against current production defaults and preview code.
2. Measure the existing and revised overlap handling on installed real speech.
3. Improve preview buffering and stop/cancel behavior, with lifecycle regression tests.
4. Validate actual decoder workers and the Python suite; record results and default decisions.

This follows the broader [testing and benchmark repairs](testing-benchmark-repairs.md).
The counts and identities for this follow-up are in
[the measurement summary](benchmarks/preview-optimizations-2026-09-11.json).

## Implemented

- Window previews remove a matching phrase at the overlap seam. Matching is bounded
  to the overlap duration (six words at the current 0.75-second overlap), ignores
  case and punctuation, and requires at least two matching words plus a new word.
  Single-word repetitions and wholly repeated chunks are preserved.
- The window preview queue now holds approximately three seconds of normal recorder
  blocks instead of ten blocks (about 0.23 seconds at 44.1 kHz). It remains bounded
  and drops preview audio if exhausted; the saved recording is independent.
- Stop requests recorder shutdown immediately. Post-roll capture and preview drain
  happen on the recording executor, including incomplete windows and lazy decoder
  results already in flight. Preview updates are muted during this drain.
- Cancellation discards preview work without waiting for inference. Both native
  and window workers suppress late output after cancellation or the existing
  five-second stop deadline. A still-running old worker prevents a replacement
  preview from sharing its state; recording/final transcription remain available.
- Historical benchmark documentation now distinguishes the older window-only
  implementation from the already-supported native Nemotron preview.

## Fresh preview measurements

The corpus contains the center 30 seconds of each of ten installed AMI meetings,
plus six seconds of silence: 306 seconds total and 1,007 reference words.
References select words by midpoint within each excerpt. These are short English
conversation excerpts, not a new full-meeting accuracy benchmark.

Hardware: RTX 2060, 6 GiB, Windows 11, Python 3.12.10. Both engines used CUDA.
Whisper used tiny.en float16; Parakeet used the installed pinned v3 q8_0 artifact.
Each model was warmed before two paired passes, with before/after order reversed
on the second pass. Window size remained three seconds with 0.75-second overlap.

| Preview engine | Live WER before → after | Drained preview WER before → after |
| --- | --- | --- |
| Whisper tiny.en | 53.18% → 49.95% | 46.87% → 43.45% |
| Parakeet v3 | 55.21% → 52.23% | 49.26% → 46.08% |

Counts are pooled across the two repeat measurements of the same corpus. The
lower error rate comes from improved text joining, not a change to the model.
Insertion counts fell, but deletion counts increased: text-only overlap matching
remains approximate and can remove legitimate repeated speech. This tradeoff is
why matching is bounded and ambiguous short/whole-chunk repetitions are retained.

Per-pass median update service times were 59.8–66.7 ms before and 59.9–60.8 ms
after for Whisper; 56.4–58.2 ms before and 57.9–62.4 ms after for Parakeet. No
update missed its three-second input cadence. These runs establish an accuracy
improvement, not a throughput improvement. The GPU snapshot showed other desktop
activity, so small timing differences should not drive default changes.

Replay models when text becomes visible using measured decoder service time.
It does not run the recorder queue, render Qt, or measure hotkey-to-paint latency.
“Drained” is preview text after all windows finish, not the separate final ASR.
The replay therefore does not quantify the benefit of the new stop/buffer logic.
Both variants produced empty text for the silence clip.

## Final ASR comparison and defaults

A separate whole-file production transcription pass used the same corpus,
with the first clip decoded once for warmup.

| Final engine | Word errors / reference words | WER | Total decode time |
| --- | --- | --- | --- |
| Whisper Turbo, CUDA float16 | 390 / 1,007 | 38.73% | 18.08 s |
| Parakeet v3, CUDA q8_0 | 244 / 1,007 | 24.23% | 3.27 s |

Both produced no insertions on silence. This one-pass timing is descriptive; it
is not a repeated latency estimate or whole-application speedup.

Parakeet remains a strong option for supported-language GPU workloads. The
global model default is retained: ten English excerpts on one NVIDIA device
do not establish behavior across languages, CPU hosts, long meetings, or other
recording conditions. Existing user model selections are preserved.

The queue duration is the default changed by this work. The three-second window,
0.75-second overlap, existing native Nemotron path, 50-word meeting context, and
disabled End re-decode remain in place. Earlier meeting measurements already
supported retaining context and leaving End re-decode off; this preview corpus
does not justify changing those meeting policies.

## Validation and reproduction

Final Python suite: 2,471 passed, 40 skipped, and 391 subtests passed in 155.39
seconds. Ruff F checks passed for all Python files changed in this follow-up;
git diff --check passed. The dashboard and sidecars were unchanged by this
follow-up; their earlier validation is in the repair report.

A real threaded 2.7-second AMI excerpt returned nonempty text for Whisper tiny.en,
Parakeet v3, and native Nemotron 3.5. All three worker threads finished; Nemotron
closed its session. This exercised a window shorter than the three-second decode
threshold and verified actual tail flushing. It is a functional smoke check,
not an end-to-end latency benchmark.

Regression coverage includes phrase boundaries, intentional repetition, zero
overlap, short recordings, in-flight generators, queued tail audio, decoder
stalls, cancellation, stop deadlines, session closure, restart isolation, muted
post-stop updates, callback detachment, and executor-based stop handling.

Local detailed artifacts are under .tmp/preview-optimization:
- measure.py, before_streaming_transcriber.py, and corpus/manifest.json reproduce
  the paired comparison; results.json includes per-clip transcripts and times.
- asr-corpus.json and asr-corpus.log contain whole-file results.
- smoke.py and smoke.json contain the real worker checks.
- full-tests.log contains final suite output.

After activating the venv, rerun the paired measurement with
python .tmp/preview-optimization/measure.py.
The whole-file command is
python scripts/benchmark_local_asr_corpus.py .tmp/preview-optimization/corpus/manifest.json --models turbo,parakeet-v3 --output .tmp/preview-optimization/asr-corpus.json.

The local .tmp files are not committed. The repository measurement summary
preserves the aggregate counts, clip hashes, source provenance, model identities,
and hardware metadata. The native lifecycle changes were made after the paired
window replay; their validation is the real worker check and regression suite.
