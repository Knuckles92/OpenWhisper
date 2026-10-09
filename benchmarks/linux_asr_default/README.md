# Reproduce the Linux ASR default comparison

The [October 9 results](../../docs/linux-asr-default-2026-10-09.md) compare production adapters using a fixed English corpus. The [JSON companion](../../docs/benchmarks/linux-asr-default-2026-10-09.json) retains input identities, references, transcripts, timings, software versions and installed model manifests. Audio and multi-gigabyte model artifacts remain outside Git.

Use a dedicated benchmark directory containing these runners and the following inputs:

| Directory | Contents |
|---|---|
| `source/` | Extracted `git archive 0546928e5b0cd40f36db721b22693a9f66e0a283` |
| `corpus/` | The 70 WAVs and `manifest.json`, matching the companion's `corpus_manifest` |
| `whisper-base/` | Cached Systran faster-whisper base snapshot |
| `whisper-tiny.en/` | Cached Systran faster-whisper tiny.en snapshot for preview checks |
| `settings/` | Independent settings created by the runner |

Install the source application's dependencies in a Linux Python 3.12 environment. Through the production installer, cache Parakeet v3 and the CPU/GPU runtimes appropriate to the host, and cache the pinned Whisper turbo alias. The companion records the exact model and component identities. Jed's GPU runtime was Vulkan; the supplementary RTX 2060 run used CUDA. GPU profiles fail if they load the CPU.

`prepare_corpus.py` retrieves LibriSpeech test rows from the public Hugging Face dataset server and derives seeded noise, silence and a concatenated duration fixture. It also copies the ten existing AMI center excerpts from `repo/.tmp/preview-optimization/corpus`. The AMI source WAVs and manual 1.6.2 annotations are handled by `benchmarks.meeting_mode.ami`; `scripts/prepare_live_preview_corpus.py` prepares centered 30-second excerpts. Preserve the excerpt references and compare the generated WAV hashes with the companion before reusing results. Future dataset-server revisions can change row ordering; the companion records the exact source revision, utterance IDs and WAV hashes used here.

Run the main comparisons before the follow-up checks, with no other benchmark sharing the tested hardware:

```sh
/path/to/application/python run_comparison.py --repeats 3 --output comparison.json
/path/to/application/python run_followups.py
/path/to/application/python summarize.py comparison.json
```

The runner selects an independent `OPENWHISPER_DATA_DIR`, disables model downloads with `HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE`, and uses offscreen Qt. It opens no microphone and does not select an engine in the running app. Component and model caches are read from the standard per-user application directories. These environment settings disable downloads; they are not an OS network-denial sandbox.

Main inference uses Whisper beam size 5 and VAD, with fresh language settings: Whisper detects language and Parakeet requests English. The original measured runner's descriptive `method` string incorrectly said Auto language for all profiles. The retained measurements are unchanged; the companion preserves that label under `runner_recorded_method` and supplies a correction. The checked-in runner corrects that description only, so its hash differs from the original hash retained in the observations.

Each model receives one warm-up decode before 70 measured files. Three complete passes alternate order forward, reverse, forward. Timings include file decoding and worker IPC, excluding scoring, identity hashing, model creation and warm-up. Adapter creation plus first decode is reported separately; those observations use cached artifacts and do not measure a fresh download or an empty OS page cache.

Accuracy uses the first complete pass and reports changes between repeats. The 60 independent speech clips form the primary result. Noise variants and the concatenated file reuse source utterances, so they remain separate stress checks. Bootstrap intervals resample paired LibriSpeech speakers and AMI meetings; repeat passes do not increase the audio sample size.

Follow-ups select English for Whisper on all AMI and test-other clips, check Auto language on nine Parakeet clips, replay five files through the production window-preview path twice per adapter, and check cleanup/reload and cancellation of pending Parakeet decodes. Preview replay uses measured service times to drive a serial virtual clock; it excludes actual recorder queues and GUI painting.

RSS sums the benchmark parent and its descendants and may count shared pages more than once. GPU memory is total device memory including desktop applications. Keep these as observations for the specific host rather than portable requirements or exact per-model allocations.

The `wsl/` helpers reproduce the supplementary CUDA comparison using the same source and corpus. Set `OPENWHISPER_BENCHMARK_TURBO_SNAPSHOT` to the existing turbo snapshot directory; this replaces the tested runner's machine-specific path. Under `wsl/`, link `source/` and `corpus/` to the same inputs and stage the installed Linux components and Parakeet cache under `xdg/OpenWhisper`. `prepare_cuda.py` stages and verifies the source-pinned Linux CUDA wheels within the benchmark directory. Start the WSL runner with its `cuda-lib` directory on `LD_LIBRARY_PATH`, and keep WSL results separate from native Linux results. The source model hashes in the observations verify that both hosts used identical Whisper turbo weights.
