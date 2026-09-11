# Test and benchmark repairs — September 11, 2026

The investigation started from the audit of bcbd584. The repair plan is implemented;
validation checks benchmark contracts without downloading models or calling paid providers.

- [x] Isolate recorder output at session and test scope; verify a pre-existing recording survives recorder tests.
- [x] Replace the stale Advanced settings count with assertions for the intended controls.
- [x] Exercise public Whisper decoding, lazy iteration, VAD options, cancellation, recovery, saved-recording guards, and production splitting dispatch.
- [x] Collect an explicit opt-in large-audio backend check; use ordered WER and fail poor output.
- [x] Fix ordered accuracy, silence insertion counts, empty offline scoring, partial polish failure, unjudged output, and command exit status.
- [x] Replay through the production scheduler and carry live corrections into End; use production draft prompts for the default AMI profile.
- [x] Validate AMI cache provenance, recompute Gemini arms, and reject incompatible preview pooling.
- [x] Exercise the actual Pi SDK and shared runner through a child process and local streaming HTTP provider.
- [x] Mount dashboard controls and exercise requests, selection, failure, retries, pending state, and focus restoration.
- [x] Run Python, dashboard, Pi and OpenCode suites, type checks, lint, build, and benchmark CLI smoke checks.

## Behavioral changes

**Scoring:** word accuracy is now bounded 100 × (1 − WER) using ordered edit distance.
Raw substitutions, deletions, insertions, and WER remain available where applicable.
Silence has undefined WER (null), but its insertions contribute to group and corpus
error totals. An enabled empty AMI offline output is scored as all deletions; a
missing required score cannot silently substitute the live transcript.

**Replay:** live product evaluation runs the same scheduler tick as the application.
Segments become visible at their end timestamps; the two-minute initial context
period, note cadence, retries and live polish follow production. The default
--live-window-s is now a one-second tick; larger values are explicitly coarse
replay experiments. Replay uses direct agent transport and excludes provider
latency, capture concurrency, recorder drops and rendering. The configured meeting
model/provider are resolved from settings.

**Provenance:** AMI cache reuse requires matching source bytes, audio and annotations,
settings, installed model identity, Python package versions and machine metadata.
Legacy AMI caches are recomputed. Gemini evaluations always recompute requested
arms; --force remains a compatibility flag. Their reports include source/config
and input fingerprints. Disabled arms are identified explicitly and never inherited
from a prior invocation. Preview pooling rejects different model snapshots,
artifact/runtime metadata, device details, package versions and stable host fields.
Historical reports are not retroactively certified.

**Failures:** partial polish is failed even if earlier blocks succeeded. Invalid
product judgments count as unjudged, never ties. Required pipeline failures and
missing requested model results produce nonzero exits. Gemini's current arm follows
the actual consolidation status. Its former “grounding” counter is now explicitly
lexical overlap and does not claim factual support.

**Tests:** the Pi integration uses the installed SDK and production runner, verifying
handshake, actual tool registration and dispatch, request scoping, reasoning replay,
history reset, provider failure, cancellation and recovery. React interaction tests
use jsdom; only unsupported dialog browser methods are substituted. The existing
OpenCode SDK protocol tests also pass.

## Validation

- Final complete Python run with branch coverage: **2,452 passed, 40 skipped, 391 subtests**, in 187 seconds.
- Final focused command/replay/backend regressions: **68 passed**.
- Dashboard: **22 passed**; Pi: **9 passed**; OpenCode: **11 passed / 89 assertions**.
- All three TypeScript checks passed.
- Required dashboard npm ci and npm run build passed; the generated committed
  bundle is byte-for-byte unchanged.
- All **11 benchmark CLI --help smoke checks** returned zero.
- Ruff undefined-name/unused-import checks passed for the repository excluding the
  pre-existing untracked demo/ directory; that directory has an unrelated unused import.
- git diff --check passed.
- Coverage improved in the paths targeted by the audit:

| Area | Statement coverage before → after | Branch coverage before → after |
|---|---:|---:|
| Transcriber | 66.8% → 71.5% | 54.6% → 62.3% |
| Benchmarks | 19.2% → 33.1% | 17.5% → 25.3% |
| Services | 73.9% → 74.1% | 63.1% → 63.3% |
| All five measured packages | 72.0% → 72.9% | 58.8% → 59.4% |

The denominator includes services, meeting, transcriber, ui_qt and benchmarks.
Scripts and JavaScript child processes are outside these percentages. Broad
coverage remains incomplete; the new assertions target the previously misleading
success paths and untested runtime boundaries rather than asserting total coverage.

Local logs and raw coverage are in .tmp/repair-* and .tmp/pi-repair-final.log.

## Validation limits and real-model checks

These changes validate test and benchmark behavior; they do not establish new
model accuracy or speed rankings. No fresh AMI model matrix, paid Gemini/agent
evaluation, frozen installer run, microphone capture, or platform-specific run was
performed. Python coverage does not include child processes. The forty skips are
the previous thirty-nine environment/parameter skips plus the new opt-in large-audio check.

For repeatable installed-model timing, use scripts/benchmark_local_asr.py with a
local audio file, a reference transcript, explicit --models, and --repeats.
It separates first decode from warmed samples. scripts/benchmark_local_asr_corpus.py
adds per-group and corpus edit counts. The legacy speed script is explicitly a
single-decode smoke benchmark, not a warmed performance comparison.

To run the collected large-file check, set OPENWHISPER_LARGE_AUDIO to an existing
WAV larger than the configured threshold and OPENWHISPER_LARGE_REFERENCE to its
UTF-8 transcript, then run pytest tests/test_large_transcription.py. It requires
an installed local model, does not synthesize audio under pytest, and requires
ordered WER at most 50%. Production dispatch is tested separately with deterministic
backends; this optional check measures actual backend decoding.
