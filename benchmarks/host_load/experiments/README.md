# Real CPU cross-request batching experiments

Research-only adapters. Not a production backend, no GPU claims, no streaming/cancellation/reload or timestamp parity guarantee. Whisper batching is English, shared options, clips at most 30 seconds and text-only. The native adapter targets the pinned Parakeet-v3 model/runtime. Do not treat its hardcoded descriptive model identity as support for arbitrary models.

## Setup

Use the application's pinned dependency environment (tested Python 3.12.14, faster-whisper 1.2.1, CTranslate2 4.8.1). Public models are downloaded separately; these scripts never install/download a model or runtime. Set `ORT_DISABLE_TELEMETRY=1` before Python launch, inherited by workers. HF telemetry is disabled too. Temporary app-data directories and patched temporary device settings protect actual pairings. Servers bind only to 127.0.0.1 via the host-load fixture.

Download and verify the assets described in PROVENANCE.md. Extract the native archive so `<runtime-parent>/nemo-speech/lib/` exists. The Whisper argument is a directory containing model.bin/config.json/tokenizer.json/vocabulary.txt. The native argument is the GGUF file itself.

From the repository root, with assets outside the repository:

```sh
export ORT_DISABLE_TELEMETRY=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
export OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1
export QT_QPA_PLATFORM=offscreen PYNPUT_BACKEND=dummy
python benchmarks/host_load/experiments/real_transport.py \
  --audio-file /path/to/jfk.wav --whisper-model /path/to/tiny.en \
  --mode production --clients 4 --repetitions 4 --output production.json
python benchmarks/host_load/experiments/real_transport.py \
  --audio-file /path/to/jfk.wav --whisper-model /path/to/tiny.en \
  --mode batch --clients 4 --active-jobs 4 --batch-size 4 --window-ms 5 \
  --repetitions 4 --output whisper-batch4-w5.json
python benchmarks/host_load/experiments/native_tls.py \
  --audio-file /path/to/jfk.wav --native-model /path/to/parakeet-v3.gguf \
  --native-runtime /path/to/runtime-parent --mode native --batching 1 \
  --window-ms 5 --clients 4 --active-jobs 4 --repetitions 6 --output native-batch5.json
python benchmarks/host_load/experiments/verify_mixed_requests.py \
  --audio-file /path/to/jfk.wav --whisper-model /path/to/tiny.en \
  --family whisper --output distinct-whisper.json
```

Use `--source-root /path/to/immutable-baseline` to test a different production checkout while retaining the current benchmark fixture; `--harness-root /path/to/harness-checkout` selects its package. Defaults resolve from this script's repository location, not a cloud-specific workspace. Use a clean subprocess for each case. Windows native-library loading is not implemented by this Linux-native C-ABI proof; the ordinary production harness remains the owner-facing cross-platform/GPU entry point.

For Whisper compare experimental batch-size 1 against 2/4 at 0/2/5/10/20 ms with four active clients. Do not equate its semantics to production VAD/timestamps. For native compare `--mode worker` (production IPC), `--mode serialized` (direct native lock), `--mode native --batching 0` and `--mode native --batching 1`. Keep model/callers/CPU budget identical for the final two. A custom native batching configuration is not used in worker mode. `--window-ms` controls native neural-stage collection, not an end-to-end request timeout.

Use `verify_mixed_requests.py --family parakeet` with native paths for the distinct-crop routing probe. It compares four different per-client reference transcripts across concurrent waves. This is not a complete cross-user privacy test.

Optional bounded process-tree resource capture:

```sh
python benchmarks/host_load/experiments/monitor_run.py --output resources.json --timeout 180 -- \
  python benchmarks/host_load/experiments/native_tls.py \
  --audio-file /path/to/jfk.wav --native-model /path/to/parakeet-v3.gguf \
  --native-runtime /path/to/runtime-parent --mode native --batching 1 \
  --window-ms 20 --clients 4 --active-jobs 4 --repetitions 2 --output result.json
```

## Evidence interpretation

Warm-up is excluded from per-request percentiles and native throughput. Whisper's whole-fixture wall_s includes warm-up/teardown, so do not use it as steady-state throughput. Model load and warm-up are reported separately by these portable scripts; the original pilot scripts did not measure load separately. Native graph execution hardcodes four threads despite OMP_NUM_THREADS=2. The native batcher adds three executor threads plus one scheduler. Multiple concurrent requests share one recognizer, not replicated model weights.

Current measurements did not prove a CPU batching gain. Retain all repetitions and outliers, randomize future runs, and validate on actual GPU hardware before claiming capacity. Parent-process before/after RSS is not peak process-tree memory. The optional sampler includes clients and warm-up, unlike the main harness's host-only sampling.

## Other experiments

- [Network, codecs and records](network/README.md).
- [Existing reload boundary characterization](reload-characterization.md). These diagnostic tests document known limitations, not desired production behavior.
