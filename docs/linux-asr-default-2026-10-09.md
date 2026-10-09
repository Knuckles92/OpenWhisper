# Linux Parakeet default benchmark October 9 2026

Promote Parakeet v3 as the dictation default for new supported Linux x86_64 installs. On the tested English speech, it produced fewer word errors than the existing Whisper defaults and substantially faster final transcription on both tested NVIDIA GPU paths. On Jed's CPU, short dictation was faster at the median, while longer recordings were slower. Preserve saved engine choices, retain Local Whisper for Linux ARM and offer Whisper for languages outside Parakeet's coverage.

Optional live preview qualifies this recommendation: Parakeet kept pace but its assembled draft text was less accurate than Whisper tiny.en on the five preview fixtures. Keep preview optional and treat its quality as a separate improvement. These results support the final transcription default; they do not establish performance across every Linux CPU, GPU or language.

The [JSON companion](benchmarks/linux-asr-default-2026-10-09.json) retains observations and input identities. [Reproduction instructions and runners](../benchmarks/linux_asr_default/README.md) accompany the results. Product defaults are unchanged by this investigation.

## Current defaults and Linux support

At source commit `0546928e5b0cd40f36db721b22693a9f66e0a283`, [config.py](../config.py) selects native Parakeet on Windows x64, Parakeet MLX on Apple Silicon with macOS 14 or later, and Local Whisper elsewhere. [Settings resolution](../services/settings.py) preserves existing selections. The README sentence saying all other platforms default to Local Whisper is already outdated for supported Apple Silicon Macs.

Linux's Whisper default is a policy choice, rather than an absence of Parakeet support. The production catalog ships pinned Linux x86_64 CPU, CUDA and Vulkan components. Auto selected Vulkan on Jed's older Pascal GPU and CUDA on the supplementary Turing GPU. The backend's `cuda` device label denotes the NVIDIA GPU in both cases; the recorded runtime component identifies the actual implementation. The catalog has no Linux ARM Parakeet package.

Parakeet covers [25 European languages](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3), including English, Russian, Spanish and French. Mandarin and other unsupported languages require a different engine. The measurements below cover English only.

## Hardware and comparison method

The primary test ran through SSH on Jed's native Omarchy Linux system: Intel i5-9300H, four cores and eight threads, approximately 8 GB RAM, GTX 1050 Ti with 4 GB VRAM, NVIDIA driver 580.178.04 and Linux 7.1.9. Its normal desktop and OpenWhisper instance remained open. The benchmark used independent settings and workers.

The supplementary system ran Ubuntu under WSL2 on a Ryzen 7 9800X3D with an RTX 2060, 6 GB VRAM and driver 610.74. WSL results remain separate from native Linux results. Both hosts used the same source snapshot, audio hashes, Parakeet Q8 weights and Whisper turbo weights. Both used faster-whisper 1.2.1, CTranslate2 4.8.1 and NumPy 2.5.2. Python was 3.12.14 on Jed and 3.12.3 under WSL.

Seventy clips total 693.3 seconds. The primary comparison comprises 60 independent speech clips, 562.755 seconds and 1,716 reference words: 26 LibriSpeech test-clean clips, 24 test-other clips from 25 speakers in total, and ten 30-second AMI conversation excerpts. Seven seeded noise variants, a 75-second concatenation and two nonspeech clips are separate stress checks. The AMI excerpts include accents, interruptions and overlapping speech; their word-weighted contribution is larger than the read-speech groups.

The corpus was selected before observing model output. Three complete passes alternate profile order forward, reverse, forward. Warm timings include production file decoding and worker IPC and exclude initialization, warm-up, scoring and identity hashing. Accuracy uses the first pass, with variation between repeats reported separately. Cached inference ran with model downloads disabled; no microphone or GUI capture was involved.

The comparison uses available product configurations: Whisper base/int8 for CPU, Whisper Auto/turbo with the host's selected compute type for GPU, and pinned Parakeet v3 Q8 through the production native runtime. It does not isolate model size or runtime implementation. Whisper uses beam size 5 and VAD. Fresh settings request English for Parakeet and automatic language detection for Whisper; the language sensitivity checks below account for that difference. The original runner's descriptive metadata incorrectly labeled all profiles Auto; the companion retains the original label and the correction without changing measured values.

## Final transcription results

