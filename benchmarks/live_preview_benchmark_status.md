# Live preview benchmark: run record and outcome

## What was requested
Benchmark live preview backends for both speed and accuracy, with a focus on:

- the current tiny.en preview path
- Parakeet
- all local streaming-capable backends (including Nemotron)

Then pick the default engine for the dictation live preview.

## Outcome (September 4, 2026)
The dictation preview now shares the loaded **Parakeet** or **Nemotron** engine
when one of them is the dictation backend (`config.STREAMING_PREVIEW_BACKENDS`,
`services/runtime/streaming.py`). No second model is loaded for the preview.
Local Whisper keeps its dedicated tiny.en preview. Qwen and Moonshine have no
dictation preview. The user-facing summary and the decision are in
[docs/local-asr.md](../docs/local-asr.md#live-preview-check); the trimmed
measured results are in
[docs/benchmarks/live-preview-windows-2026-09-04.json](../docs/benchmarks/live-preview-windows-2026-09-04.json).

The measured production preview path was also exercised live: the threaded
`StreamingTranscriber` worker fed 1024-frame recorder callbacks to the real
Parakeet CUDA worker under the `--test-root` layout, produced its first window
in about 1.3 s of wall time, and the final pass then ran on the same worker in
0.11 s.

## Harness
- `benchmarks/live_preview.py` (live preview benchmark harness)
- `scripts/prepare_live_preview_corpus.py`
- `scripts/report_live_preview.py`
- `tests/test_live_preview_benchmark.py`
- `benchmarks/LIVE_PREVIEW.md`

Last full command run:

```powershell
python -m benchmarks.live_preview .tmp/live-preview/corpus/manifest.json --test-root .tmp/local-asr --repeats 1 --models tiny.en,parakeet-v3,nemotron-3.5,moonshine-small,moonshine-medium,qwen-0.6b,qwen-1.7b --output .tmp/live-preview/results.json
python scripts/report_live_preview.py .tmp/live-preview/results.json --output .tmp/live-preview/progress
```

Both paths use the production `StreamingTranscriber` cadence (3.0 s windows,
0.75 s overlap, `beam_size=1`, `vad_filter=False`). `window` calls the
production `_process_incremental_chunk` with each engine's production decoder;
`native` additionally exercises `stream_audio` where the catalog advertises it.

## Results saved in `.tmp/live-preview/results.json` (43 clips, one pass, RTX 2060)

| Model / path | Device | Update p50 / p95 (ms) | First text (s) | Live WER | Drained WER | RTF |
|---|---|---:|---:|---:|---:|---:|
| tiny.en / window | cuda | 112 / 219 | 3.11 | 52.8% | 47.2% | 0.049 |
| parakeet-v3 / window | cuda | 52 / 62 | 3.06 | 53.1% | 44.4% | 0.018 |
| nemotron-3.5 / window | cuda | 60 / 76 | 3.07 | 49.3% | 40.2% | 0.021 |
| nemotron-3.5 / native | cuda | 62 / 75 | 1.60 | 30.1% | 26.2% | 0.084 |

| Model / path | Clean live / drained | Noisy live / drained | AMI live / drained | AMI checkpoint |
|---|---:|---:|---:|---:|
| tiny.en / window | 45.9% / 41.4% | 59.7% / 55.5% | 52.8% / 46.5% | 57.6% |
| parakeet-v3 / window | 46.2% / 33.4% | 52.4% / 38.6% | 55.2% / 49.3% | 59.7% |
| nemotron-3.5 / window | 36.2% / 23.8% | 49.7% / 37.9% | 52.9% / 45.6% | 59.0% |
| nemotron-3.5 / native | 17.9% / 9.7% | 29.0% / 19.0% | 33.9% / 33.1% | 41.9% |

No engine missed its input cadence. Clips with no live text before EOF: 7, 8,
7, and 1 of 43 respectively. Silence produced no insertions anywhere.

Moonshine Small window mode completed in the terminal (live WER 49.3%, drained
42.2%, median update 547 ms, RTF 0.187, 7 clips without live text) but is not
in the saved JSON because the model's row failed before it was written.

## Reading the numbers
- **Nemotron native streaming is the clear accuracy winner** and shows text
  twice as early (1.6 s against 3.1 s). It is not wired into dictation yet:
  the current `StreamingTranscriber` is windowed, and `stream_audio` would need
  a replacing-text adapter like the meeting preview's.
- **Parakeet in window mode is not more accurate than tiny.en live** (53.1%
  against 52.8%), but it is better after the flush (44.4% against 47.2%),
  decodes a window in under half the time, and is already resident on the
  Windows x64 default install, so the preview costs no extra memory or load.
  That, not raw accuracy, is why it is the default preview engine.
- Nemotron window mode is a little better than Parakeet on every accuracy
  column and is included in the shared-engine set for users who dictate on it.
- Window WER is high for every engine because the unchanged append-only
  assembly repeats words at each seam and because unshown tails count as
  deletions at EOF. Compare paths, not absolute quality.
- `stop_streaming` sets a stop flag that makes the flush window's segments be
  discarded, so the tail of a recording never reaches the preview text in the
  app (the benchmark's drained numbers decode that tail directly). This only
  affects the empty-transcript fallback text and was left as is.

## Failure encountered during the full run
The run died during `moonshine-small native` with worker teardown errors:

- `RuntimeError: Speech worker stopped. Reload the engine.`
- `OSError: [Errno 22] Invalid argument`
- Traceback in `backend.cleanup()` / `process.close()` for the speech process pipes,
  after `moonshine-small native: clip 31/43`.

Not completed: `moonshine-small native`, `moonshine-medium` (both modes),
`qwen-0.6b` / `qwen-1.7b` (both modes).

## Remaining work
1. Re-run the incomplete models in reduced scope:
   - `--models moonshine-small --mode native --repeats 1`
   - `--models moonshine-medium --repeats 1`
   - `--models qwen-0.6b,qwen-1.7b --mode window --repeats 1`
2. If native teardown stays flaky, harden `transcriber/optional_backend.py`
   and `services/local_asr/process.py` so a cleanup error cannot abort the
   benchmark loop.
3. Consider a native-streaming dictation preview for Nemotron, which is where
   the measured accuracy and first-text gains are.
