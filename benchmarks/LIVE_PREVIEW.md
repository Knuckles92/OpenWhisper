# Live transcription preview benchmark

Compare Whisper `tiny.en` (the current dictation preview) with all six optional
local model choices: Parakeet v3, Qwen3-ASR 0.6B and 1.7B, Nemotron 3.5, and
Moonshine Small and Medium. This benchmark does not change the app's backend
selection or install/download anything. Cloud transcription is outside this
local preview comparison.

The September 4, 2026 results and the decision they informed (the dictation
preview shares the loaded Parakeet or Nemotron engine) are summarized in
[the local speech guide](../docs/local-asr.md#live-preview-check); the run
record is [live_preview_benchmark_status.md](live_preview_benchmark_status.md).

## Run

Activate the project virtual environment first:

```powershell
. ./venv/Scripts/Activate.ps1
python scripts/prepare_live_preview_corpus.py .tmp/local-asr/corpus/manifest.json
python -m benchmarks.live_preview .tmp/live-preview/corpus/manifest.json --output .tmp/live-preview/results.json
```

The preparation script reuses the clean/noisy manifest described in
[the local speech guide](../docs/local-asr.md) and the downloaded
[AMI meeting corpus](meeting_mode/README.md). It selects the centered 30 seconds
of each of the ten curated meetings, without consulting model outputs. Reference
words belong to an excerpt when their midpoint is inside it. It adds six seconds
of digital silence. All generated audio and detailed results stay under `.tmp/`.
The corpus contains 16 clean utterances, their 16 noisy counterparts, ten
conversational excerpts, and silence. Clean/noisy speech is one LibriSpeech
speaker; the AMI excerpts contain accents, disfluencies, and overlapping speakers.
Both corpora use CC BY 4.0. This is a small product benchmark, not a leaderboard.

For an existing development installation layout, append
`--test-root .tmp/local-asr`. This resolves optional weights from
`test-app/speech-models` and runtimes from `install-validated` for this process
only. SDK imports remain inside the production isolated workers. Otherwise the
ordinary application caches are used. Whisper always uses its ordinary local
cache. Missing models, device fallback, or inference failures are reported as
errors and return a nonzero process status.

Useful options: `--models tiny.en,parakeet-v3,nemotron-3.5`, `--mode window`,
`--mode native`, `--device cpu`, `--repeats 3`, `--limit 2` (smoke check only),
`--chunk 1.5`, and `--overlap 0`. Moonshine always uses CPU. The default device
for the other models is CUDA, matching the GPU Tiny preview profile; use CPU
explicitly for an appropriate host. Repeated passes reverse model order every
other pass. Each model is released before the next loads.

## What runs

**Window** calls the production `StreamingTranscriber._process_incremental_chunk`
with each model's production decoder. Its defaults are 3 seconds of new audio,
0.75 seconds of overlap, `beam_size=1`, `vad_filter=False`, and unmodified
append-only text assembly. Whisper consumes the segment generator inside the
timed region. Optional decoders ignore Whisper-specific options. Both paths use
the decoder's default automatic language behavior; Tiny is English-only.

The input is decoded to the recorder's 44.1 kHz mono PCM16 format before timing.
Window boundaries respect its 1024-frame callbacks: the actual 3-second cadence
is 3.0186 seconds. Preparation/resampling, overlap concatenation, decode,
worker IPC and temporary audio serialization (optional models), text assembly,
and callback checks are within service timing. Input file decoding, scoring,
model load, and a full-clip warmup are outside warm service timing. Warmup and
load times are recorded separately.

**Native** additionally exercises `stream_audio` for the three catalog models
advertising streaming: Nemotron and both Moonshine sizes. It uses 0.75-second
updates quantized to recorder callbacks (0.7663 seconds), automatic language,
and no repeated overlap audio. NeMo interim utterances replace their preceding
partial; completed utterances append. Moonshine lines replace by stable ID.
All raw text-changing events are retained in the detailed JSON. These are
candidate dictation preview adapters using the production worker API, not an
assertion that native dictation preview is wired into the current app.

## Metrics and limits

- **Update p50/p95**: measured service time per full input update, including
  native updates that return no new text. A fast empty response is not a fast
  visible transcript; read first-text and WER alongside service timing.
- **First text**: simulated time from audio start until a nonempty update is
  available, including initial buffering and serial backlog. Median excludes
  clips with no text before EOF; their count is reported separately.
- **Live WER**: full reference versus text available at audio EOF before
  stopping. Unshown trailing words count as deletions.
- **Drained WER**: diagnostic after decoding remaining audio / native finish.
  This is not the app's separate final transcription or a reproduction of
  `stop_streaming`, which sets a stop flag that can discard pending segments.
- **Checkpoint WER**: AMI only, repeated reference-prefix scoring at common
  3.0186-second audio-clock checkpoints. Includes only words whose human end
  time has passed and hypotheses whose simulated completion has passed. It
  measures lag throughout recording; repeated prefixes are not independent
  observations and this score is not ordinary corpus WER.
- **RTF**: all measured service seconds, including flush, divided by audio
  seconds. Deadline misses compare service time to that profile's input cadence.
- **Silence**: insertion count; WER is undefined for an empty reference.

WER uses the existing meeting benchmark's exact word-level Levenshtein metric:
NFKC/lowercase, punctuation ignored, apostrophes retained, AMI acronym underscores
removed. Fillers, spelling, and number-format differences count. Corpus and group
WER are word-weighted; repeated passes are repeat measurements of the same clips.
The unchanged overlapping append strategy can insert repeated words and make
window WER worse than whole-file ASR. No cleanup or overlap deduplication is used.

Replay runs as fast as inference permits. Completion is modeled as
`max(previous completion, audio availability) + measured service time`. It does
not exercise the recorder thread, its finite queue/drop behavior, Qt rendering,
concurrent final-ASR scheduling, or native meeting preview resets/busy gates.
The native resampler works on each accumulated update, not individual meeting
capture blocks. Faster-than-realtime replay also differs from real-time power
management. Results on a busy desktop must not be described as isolated GPU
throughput or measured hotkey-to-paint latency. No timestamps are fabricated for
LibriSpeech; only AMI contributes checkpoint accuracy.

The JSON records exact source and audio hashes, model/runtime manifests, actual
devices, package versions, host GPU snapshots, per-call times, partial text,
references, group metrics, and load/warmup times. Only installed pinned artifacts
are used. Run `python -m pytest tests/test_live_preview_benchmark.py` to check
boundary quantization, overlap, delayed visibility, weighted WER, native revisions,
final flush, and failed-inference handling without loading models.