Times are the median warm decoding total for the same 60 primary speech clips, representing 9.38 minutes of audio. Lower word error rate is better.

| System and backend | Decode time | Word error rate | Median decode for clips under 10 seconds |
|---|---:|---:|---:|
| Jed CPU, Whisper base int8 | 156.3 s | 28.73% | 1.84 s |
| Jed CPU, Parakeet Q8 | 178.1 s | 15.62% | 1.14 s |
| Jed GPU, Whisper turbo int8_float32 | 179.9 s | 25.52% | 2.68 s |
| Jed GPU, Parakeet Vulkan | 31.0 s | 15.44% | 0.16 s |
| WSL RTX 2060, Whisper turbo float16 | 51.7 s | 24.48% | 0.69 s |
| WSL RTX 2060, Parakeet CUDA | 6.5 s | 15.68% | 0.06 s |

Parakeet was **5.8 times faster** than Whisper Auto on Jed's GPU and **8.0 times faster** under WSL for this primary set. Jed's GTX 1050 Ti does not support CTranslate2's float16 compute mode; Auto chose int8_float32. The WSL result confirms an advantage against Whisper float16 on newer NVIDIA hardware, without making the exact ratios portable to other GPUs.

On Jed's CPU, Parakeet reduced errors by 45.6% relative to the first Whisper pass and reduced median short-clip latency by 37.7%, while taking 14.0% longer across the primary set. Its short-clip 95th percentile was slightly worse: 2.47 seconds versus 2.07 seconds. The CPU benefit therefore depends on whether accuracy, typical short dictation or longer-file throughput matters most.

| Primary speech group | Whisper CPU | Parakeet CPU | Whisper Jed GPU | Parakeet Jed GPU |
|---|---:|---:|---:|---:|
| LibriSpeech clean, 376 words | 5.32% | 1.33% | 2.93% | 1.33% |
| LibriSpeech other, 333 words | 13.21% | 5.41% | 5.71% | 5.41% |
| AMI conversations, 1,007 words | 42.60% | 24.33% | 40.52% | 24.03% |

Parakeet's transcripts were identical across each host/device's three passes. Whisper turbo also remained stable. Whisper base changed two AMI transcripts, yielding overall primary WER of 28.73%, 29.25% and 30.42%. Parakeet CPU, Vulkan and CUDA outputs differ slightly from one another; identical output across implementations is not assumed.

Paired cluster bootstrap intervals resample 25 LibriSpeech speakers and ten AMI meetings, with 10,000 draws. Parakeet's primary WER difference versus Whisper was −13.11 percentage points on CPU, with a 95% interval of −23.49 to −5.61 points, and −10.08 points on Jed's GPU, with an interval of −20.54 to −2.24 points. These describe this small English corpus, rather than all users or recordings. Public benchmark corpora may overlap model development datasets.

## Language and scoring sensitivity

An additional pass selected English for Whisper on all 24 test-other clips and all ten AMI clips. Test-other error counts were unchanged. Conversation errors improved, showing that Whisper's automatic language detection explains part of the primary difference.

| AMI language comparison | Word error rate |
|---|---:|
| Whisper base CPU, Auto | 42.60% |
| Whisper base CPU, English | 33.27% |
| Parakeet CPU, default English request | 24.33% |
| Whisper turbo Jed GPU, Auto | 40.52% |
| Whisper turbo Jed GPU, English | 30.19% |
| Parakeet Jed GPU, default English request | 24.03% |

Parakeet retained fewer errors in the matched English comparison. On the 34 checked clips, Whisper base with English took 67.3 seconds and Whisper turbo took 66.7 seconds in their single sensitivity pass. Those are useful observations for explicitly configured English users, rather than the repeated default timings above. Requesting Auto for Parakeet on three AMI and six test-other clips changed none of their transcripts on CPU or Vulkan.

Primary scoring ignores case and punctuation and normalizes AMI acronym separators. It retains fillers, repeats and ordered overlapping words, and counts numerals literally. A diagnostic AMI rescore removing common fillers and treating digit tokens below 100 as spoken English numbers still favored Parakeet: 22.96% versus 39.98% on CPU, and 22.65% versus 38.00% on Jed's GPU. This sensitivity is separate from the primary metric.

## Longer recordings and resource tradeoffs

