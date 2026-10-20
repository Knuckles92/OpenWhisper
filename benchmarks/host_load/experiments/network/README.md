# Portable network, codec, and production-records benchmarks

These are benchmark-only copies of the measured scripts. They do not modify
OpenWhisper production code. All host listeners/proxies bind `127.0.0.1`, use real
pinned TLS and disposable local identities, and replace Tailscale lookups with
not-installed callbacks. Settings/storage live in temporary directories; paired
device tokens exist only inside the test fixture. No real user credentials are
needed. The engine is a serialized fake, not a real ASR capacity measurement.

## Prerequisites and scope

Use a Python environment with the selected OpenWhisper revision's dependencies,
NumPy, cryptography and websockets. Native FLAC additionally requires SoundFile
(the measured wheel was 0.14.0, supporting `compression_level`) and libsndfile.
Native Opus requires installed libopus discoverable by the loader or supplied
with `--opus-library`. The ctypes wrapper and discovery were tested only on Linux;
other platforms require adaptation and validation. The alternative FFmpeg codec
backend requires an existing FFmpeg executable with FLAC and libopus support;
pass `--ffmpeg /path/to/ffmpeg` if it is not on PATH. No script installs packages
or downloads audio/software. Native codec calls are research code, not a hardened
untrusted-media decoder.

Supply an existing **16 kHz, mono, uncompressed PCM16 WAV** using `--audio`. The
original measurement used the public `whisper.cpp/samples/jfk.wav` sample, repeated
for longer clips, SHA-256:
`59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e`.
Supply your own local copy matching that hash, or other representative speech.
Do not substitute silence when comparing compression. A single repeated speaker
is not a representative corpus.

The portable copies validate source HEAD against required `--expected-revision`
and reject tracked modifications. `--output` is required and must not already
exist; parent directories are created. Every host run records the full source
revision, except the original-format lock-churn list. A Python audit hook blocks
non-loopback network I/O and configured loopback proxy endpoints in this process;
this is not a kernel sandbox and does not constrain native FFmpeg subprocesses.
Only trusted local audio and installed dependencies should be used. The scripts
opt out of ORT/Hugging Face telemetry and bypass HTTP proxies for loopback.

## Copyable commands

From a checkout containing this `benchmarks` directory, set the following paths.
The baseline and candidate must be separate clean worktrees. The original
measured baseline was `169d254`; the records candidate was `ab9b4c8` (scheduler,
weak lock registry and telemetry, before the later event-refresh follow-up).
That isolated historical commit was lost with the shared workspace. The commands
below compare the preserved baseline with the later, published integrated candidate:

```bash
REPO="$PWD"
BASE="$(dirname "$REPO")/openwhisper-network-baseline"
CAND="$(dirname "$REPO")/openwhisper-network-candidate"
git worktree add --detach "$BASE" 169d254
git worktree add --detach "$CAND" 285d01d2d693046d3caccc1ae2b53f68bd29ae91
```

Do not label this newer candidate an exact reproduction of the historical
`ab9b4c8` timing experiment. Set `BASE` and `CAND` to the clean worktrees, then:

```bash
EXP="$REPO/benchmarks/host_load/experiments/network"
AUDIO="/absolute/path/to/speech-16k.wav"  # Existing PCM16 mono 16 kHz speech.
OUT="$PWD/network-results-$(date +%Y%m%d-%H%M%S)"
PY=python  # Select your prepared OpenWhisper Python environment.
BASE_REV=$(git -C "$BASE" rev-parse HEAD)
CAND_REV=$(git -C "$CAND" rev-parse HEAD)
test "$BASE_REV" = "$(git -C "$BASE" rev-parse '169d254^{commit}')" || exit 1

"$PY" "$EXP/network_bench.py" --source-root "$BASE" --expected-revision "$BASE_REV" \
  --audio "$AUDIO" --output "$OUT/network.json" --shapes 0:10000,20:20,100:5 \
  --clients 1,3,6 --durations 3,15,30 --reps 5 --label portable-reproduction

"$PY" "$EXP/codec_transport_bench.py" --source-root "$BASE" --expected-revision "$BASE_REV" \
  --audio "$AUDIO" --output "$OUT/native-codec-transport.json" --codec-backend native --reps 5

"$PY" "$EXP/records_bench.py" --source-root "$BASE" --expected-revision "$BASE_REV" \
  --audio "$AUDIO" --output "$OUT/records-baseline.json" --seconds 30 --repeats 2 --uploaders 0,4
"$PY" "$EXP/records_bench.py" --source-root "$CAND" --expected-revision "$CAND_REV" \
  --audio "$AUDIO" --output "$OUT/records-candidate.json" --seconds 30 --repeats 2 --uploaders 0,4

"$PY" "$EXP/native_codec_bench.py" --audio "$AUDIO" --output "$OUT/native-codecs.json" --reps 10
"$PY" "$EXP/codec_bench.py" --audio "$AUDIO" --output "$OUT/ffmpeg-codecs.json"
"$PY" "$EXP/records_lock_churn.py" --source-root "$CAND" --expected-revision "$CAND_REV" \
  --audio "$AUDIO" --output "$OUT/lock-churn-candidate.json"
```

Use `--codec-backend ffmpeg` and a new output file to measure subprocess codec
costs. The transport codec matrix remains the measured fixed 0ms/10Gbit,
20ms/20Mbit and 100ms/5Mbit shapes, with 3/15/30-second audio; network-only
`--shapes`, `--clients`, and `--durations` do not alter that fixed codec matrix.
Lock churn uses 5,000 begin/abort pairs; it accepts the common audio option for
fixture consistency but does not upload speech. Records uploads repeatedly stage
64MiB files by default (`--mib` changes this), hash-check/fsync, then abort. Plan
for substantial temporary disk writes; commit/import is not tested.

Run cases sequentially during a quiet interval, not alongside the main load
sweep. Every help command is safe and exits without starting a server. The
portable copies received syntax and CLI-help checks only, deliberately avoiding
new performance runs during the primary controlled sweep.

## Evidence and limitations

The original raw network/codec/records-contention files were lost during the
shared-workspace incident. The scaling report labels their surviving aggregate
observations accordingly; an `evidence/original-network/` directory is not
delivered. A later scratch-only record-lock count comparison has fresh raw
evidence and provenance. Use these portable entry points to generate new runs.
Native dependencies and generated encoded examples are not vendored.

The byte-pacing proxy is **not netem**: it does not model packet loss, TCP
congestion/ACKs, or real Tailscale/WAN behavior. Optional stalls are sensitivity,
not loss. Five burst repetitions and two short records repetitions do not prove
production tail SLOs. No real-engine throughput, GPU capacity or lossy-codec WER
claim follows from these scripts. Codec headers are recognized only by the
benchmark subclass. A production extension needs mutual capability negotiation,
decoded-duration quotas, cancellation and compatibility tests.
