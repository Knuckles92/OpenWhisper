# Host Mode load harness

This harness exercises the production `SpeechHost` and `RemoteConnection` over
real, pinned TLS WebSockets on `127.0.0.1`. The server is a separate spawned
process; resource samples include its inference-worker descendants, not the
client generator. It does not open LAN listeners, UDP discovery, Tailscale, real
pairing files or credential storage. Temporary certificates and in-memory device
registries are discarded. `OPENWHISPER_DATA_DIR` is temporary before app imports.
`ORT_DISABLE_TELEMETRY=1` is set before importing optional inference packages and
inherited by children. No external service or paid model API is used.
The harness preserves existing proxy exclusions and adds loopback addresses to
`NO_PROXY` and `no_proxy`, so its local traffic never needs an outbound proxy.

Install the app's requirements, then `pip install -r benchmarks/host_load/requirements.txt`.
For the constrained setup tested here (Python 3.12.14, pytest 9.1.1 and
pytest-xdist 3.8.0), use:

```sh
python -m pip install -r requirements.txt -r requirements-build.txt -r benchmarks/host_load/requirements.txt -c requirements-release-constraints.txt
```

Run from the repository root:

```sh
python -m benchmarks.host_load --clients 8 --duration 120 --profile gpu-parakeet --rate .05 --output .tmp/host-load/run.json
python -m benchmarks.host_load.sweep --source-root . --output-dir .tmp/host-load/baseline --duration 60 --rate .05
python -m benchmarks.host_load.analyze --run baseline=.tmp/host-load/baseline --run candidate=.tmp/host-load/candidate --output .tmp/host-load/charts
```

Use `--source-root /absolute/path/to/immutable/main-checkout` to measure baseline
production modules while running the harness from the candidate checkout. The
output records source commit, dirty state, complete arguments and raw events.
Do not edit production modules during a sweep. Keep the same seed, arguments,
package environment and CPU concurrency in before/after runs. `sweep --jobs 1`
is the default; `sweep --jitter 0` (also the default) gives identical per-request
service costs across schedulers. Nonzero jitter is a separate sensitivity run
because a shared random stream is consumed in execution order. Larger job counts are shared-machine exploratory screens, not
isolated latency measurements. Run real CPU inference and full pytest separately
from capacity timing. The CPU percent scale is 100% per saturated core.

## Workloads and arrival model

- `dictation`: independent precomputed Poisson utterance releases, clipped
  lognormal durations from 2–30 seconds (median 6 seconds). The first release
  is exponential too, so short cells can legitimately have no samples and no
  acceptance claim. Preview and continuous meetings instead begin with a
  uniformly distributed 0–2-second startup phase. Default `.05`
  releases/s/client is three utterances/minute. `.2` is an intentionally heavy
  12/minute stress model; it can imply more than 100% recording duty and is NOT
  a realistic human typing/recording model. Each device has one sequential
  connection, matching the client's serialized request lock.
- `preview`: four recorder-quantized 3.0186-second windows, 0.75-second overlap
  after the first, followed by a final. Parakeet window preview uses `transcribe`,
  **not** `stream`. Preview windows carry the optional `preview: true` hint;
  older hosts ignore it. `--preview native` uses sixteen non-overlapping
  0.7663-second `stream` pushes, a finish frame, and final transcription. Native
  streaming is a protocol/scheduler stress case; choosing a Parakeet timing
  profile does not establish native Parakeet support. Remote Whisper preview is
  not supported by current UI: Whisper-profile preview rows are sensitivity
  experiments, not currently available product behavior.
- `meeting`: the real `MeetingRemoteBackend` route and its paired-model checks,
  with `--meeting-chunk 5` seconds by default. The spool targets 5 seconds and
  hard-cuts at 20; vary this argument for long uninterrupted speech. This does
  not run capture devices, VAD, SQLite spooling or meeting retry scheduling.
