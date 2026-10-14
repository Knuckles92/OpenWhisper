# Qwen silence handling and file batching

Local investigation of OpenWhisper issues #49 and #48 on October 12, 2026, and
the batching that shipped for #48 on October 13 ([Batching in the app](#batching-in-the-app-48)).

## Silence fix (#49)

Only Qwen's parent-side transcription requests check for speech. The check uses
the Silero CPU detector already shipped with faster-whisper. It skips a complete
window only when the detector returns no speech; a retained window is passed to
Qwen byte for byte. There is no RMS threshold, audio trimming, timestamp shift,
or minimum speech duration. Uploads, dictation, meeting decoding, preview, and
local remote-host recognition reach the same request boundary. Other engines
do not call this detector. The worker's isolated import path is unchanged.

A detector error logs a warning and continues the original decode. Cancellation
and unloaded-engine checks still apply, including during detection.

Validation used the previously downloaded LibriSpeech/AMI corpus, with synthetic
quiet variants and noise. All 68 speech clips were retained at their original
levels and when rescaled to -45 and -55 dBFS RMS. Three 25-second synthetic noise
windows approximated the issue's RMS/peak pairs (-67/-43, -67/-53, -63/-33 dBFS).
All passed the old peak gate and all were rejected by the speech detector.

On the real CUDA worker, both Qwen 0.6B and 1.7B returned `The.` on each of those
three noise windows before filtering, and empty text through the fixed app
request path. Twelve speech cases per model, including quiet speech, sparse
speech, and an isolated word at the middle/end of a window, retained identical
text. Parakeet v3 and Nemotron 3.5 retained identical direct-worker/app output
across the same 15 cases each. Whisper Turbo transcribed two labeled clips on
CUDA with zero normalized word errors using its existing VAD settings.

The 62 real-engine comparisons, speech-presence checks, and regression count
are saved in [results/silence-2026-10-12.json](results/silence-2026-10-12.json).

This does not prevent hallucinations inside windows that contain detected
speech. The reporter's private recording was unavailable; the synthetic cases
reproduce the same failure class, not the original recording's exact samples.

## Batching measurements (#48)

Hardware: Windows, RTX 2060, 6 GiB VRAM, NVIDIA driver 610.74. Runtime:
`asr-qwen` 2026-10-08.1, PyTorch 2.7.1+cu128. Both model revisions match
`services/local_asr/models.json`; the downloaded 1.7B files were verified against
every pinned file size and SHA-256. CUDA uses float16, CPU uses float32, and the
worker's 2048-token limit/check is reproduced.

The GPU corpus is 335.395 seconds, 37 production-cut windows: 26 clean
LibriSpeech utterances, four AMI meeting excerpts, and a 75-second concatenated
read-speech case. It has 950 reference words. Each setting ran three times for
0.6B and twice for 1.7B. The order was reversed on alternate runs. Model loading
and one warmup decode are excluded. These are SDK decode times on app-identical
inputs; they exclude file conversion, app IPC, and the new speech check.

| Model | Windows/call | Median seconds | Speedup | Peak allocated MiB | Peak reserved MiB | Word edits vs serial |
|---|---:|---:|---:|---:|---:|---:|
| 0.6B | 1 | 33.40 | 1.00x | 1733 | 1976 | 0 |
| 0.6B | 2 | 24.75 | 1.35x | 1834 | 1976 | 0 |
| 0.6B | 4 | 16.04 | 2.08x | 2162 | 2968 | 3 |
| 0.6B | 8 | 11.38 | 2.93x | 2818 | 3770 | 0 |
| 1.7B | 1 | 35.52 | 1.00x | 4129 | 4558 | 0 |
| 1.7B | 2 | 26.57 | 1.34x | 4234 | 4546 | 0 |
| 1.7B | 4 | 19.31 | 1.84x | 4565 | 5014 | 0 |
| 1.7B | 8 | 19.82 | 1.79x | 5228 | 5904 | 0 |

The 0.6B batch-four differences occurred in one window, consistently across all
three runs. Against the human reference, that reduced errors from 121 to 119
(WER 12.74% to 12.53%); matching serial output is not an accuracy measure.
The other 0.6B settings had 121 errors, and every 1.7B setting had 112 errors
(11.79% WER). No batch failed in these runs. PyTorch memory figures exclude
other applications, driver allocations, and Windows shared GPU memory.

A smaller CPU check used 41.97 seconds, eight clean speech clips, and two runs
per setting. 0.6B medians were 18.71 seconds serial, 13.14 at batch two, and 11.30
at batch four (1.66x). All had identical words and three errors against 106
reference words. This is a limited CPU result, not a long-file qualification.

The exact measurements, repeats, reference scores and memory values are in
[results/rtx2060-2026-10-12.json](results/rtx2060-2026-10-12.json).

## Batching in the app (#48)

Qwen on this computer's CUDA GPU decodes a file's windows several per worker
request: uploads and the final pass of dictations longer than one window. Live
preview, meetings, the CPU, Apple Silicon and remote engines still send one
window per request.

