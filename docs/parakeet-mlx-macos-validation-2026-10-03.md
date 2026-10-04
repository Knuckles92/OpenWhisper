# Parakeet MLX validation on Apple Silicon — October 3, 2026

Real Metal and CPU inference works on this Mac after the fixes described below. Source and packaged workers passed English and Spanish transcription, timestamps, cancellation, reload, shutdown and offline restart. Production live-preview and meeting-chunk paths also passed. Keep the backend experimental: physical microphone capture and its macOS permission flow still need validation, and these synthetic fixtures do not establish accuracy on human or noisy speech.

This is evidence for [issue #30](https://github.com/Knuckles92/OpenWhisper/issues/30). The speed comparison, fixes and remaining limits were [posted to the issue](https://github.com/Knuckles92/OpenWhisper/issues/30#issuecomment-5975313032), which remains open at the user's request for testing on other Apple Silicon Macs. The fixes are still local and uncommitted. Detailed sanitized observations are in [the JSON companion](benchmarks/parakeet-mlx-macos-2026-10-03.json). No private recordings, credentials or user paths are included.

## Tested configuration

| Item | Value |
|---|---|
| Hardware | Mac17,4, Apple M5, 16 GiB unified memory, arm64 |
| OS | macOS 26.6.2, build 25G83 |
| Base source | `0214932b2d5cebbf9d10d5f9cfb9784191fbfa9d`, with local validation fixes |
| Source Python | 3.12.13 |
| Runtime | `parakeet-mlx==0.5.3`, `mlx==0.32.3`, `mlx-metal==0.32.3` |
| Component | `parakeet-mlx-0.5.3-mlx-0.32.3-db0dcfd455cc`; all 50 pinned wheels verified |
| Model | `parakeet-v3-mlx`, `mlx-community/parakeet-tdt-0.6b-v3` |
| Model revision | `ed2b7e8c15f9aaa0b5772e2efb986255eaef7e15` |
| Packaged app | Fresh 2.6.13 arm64 build, ad-hoc signed; relocated bundle launched successfully |

The runtime and model were installed with the production component/model installer APIs. Settings → Downloads subsequently showed the runtime installed and the model downloaded (2.51 GB). Clicking the download button itself was not exercised. The existing app in `/Applications` was not replaced. Test profiles disabled AI cleanup, clipboard copying and automatic pasting.

Auto selected Metal. A direct runtime probe observed `Device(gpu, 0)`, `metal.is_available() == True`, Apple M5 and `applegpu_g17g`. CPU selected `Device(cpu, 0)` with `arm64` architecture. These observations supplement the worker's reported device rather than relying only on the UI label.

## Failures found and repaired

1. **Metal failed on nonempty audio.** The waveform was converted to bfloat16 before the pinned runtime's FFT preprocessing. Its complex64 output was then viewed using the input dtype, doubling the frequency dimension: `[matmul] (128, 257) vs (514, 900)`. Keep waveform preprocessing in float32 and cast the completed mel features to the model dtype. The model still uses bfloat16 on Metal and float32 on CPU. Pad very short input to a complete FFT window (`n_fft=512`).
2. **Source workers could not import MLX.** Executing `services/local_asr/worker.py` put its directory on `sys.path`; the adjacent `mlx.py` shadowed the installed MLX namespace package. Remove that script directory before importing optional runtime packages. Real source-worker inference now passes.
3. **The backend was missing from the visible choices.** `MODEL_VALUE_MAP` contained MLX, but `MODEL_CHOICES` did not. Add Parakeet MLX to the menu. Regression checks select it through both transcription tabs.
4. **Meeting live preview excluded MLX.** Include `parakeet_mlx` in the existing window-based Parakeet preview route. Actual production meeting preview emits text with both devices.
5. **The frozen GUI failed despite passing its previous self-test.** PyInstaller missed lazily imported public widget modules, including `ui_qt.widgets.tabbed_content`. Collect widget submodules into the bundle and resolve every public widget export during package self-test. The rebuilt full GUI opens all tested screens.

## Real workload results

Fixtures were generated locally with macOS `say`: Samantha for English and Mónica for Spanish, 165 words/minute, resampled to mono 16 kHz. The English clip is 8.986 seconds; Spanish is 8.692 seconds. Both include three sentences, a meeting time and a request to save the transcript. English retained the wording and rendered “nine thirty” as “9.30”; Spanish retained the wording. Sentence timestamps were ordered and bounded by the recording duration. They were not scored against manually annotated timing ground truth.

| Production operation | Auto / Metal | CPU | Observation |
|---|---:|---:|---|
| Model load | 0.269 s | 0.265 s | Loaded installed artifacts |
| Warmup | 0.397 s | 0.344 s | Successful |
| Warm English dictation decode | 0.153 s | 0.341 s | Complete fixture text |
| Spanish file transcription | 0.147 s | 0.349 s | Complete fixture text, Auto language |
| Queued live preview and finalization | 0.200 s | 0.342 s | Draft and finalized text emitted |
| Five-minute file (305.526 s) | 4.766 s | 10.557 s | All 34 repeated phrases recovered |
| Two durable meeting chunks | 2.544 s | 3.369 s | English + Spanish; 6 bounded segments and live preview |
| Active transcription cancellation | 0.289 s | 0.178 s | Worker terminated; caller received cancellation |
| Reload + decode, three cycles | 0.517–0.717 s | 0.751–1.096 s | Successful after every reload |
| Restart + decode after closing workers | 0.503 s | 0.722 s | Successful |

These are individual functional-test timings, not medians or a controlled performance comparison. The long recording repeats one synthetic clip. Live preview processes sliding windows; its wording and punctuation differ from a full-file transcript. The meeting test used production queueing and SQLite persistence with injected fixture PCM, without opening microphone or system-audio capture.

Empty input, one sample, 5 ms audio, one second of digital silence and seeded noise completed without crashing. Product silence gating returned no text on both devices. The raw CPU model hallucinated “Oh” on a one-second zero waveform; silence gating remains necessary, and broader noise rejection is unvalidated.

### Memory observations

| Measurement | Metal | CPU |
|---|---:|---:|
| Direct adapter process peak RSS | 810 MiB | 2,817 MiB |
| Direct adapter MLX allocation peak | 1,675 MiB | 2,505 MiB |
| Final frozen worker lifecycle peak child RSS | 1,611 MiB | 2,734 MiB |

RSS and MLX allocations are different measurements and must not be added together. MLX allocation counters include GPU allocations; RSS alone does not describe unified GPU memory. Values are process high-water observations across each test sequence, not steady-state or long-file memory estimates. The source workflow memory sampler was blocked by the network-denial sandbox; its invalid zero was removed from the report.

## Offline, packaging and native UI

After the initial downloads, source production workflows and final frozen worker lifecycle tests ran under an OS sandbox denying every network operation. Direct adapter probes also rejected socket connections and recorded no attempted connections. Both devices loaded, transcribed, restarted and transcribed again offline.

The final frozen executable passed 10 real lifecycle stages per device: load, English, Spanish, graceful shutdown (exit 0), offline restart, decode after restart, active inference termination, restart after cancellation, decode after cancellation and final graceful shutdown. Cancellation ended the child in under 40 ms. This uses real downloaded native MLX libraries through the packaged worker protocol, not the fixture-based release-health test.

The macOS build passed its expanded self-test, deep strict ad-hoc signature validation and native-library audit. Every Mach-O file contains arm64 and no bundled library refers to an absolute host library path. The approximately 314 MB app bundle was copied to a separate test installation directory and launched there. No DMG was created; Gatekeeper, notarization and drag-to-Applications installation were not tested.

Native UI checks passed:

- Source classic UI: English file upload on CPU; installed runtime/model visible in Downloads.
- Relocated packaged classic UI: Spanish file upload on Metal; warmup and live-preview controls available.
- Classic controls fit at the minimum 650 × 580 logical-pixel window size.
- Omarchy controls fit at 550 × 490 logical pixels. The long model name elides within its control without displacing Device or Language.
- Omarchy switching Auto → CPU reloaded the model and updated the loaded-device status.
- Quitting Omarchy during the five-minute CPU upload exited the app with code 0. Process inspection confirmed no test app or MLX worker remained.

## Automated validation and reproduction

- Opt-in real hardware suite: **6 passed**.
- Affected ASR, meeting, preview, settings, upload and macOS installer regression suites: **370 passed, 2 skipped**.
- Deferred-screen, macOS/Linux/Windows packaging and release-health regression suites: **36 passed**. These overlap some installer coverage above.
- Ruff passed for the changed MLX adapter and MLX test files. `git diff --check` passed.

Install the pinned runtime and model through Settings → Downloads, use the locked Python 3.12 source environment, then run outside an execution sandbox that blocks Metal or macOS speech synthesis:

```sh
uv sync --frozen --group build
OPENWHISPER_TEST_MLX=1 .venv/bin/python -m pytest tests/test_parakeet_mlx_hardware.py -q
.venv/bin/python -m pytest tests/test_parakeet_mlx.py tests/test_meeting_voice_preview.py -q
OPENWHISPER_PYTHON="$PWD/.venv/bin/python" ./scripts/build_installer_macos.sh --skip-dmg
```

The hardware suite generates its own local synthetic speech, runs Auto and CPU through the real source-worker path, verifies uploads, previews and bounded timestamps, and exercises short input, silence, cancellation, worker cleanup and successful reload. It skips by default and does not download artifacts. Source fingerprints, fixture descriptions and raw measured observations are stored in the JSON companion. The broader workflow and frozen-protocol harnesses used for this session remain in the ignored local `.tmp/macos-mlx` directory.

## Remaining issue checks

Physical microphone capture and the macOS microphone permission prompt are pending user approval. Their success cannot be inferred from file uploads or PCM-injected preview tests. Paired remote-engine testing also remains unverified because no second computer was available. Real human speech, multilingual switching, noisy meetings and packaged distribution approval need separate evidence. Keep issue #30 open and retain the experimental label until the required microphone/package lifecycle checks or explicit follow-up issues are completed.

## Follow-up: comparison with the existing Mac engines

A separate sequential comparison used the already-cached CPU backends and the exact same synthetic WAV files through each production backend. All runs used Auto language. Whisper used the product defaults of int8, beam size 5 and VAD enabled; the existing Parakeet weights are q8 GGUF. MLX used float32 on CPU and bfloat16 on Metal. This compares available product configurations rather than isolating runtime implementation or model size.

| Backend on the same Apple M5 | Warm 8.986-second English clip | 305.526-second file |
|---|---:|---:|
| Existing Parakeet GGUF, CPU | 0.312 s | 8.751 s |
| Existing Local Whisper base, CPU | 0.679 s | 63.390 s |
| Existing Local Whisper turbo, CPU | 10.198 s | Not measured |
| Parakeet MLX, CPU | 0.314 s | 9.375 s |
| Parakeet MLX, Metal | **0.119 s** | **3.645 s** |

Short timings are the median of three repetitions after an initial decode. File timings are single observations. These follow-up values are from a different run than the earlier workflow table; they are not pooled averages. The models ran one at a time, without shuffled order. Turbo's optional long-file timing was deliberately canceled after its English and Spanish comparisons completed, and no result is assigned to that case.

On these fixtures, Metal was about **2.6× faster for short transcription and 2.4× faster for the long file than the existing CPU Parakeet option**. Compared with the default Mac Whisper base configuration, it was about **5.7× faster for the short clip and 17.4× for the long file**. The MLX CPU fallback offered essentially no speed gain over existing CPU Parakeet. The product improvement comes from enabling the Apple GPU.

Existing Parakeet and MLX produced identical short English and Spanish transcripts. Whisper turbo also retained both fixtures' wording. Whisper base introduced an error in the Spanish ending (“comproe a los tiempos” instead of “comprueba los tiempos”). Two clean synthetic clips are insufficient to rank general accuracy, accents, noisy meetings or multilingual switching. No energy, temperature, CPU-load or battery-life measurements were collected.

MLX needs roughly **2.5 GB** of weights, versus **0.714 GB** for existing quantized Parakeet and **0.148 GB** in the cached Whisper base snapshot. It retains Parakeet's [25 European languages](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3); [Whisper covers a broader language set](https://github.com/openai/whisper). Offline processing, timestamps, preview and meeting support already existed in the CPU options, so these are retained capabilities rather than new privacy or workflow advantages.

All comparison models loaded locally without downloads. Parakeet/MLX ran with every network operation denied. Whisper's isolated worker uses localhost sockets, so its adjusted test sandbox permitted localhost IPC while denying external networking; both the permitted local connection and denied external connection were checked. The first deny-all profile blocked Whisper's local IPC and was excluded from measured results. Runtime observations and warm repetitions are saved under `backend_comparison` in the JSON companion.