- `history`: a second authenticated history connection per client. Add
  `--chatty-history` to have the real broker issue one query/client/second and
  receive synthetic records. Without the flag these connections stay idle.
- `local`: remote dictation plus host-owned requests against the same engine
  lock; this is a shared-engine contention proxy, not owner UI/backend
  scheduling. In particular, real Whisper uses `fixture.engine.transcribe`
  and therefore the remote-only `WhisperEngine` lock, unlike the actual local
  entry through `IsolatedWhisperModel`.
- `records`: client zero sends 1 MiB `records_put` frames every 200 ms by default;
  other clients dictate. The sink writes to a temporary file. This measures
  real admission/TLS/dispatch/disk-copy contention, **not** full production
  manifest validation/database persistence. See the separate production-record
  experiment in the scaling report.
- `mixed`: meeting, preview and dictation clients (index modulo four), history
  channels, and host-owned dictation. Add `--chatty-history` for history traffic.

Release schedules are fixed before execution. A delayed client cannot reduce
its planned arrival count. `latency_s` includes client schedule backlog from the
intended release; `service_s` starts when the request actually starts. At the end
of the generation window, a currently executing request may drain, but queued
releases not yet started are explicitly recorded as `not_started`. Connection
refusals record every planned request as `not_connected`. These are **not**
zero-latency successes, and invalidate the all-dictations-success SLO.

The main SLO is observed p95 final-text latency <1 second for clips <15 seconds,
with no failed/censored final requests and no required connection refusals.
Preview skips are separate, not failed final dictations. No low-sample run
certifies reliability. Every summary includes offered/successful sample counts
and a nominal one-sided 95% binomial upper failure-rate bound when zero failures
were observed. That bound assumes independent/exchangeable Bernoulli attempts;
correlated, fixed-seed shared-queue traces do not establish fleet reliability.
Pure meeting runs have no dictation-final SLO: `acceptance_applicable=false`
and `acceptance_observed=null`, not a failed meeting SLO.
A CPU profile that cannot meet a one-second SLO at N=1 may still serve useful
work with a looser latency target.

## Simulation versus real inference

`gpu-parakeet` approximates the owner's prior 70.7-second/462-ms and
3-second/31-ms observations with `t=.012+.0063*audio_seconds`. Other supplied
observations differ. `cpu-parakeet` uses `.030+.247*audio_seconds`, matching the
single 8.8-second/2.2-second observation; its intercept is assumed.
`gpu-whisper-turbo` and `cpu-whisper-small` are **uncalibrated sensitivity
profiles**, not hardware measurements or claims about the RTX 2060/1050 Ti.
All fake profiles sleep for wall time with configurable jitter and a shared
serialization lock. They do not execute GPU kernels or reproduce cache,
thermal, batching, memory-bandwidth or recognition-accuracy effects.
`--parallel-decode` is an explicit hypothetical sensitivity mode.

Real Whisper uses the production `IsolatedWhisperModel` worker and
`WhisperEngine`, including IPC serialization. Real Parakeet uses the production
`LocalSpeechBackend`/`SpeechWorkerEngine`. Models must already exist; nothing
is downloaded or installed by real mode. Supply actual speech WAV (PCM16,
mono, 16 kHz); the requested test lengths repeat/truncate this clip. Inspect
`has_text` and the report's dedicated real-speech calibration. Tone-only fake
results say nothing about recognition quality.

```sh
# RTX 2060 host: existing downloaded Parakeet runtime/model root.
python -m benchmarks.host_load --engine real --family parakeet --model parakeet-v3 --device cuda --app-data "PATH_TO_EXISTING_APP_DATA" --audio-file speech-16k.wav --clients 4 --duration 300 --rate .05 --output parakeet-2060.json
# CPU on the GTX 1050 Ti laptop; the GPU is not used by this command.
python -m benchmarks.host_load --engine real --family parakeet --model parakeet-v3 --device cpu --app-data "PATH_TO_EXISTING_APP_DATA" --audio-file speech-16k.wav --clients 2 --duration 300 --rate .05 --output parakeet-laptop-cpu.json
# Whisper, previously downloaded model folder; int8 CPU avoids fp16 assumptions.
python -m benchmarks.host_load --engine real --family whisper --model "PATH_TO_DOWNLOADED_WHISPER_MODEL" --model-id tiny.en --device cpu --compute-type int8 --audio-file speech-16k.wav --clients 2 --duration 300 --output whisper-cpu.json
```