- `LocalSpeechBackend.transcribe_windows` reads windows only as far as the next
  request needs and skips those without speech (#49) before they take a place.
  It sends up to `MAX_BATCH_SIZE` (16) windows per request, in source order,
  always to the worker it started on, so a reload or cancel mid-file fails the
  transcription instead of joining two models' text.
- In the worker, `qwen_batch.transcribe_batch` gives each generate call as many
  of them as fit: the GPU's free memory plus the worker's own unused pool, less
  512 MiB, at 256 MiB per window after the first. Windows drivers page an
  overcommitted GPU out to system memory instead of failing, so this estimate
  has to hold without an out-of-memory error to correct it. The model is loaded
  with `max_inference_batch_size=-1`, so each call is exactly one batch.
- CUDA out-of-memory halves the limit for the rest of the file. Any other
  failure of a multi-window call decodes that call's windows one at a time, as
  before batching; a window that reaches the output limit fails its padded
  batch, and alone raises the error it always did.
- Each request ends with `torch.cuda.empty_cache()`, so the worker goes back to
  the model's own memory between requests instead of keeping its peak.

Measured on the RTX 2060 through the app's backend and real worker
([bench_upload.py](bench_upload.py), median of two runs each way): a 12.2-minute
file of 61 LibriSpeech and AMI clips that opens with 25 s of near-silence and
pauses for 40 s halfway.

| Model | One window per request | Batched | Speedup | Peak GPU used, one window → batched | WER, both ways | Words that differ |
|---|---:|---:|---:|---:|---:|---:|
| 0.6B | 69.3 s | 17.7 s | 3.9x | 3,524 → 5,396 MiB | 16.08% | 1 of 1,723 |
| 1.7B | 75.2 s | 44.0 s | 1.7x | 5,792 → 5,720 MiB | 14.76% | 1 of 1,741 |

With 1.7B loaded only 330 MiB of the card was free. The gain there came from
fitting two windows per call into memory the worker had already reserved, so
batching used no more of the card than one window at a time. After every
batched run the card went back to its usage after loading. A one-window file
returned identical text both ways, and a cancel during a batch stopped the
worker at once, after which a reload transcribed identically.

A forced CUDA out-of-memory error (0.6B, worker allocator capped at the model
plus 700 MiB, preflight told each window costs 1 MiB) took the file's first
request from 16 windows per call down to 2, and the rest of the file stayed at
2: the same words as one window per request, in 44 s.

The reporter's 1 h 51 min call on an RTX 5080 went from 681 s to 67 s at 16
windows per call outside the app; that still has to be confirmed in a build.
The full measurements are in
[results/upload-rtx2060-2026-10-13.json](results/upload-rtx2060-2026-10-13.json).
In `forced_out_of_memory.requests`, each entry is the windows sent, the limit
sent and the limit returned.

## Reproducing the batch experiment

Use the app environment to cut audio, then the isolated Qwen runtime to decode.
No installed packages, user settings, or existing model caches are modified.
A corpus manifest contains `clips` with `id`, `group`, `audio_path` (relative to
the manifest), and `reference`. Source metadata is retained if present.

```powershell
.venv/Scripts/python.exe benchmarks/qwen_asr/prepare.py `
  --corpus-manifest .tmp/linux-asr-default-2026-10-09/corpus/manifest.json `
  --output-dir .tmp/qwen-batch/windows --groups librispeech_clean `
  --clip-ids IN1001 IN1002 IN1005 IN1007 long-read-speech

$qwenPython = 'C:/Users/Big D/AppData/Local/OpenWhisper/components/asr-qwen/python.exe'
$qwenModel = 'C:/Users/Big D/AppData/Local/OpenWhisper/speech-models/qwen-0.6b'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& $qwenPython -B -u benchmarks/qwen_asr/bench_batch.py `
  --model-path $qwenModel --manifest .tmp/qwen-batch/windows/manifest.json `
  --output .tmp/qwen-batch/cuda.json --device cuda --batch-sizes 1 2 4 8 --runs 3

.venv/Scripts/python.exe benchmarks/qwen_asr/summarize.py `
  .tmp/qwen-batch/cuda.json --output .tmp/qwen-batch/summary.json
```

The optional `--groups`/`--clip-ids` filters use a union. Without them, preparation
includes every clip, including non-speech. The raw benchmark JSON retains every
window's provenance and text, loading/warmup durations, call times, and failures.

The in-app comparison runs on the app's Python with the installed `asr-qwen`
runtime. `--model-path` loads a model that isn't in the speech-model cache:

```powershell
.venv/Scripts/python.exe benchmarks/qwen_asr/bench_upload.py `
  --corpus-manifest .tmp/linux-asr-default-2026-10-09/corpus/manifest.json `
  --work .tmp/qwen-upload --model qwen-1.7b --model-path .tmp/qwen49-48/qwen-1.7b
```