The separate 75.115-second concatenated read-speech fixture took **24.5 seconds on Parakeet CPU versus 7.1 seconds on Whisper base CPU**. Parakeet had two word errors versus Whisper's twelve. On Jed's GPU, Parakeet took 3.8 seconds versus Whisper turbo's 8.5 seconds. This fixture checks duration handling and reuses ten primary utterances; it does not represent an independent continuous meeting.

Across all 70 clips, median times were 180.7 seconds for Whisper CPU, 217.8 seconds for Parakeet CPU, 210.2 seconds for Whisper Jed GPU and 37.6 seconds for Parakeet Vulkan. All measured configurations processed faster than real time. The seven nominal 10 dB white-noise variants yielded WER of 12.31% for Whisper CPU, 5.38% for Parakeet CPU/Vulkan and 6.15% for Whisper Jed GPU. All configurations returned no words for the two nonspeech clips in every pass.

Parakeet's weights occupy 714 MB, versus 148 MB for the cached Whisper base snapshot. Jed's Parakeet CPU runtime download is 4.6 MB and its Vulkan download is 18.0 MB; both are already packaged for Linux. The CUDA runtime download tested under WSL is 107.3 MB. Whisper's separate pinned Linux CUDA library archives total approximately 674 MB.

Observed peak summed process-tree RSS on Jed was 1,281 MiB for Parakeet CPU versus 1,070 MiB for Whisper CPU, and 571 MiB for Parakeet Vulkan versus 2,206 MiB for Whisper turbo. GPU memory peaked at 1,847 MiB versus 2,293 MiB, including a desktop baseline of approximately 923 MiB. RSS may count shared pages repeatedly; total GPU memory includes other applications. These are observations for this workload, not minimum hardware requirements.

Adapter creation plus first decode took a median 2.05 seconds for Parakeet CPU versus 3.35 seconds for Whisper CPU, and 1.67 seconds for Parakeet Vulkan versus 8.28 seconds for Whisper turbo. Cached artifacts and OS file caches were present. CPU pass totals varied by roughly 14%; Jed's GPU reached approximately 88°C during Whisper runs. Alternating order and medians reduce order effects without eliminating desktop contention or thermal variation. No battery or energy measurements were collected.

## Optional preview and lifecycle checks

Five clips totaling 121.19 seconds were replayed twice per adapter through the production preview path, using three seconds of new audio and 0.75 seconds of overlap. Whisper preview uses tiny.en, beam size 1 and no VAD; Parakeet shares its dictation worker. Service times drive a serial virtual clock. Each pass contained 36 nonflush update calls per adapter.

| Jed preview backend | Median update time across two pass medians | Largest pass 95th percentile | Assembled preview WER after drain |
|---|---:|---:|---:|
| Whisper tiny.en CPU | 521 ms | 1,237 ms | 41.13% |
| Parakeet CPU | 1,190 ms | 1,307 ms | 48.39% |
| Whisper tiny.en GPU | 158 ms | 302 ms | 40.59–42.20% |
| Parakeet Vulkan | 176 ms | 195 ms | 48.66% |

No adapter missed the three-second service deadline in 72 measured nonflush calls across its two passes, and every fixture produced live text. Parakeet's preview lost more reference words than tiny.en and had higher draft WER despite better full-recording transcripts. Preview is disabled by default in the tested configuration. The replay excludes physical recorder queues and GUI painting, so it establishes decoder cadence rather than complete microphone responsiveness.

All eight cleanup/reload checks produced nonempty text after reload. Canceling a pending Parakeet decode stopped the caller with `Transcription canceled`, and reloading restored transcription on both devices. Cancellation took 64 ms on CPU and 1.22 seconds on Vulkan in these individual checks.

## Default decision

The measured final-transcription gains justify Parakeet as the default for new supported Linux x86_64 installs focused on English dictation, with Auto choosing the installed GPU runtime or CPU fallback. The GPU case is particularly strong: less waiting, fewer errors and lower observed memory use. CPU users gain accuracy and faster median short dictation while accepting larger weights and slower longer-file processing.

Keep saved selections intact, keep Local Whisper available for broader language coverage and smaller CPU downloads, and retain Whisper as the default on Linux ARM. Address the optional preview's word loss before presenting Parakeet as an improvement across every workflow. These English measurements provide no accuracy ranking for its other supported languages or performance ranking for AMD and Intel GPUs.
