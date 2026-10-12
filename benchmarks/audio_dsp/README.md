# Microphone DSP evaluation (issue #45)

This experiment checks whether microphone conditioning helps **transcription**, and how much processing time/delay it adds. It runs outside the application: no microphone, API calls, model downloads or live settings changes. Generated recordings and full reports stay in `.tmp/`.

The proposal is [OpenWhisper #45](https://github.com/Knuckles92/OpenWhisper/issues/45). The inspected [AudioForge revision](https://github.com/FueledByRedBull/audio-forge/tree/b5598ae4dc794e4e60ffbda10a620ead8f36d488) has a Rust DSP engine, a Python 3.13/PySide6 interface, and RNNoise implemented with `nnnoiseless`. OpenWhisper uses Python 3.12 and records mono PCM at 44.1 kHz. Reusing Python UI widgets alone will not provide the DSP or solve sample-rate conversion.

## Prepare paired recordings

Use the project environment (NumPy and faster-whisper/PyAV are existing dependencies). Start with a JSON object containing `clips`, each with `id`, `audio_path`, `reference`, and optional `group`/`speaker_id`. Paths resolve relative to the manifest. Choose clean speech with trustworthy manual references and several independent speakers, including your own microphone recordings if available.

```powershell
.venv/Scripts/python.exe -m benchmarks.audio_dsp.prepare .tmp/linux-asr-default-2026-10-09/corpus/manifest.json --group librispeech_clean --limit 8 --output .tmp/dsp45/corpus
```

Each utterance produces clean, quiet (-18 dB), changing-volume, fan/hum proxy, keyboard proxy, and quiet/noisy versions, with leading/trailing pauses. Add `--noise-file keyboard-room.wav` to include recorded environmental noise. SNR defaults to 10 dB measured over clean speech-energy intervals. The clean and noisy pair are attenuated together if necessary to avoid clipping. Silence and noise-only files have empty reference transcripts.

Preparation is seeded and records original source metadata, input hashes, and generated WAV hashes. It refuses to overwrite an existing corpus manifest. All processing/ASR variants receive the same 48 kHz PCM conversion, including raw. Actual 44.1→48→16 kHz streaming resampling costs still need a later capture-pipeline measurement.

## Run the comparisons

```powershell
# Signal/frame timing only; does not measure recognition accuracy.
.venv/Scripts/python.exe -m benchmarks.audio_dsp.run .tmp/dsp45/corpus/manifest.json --output .tmp/dsp45/controls --repeats 3

# RNNoise C library plus the installed production Whisper adapter.
.venv/Scripts/python.exe -m benchmarks.audio_dsp.run .tmp/dsp45/corpus/manifest.json --output .tmp/dsp45/rnnoise-base --profiles raw,highpass,gate_agc,rnnoise,rnnoise_conditioned --rnnoise-library C:/path/to/rnnoise.dll --models base --device cpu

# Existing Parakeet/Qwen models can be evaluated using local ASR catalog keys.
.venv/Scripts/python.exe -m benchmarks.audio_dsp.run .tmp/dsp45/corpus/manifest.json --output .tmp/dsp45/parakeet --profiles raw,rnnoise --rnnoise-library C:/path/to/rnnoise.dll --models parakeet-v3 --device cuda
```

Supply an existing RNNoise library exporting `rnnoise_create`, `rnnoise_destroy`, `rnnoise_get_frame_size`, and `rnnoise_process_frame`. One way to stage a developer-only Windows binary without changing app dependencies is to download the `pyrnnoise==0.4.5` wheel with `pip download --no-deps`, extract its `rnnoise.dll`, and pass that path. In the Windows wheel, the DLL is under `pyrnnoise-0.4.5.data/purelib/pyrnnoise/`. Keep the wheel/library identity with results; different RNNoise builds can use different model weights. The [RNNoise C API](https://github.com/xiph/rnnoise/blob/main/include/rnnoise.h) uses float PCM-scale samples and 480-sample frames at 48 kHz. The benchmark handles scaling, persistent per-capture state, one-frame signal delay, partial frames, and tail flushing.

| Profile | What is evaluated |
|---|---|
| `raw` | Exact PCM passthrough; always included as the paired baseline |
| `highpass` | Experimental causal 80 Hz Butterworth biquad |
| `gate_agc` | High-pass plus an experimental smoothed threshold gate and slow bounded gain leveling |
| `rnnoise` | Actual RNNoise C library; no prototype gate or AGC |
| `rnnoise_conditioned` | High-pass → RNNoise → experimental gate/AGC |
| `audioforge_rnnoise` | Upstream full-chain simulator: input cleanup, RNNoise, output protection |
| `audioforge_full` | Upstream simulator plus threshold gate, gentle compression/automatic makeup, +2 dB at 3.2 kHz |

The experimental controls are **not** AudioForge implementations or proposed production defaults. The C RNNoise build is also distinct from AudioForge's Rust `nnnoiseless`. The comparison exposes those differences instead of assigning all denoisers the same label.

## Evaluate AudioForge itself

Build the pinned checkout following its [source instructions](https://github.com/FueledByRedBull/audio-forge/tree/b5598ae4dc794e4e60ffbda10a620ead8f36d488#quick-start-from-source), including trusted native runtime assets. Then use its Python interpreter for rendering while OpenWhisper's interpreter runs ASR:

```powershell
.venv/Scripts/python.exe -m benchmarks.audio_dsp.run .tmp/dsp45/corpus/manifest.json --output .tmp/dsp45/audioforge --profiles raw,audioforge_rnnoise,audioforge_full --audioforge-python D:/audio-forge/.venv/Scripts/python.exe --audioforge-root D:/audio-forge --models base --device cpu
```

The worker calls `mic_eq.mic_eq_core.simulate_auto_eq_chain`, preserves the engine's already-aligned output, and records native-extension hashes, source hashes, interpreter, explicit settings, whole-simulation runtime, and declared algorithmic delay. It disables model-backed VAD explicitly and uses the threshold gate so the experiment does not require Silero inference or feed reference labels to the engine. The preset is exploratory; it does not represent Auto Voice Setup. This simulator does **not** expose full-chain callback p95: those fields remain null and cannot pass the frame budget. An unbuilt/unavailable engine is recorded as an error and yields a nonzero exit status.

## Read the evidence

Each run writes `results.json`, processed WAVs for listening, and (for AudioForge) worker output/metadata. DSP state resets between captures and repeats, but stays continuous through speech and pauses within a capture. Frame timing includes the first frame and Python/native calls; initialization is separate. Algorithmic signal delay is reported separately from CPU time. A fast 10 ms RNNoise call still has a 10 ms signal delay.

With `--models`, ASR uses the production local adapters, installed models, an isolated settings directory, and offline download flags. A requested CUDA run fails if it falls back to CPU. The model warms up once; decode timing includes file decoding/worker IPC. DSP is replayed `--repeats` times; ASR runs once on the first processed output. Profile order alternates between clips. Scoring reuses the project's normalization and exact substitution/deletion/insertion counts.

Group summaries report word-weighted WER, paired WER change in percentage points (negative is improvement), deletion/insertion changes, and noise-only hallucinated words. Missing/failed cases make coverage incomplete. Silence WER stays undefined; hallucinated words are retained. No unpaired accuracy comparison is produced. Signal diagnostics include speech/pause level change, clipping, SI-SDR against the clean pair, and an explicitly named energy-above-threshold fraction. That fraction is not a production VAD false-trigger rate.

Before considering integration, require improvement on real fan/keyboard recordings with several speakers, no meaningful clean/quiet speech regression, and fewer false words on silence/noise. Inspect word deletions and listen to phrase starts/tails: removing noise can also remove consonants. Treat 5 ms callback p95 and zero 10 ms deadline overruns as provisional CPU budgets, then measure resampling, queues, device buffers, and model-backed VAD in the real recorder. The issue's claimed <5–10 ms total overhead is a hypothesis, not a benchmark result.

Synthetic noise/gain changes are stress probes. They are not real whispers, room acoustics, mechanical-keyboard recordings, or evidence that a default should change. Conditions reuse utterances and speakers; repeats improve timing estimates without increasing the independent sample count. Report groups separately and compare larger microphone corpora before making a product decision.

Validate the harness with:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_audio_dsp_benchmark.py -q
.venv/Scripts/python.exe -m ruff check benchmarks/audio_dsp tests/test_audio_dsp_benchmark.py
```

## Initial smoke run: October 10, 2026

[smoke-results.json](smoke-results.json) retains a compact observation with corpus/model/library/source identities and paired group counts. The complete local report and listening WAVs are in `.tmp/dsp-issue45/asr-base-validated/`; the separate DSP timing report is in `.tmp/dsp-issue45/signal-run/`. These measurements used the installed Whisper base CPU adapter and the `pyrnnoise` 0.4.5 Windows DLL. The runner has since received reporting/ABI-signature fixes; the snapshot records the source hashes actually measured.

Four LibriSpeech utterances from **two speakers**, 68 reference words per condition, produced 24 speech conditions plus silence/noise-only clips. All 130 profile/clip ASR comparisons completed. This is a harness smoke test, too small to establish a production benefit. Noise is synthetic, and gain attenuation is not whispering. Timing was collected on a developer desktop with uncontrolled background load; an ASR replay also overlapped harness validation.

| Condition | Raw WER | High-pass WER | Gate/AGC WER | RNNoise WER | RNNoise + controls WER |
|---|---:|---:|---:|---:|---:|
| Clean | 2.94% | 2.94% | 2.94% | 2.94% | 4.41% |
| Quiet | 2.94% | 2.94% | 10.29% | 5.88% | 8.82% |
| Changing volume | 2.94% | 2.94% | 5.88% | 7.35% | 7.35% |
| Fan proxy | 4.41% | 4.41% | 5.88% | 5.88% | 13.24% |
| Keyboard proxy | 2.94% | 2.94% | 2.94% | 4.41% | 8.82% |
| Quiet/noisy | 2.94% | 2.94% | 4.41% | 8.82% | 11.76% |

All profiles emitted zero words on the single silence and single noise-only probe. The separate signal run's worst clip RNNoise p95 was 0.57 ms, with 10 ms algorithmic delay; the ASR replay recorded one RNNoise frame above the 10 ms processing deadline. This does not validate the issue's <5–10 ms total-overhead claim.

These settings showed no accuracy improvement and several regressions. Keep processing opt-in while gathering real noisy microphone data and testing additional installed engines. This result applies to the tested C RNNoise build and experimental controls; **AudioForge's native full-chain accuracy remains unmeasured** because its compiled extension is unavailable in this checkout. The availability check correctly returned errors instead of substituting passthrough audio.