The Parakeet root is used only to resolve existing `speech-models/<model>` and
`components/<runtime>` directories. Settings, TLS identity and test records stay
in separate temporary locations. App source imports and installed runtime code
still require the normal app dependencies. GPU dtype/compatibility and actual
VRAM headroom must be verified on the real machine; these commands do not
promise GPU availability or a particular throughput. For Whisper model directories,
`--model-id tiny.en` (or the matching actual model ID) supplies routing metadata
so MeetingRemoteBackend can enforce its supported-model gate. A recognized
folder basename is used when omitted; an arbitrary folder name will not pass
meeting validation. This label does not verify the identity of downloaded weights.

## Metrics and artifacts

Each cell produces JSON (all attempts, resource samples, summary) and incrementally
flushed NDJSON attempts. Sweeps retain the exact command and exit code per cell.
CSV/JSON aggregation and PNG charts retain successful-tail latency separately
from unserved-work ratios. `host_s` follows the tested client's `request_timed` behavior: baseline returns
legacy `host_ms` (excludes admission), while newer timing-aware clients may
prefer `server_total_ms`. Raw `wire_host_ms` and `wire_server_total_ms` are
recorded separately; compare like-for-like fields. Do not call baseline
`RTT-host_s` pure network latency. Measurement-only
wrappers time admission and attach a small `_benchmark` object to replies; both
baseline and candidate use the same instrumentation. Fake inference separately
records engine-lock wait and actual sleep duration. Wire byte totals therefore
include small measurement metadata; use the network-specific experiment for
unmodified frame-size measurements.

`--max-connections 64` separates connection-policy limits from engine capacity.
It is **not** the production default and must be labeled a cap-relaxed sensitivity
experiment. Always keep the default-cap results. Pre-auth abuse, same-device
fairness, restart, revocation and connection leak experiments have separate
scripts/results in the report rather than being silently mixed into capacity
curves.

### Method version

Schema 3 holds successful main/history connections through the entire generation
window, including clients with zero scheduled utterances. Schema 2 closed each
client after its last request, so a zero-offer client could release a slot during
startup and allow more unique clients than the simultaneous connection cap.
Schema 3 also records every unserved host-own arrival and actual
`engine.describe()` metadata, separate from requested configuration, plus the
harness source hash. Describe metadata is the application's reported engine
state, not an independent GPU/VRAM measurement.

`--arrival-grace-s 10` lets already-planned arrivals be issued during a bounded
ten-second grace period after generation ends. Remaining unissued arrivals
are right-censored, not proven server failures. Use identical method versions
and grace periods for paired comparisons; do not silently combine schema-2
primary screens with schema-3 sustained followups.

For a soak:

```sh
python -m benchmarks.host_load --workload mixed --chatty-history --clients 4 --duration 3600 --rate .05 --connection-cycle-s 30 --quiescence-s 5 --output .tmp/host-load/soak.json
```

The server samples resources once per second. Start, live-server quiescent
(after clients disconnect), and after-stop snapshots let a report distinguish
normal retained allocation from still-live handlers. Overlapping a soak with
other work is acceptable for leak observation, but its latency must then be
labeled shared-machine and not compared as an isolated capacity curve.
Connection cycles are graceful reconnects between final/meeting requests, not
abrupt socket-drop tests. The five-second settle occurs while the host remains
alive, before its final resource snapshot and shutdown.
