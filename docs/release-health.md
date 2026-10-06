# Reliability and performance release checks

Every pull request now runs the core recording, audio decoding, cancellation,
history and lifecycle regressions on Windows and Apple Silicon macOS. Each
platform checks both the pinned release dependencies and an unconstrained
`pip install -r requirements.txt` environment. Windows source coverage uses
Python 3.11; the release runtime uses Python 3.12. Linux retains the full suite.
Dependency snapshots and synthetic timing reports are retained as CI artifacts.

Native candidate builds resolve their source tag to one commit and run the full
CI workflow against that commit before building any installer. All platform
builds and the combined metadata use that same commit. The shared CI includes
backup/restore, interrupted recording and meeting recovery, ASR, remote-host,
and desktop lifecycle tests. Workflow reuse follows the
[GitHub reusable workflow contract](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

Run the offline lifecycle gate in a configured source environment:

```sh
python scripts/check_release_health.py --output release-health.json
```

Check an executable built by the installer scripts:

```sh
python scripts/check_release_health.py --executable path/to/OpenWhisper --output release-health.json
```

The gate enforces a 45-second process exit deadline and bounds fixture startup,
stop-to-result p95, cancel, restart, worker cleanup and peak process RSS. It
decodes a known WAV through production PyAV conversion, uses the real streaming
worker and queued Qt signal delivery, cancels a decode in flight, restarts the
stream, and persists/reopens a SQLite history entry in a disposable directory.
It also recovers a retained recording journal without duplicating audio, creates
a real SQLite backup, applies the staged restore through the startup data lease,
and verifies restored audio and preservation of the previous data. These checks
must appear as successful in the report; older or incomplete reports fail.
The frozen `--self-test` also includes this workflow. Installer builders run the
timed gate and write `build/release-health.json` before producing artifacts.
The macOS builder also copies the app out of the finished DMG into a disposable
installation path, checks its signature and frozen imports, and runs the timed
gate there. Windows checks a previous-version installation, upgrade and
uninstall; Linux installs the built packages on the advertised base systems.

**This gate uses a deterministic fixture decoder.** It does not measure real
ASR accuracy, microphone capture, model startup, GPU memory, application window
startup, or full UI shutdown. The report labels these limitations. It proves
the bundled service plumbing and its process can run and exit. Core controller
and cancellation regression tests cover additional UI orchestration behavior.

On equivalent hardware, add `--baseline previous-release-health.json` to reject
a regression greater than 50% (with a 100 ms timing / 50 MB RSS noise floor).
Baseline comparison is opt-in: different CI runner hardware is not comparable.
Missing, NaN, or invalid required metrics fail the gate.

## Optional hardware testing

Use the existing `scripts/benchmark_local_asr.py` and
`scripts/benchmark_local_asr_corpus.py` with cached models and a fixed corpus.
For a useful comparison, record 30 warmed dictations per selected engine on a
CPU-only machine and a supported GPU, including first model load, median/p95
decode latency, real-time factor, peak process-tree RSS, and accuracy. Benchmark
reports can contain source paths and transcripts; do not attach them as public support
reports without reviewing their contents.

When doing manual hardware testing, these checks can help compare a candidate
with the previous release on the same machine. They are optional guidance, not
a release or draft-upload requirement:

| Check | Acceptance criterion |
| --- | --- |
| Cold app startup to ready | Record five launches; investigate >20% median regression |
| Stop button to final displayed result | Record 30 utterances; investigate >20% p95 regression |
| Cancel during decode and model download | No late text/paste; verify isolated workers stop within their cancellation timeouts |
| Quit while workers are busy | Measure process exit time, including pending history writes and component installation |
| One-hour recording | No dropped capture frames; RSS bounded; retained WAV opens correctly |
| Microphone removal / disk-full | Visible failure; recoverable audio identified; next recording works |
| Forced termination | Recover the interrupted capture on restart; history and settings remain readable |
| GPU run | Record peak VRAM externally; verify requested device actually used |

Automated fixture checks cannot certify physical capture, driver behavior,
long-session resource use or model quality. Manual testing is useful when
available, but no hardware report or manual sign-off is required to release.

## Verify a candidate before uploading a release draft

**Build native installers** produces the `openwhisper-release-candidate`
artifact after all source and native-package checks pass. **Verify candidate and
upload draft** verifies that exact candidate and uploads it to an existing draft.
It does not rebuild the candidate or publish the release.

1. Download and extract `openwhisper-release-candidate` from the successful build.
2. Verify the candidate locally:

   ```sh
   python scripts/check_release_qualification.py --candidate release --expected-commit COMMIT_SHA --expected-run BUILD_RUN_ID
   ```

3. Upload the verified candidate using the build run and its existing draft tag:

   ```sh
   gh workflow run qualify-release.yml -f candidate_run_id=123456 -f release_tag=v2.6.15
   ```

Use the actual build run, commit, and release tag. The validator checks every
installer checksum, the complete file inventory, and full source qualification.
The workflow verifies the successful build run and checks that the release tag
still identifies the candidate commit. It rejects unrelated existing draft
assets and verifies the complete asset inventory and checksums after upload.
The Windows setup-only recovery option omits only the native update archive
and follows the same automatic verification steps. No manual hardware report
is requested or attached.

## Shutdown limits

There is no application-wide hard shutdown deadline. Isolated native inference
and supported network workers have bounded cancellation, while cooperative
history persistence and component filesystem operations can still await disk
I/O. Automated lifecycle checks do not establish a shutdown bound for stalled
drivers or filesystems.

## Safe support reports

Users can create a local report without starting the GUI:

```sh
python main.py --diagnostics support.json
# Installed application:
OpenWhisper --diagnostics support.json
```

The command creates a new file and refuses to overwrite an existing one. It
includes version, package versions, OS/architecture/CPU count, NVIDIA hardware
if present, allowlisted engine settings, and recent failure categories/code
locations. It sends nothing. The application stores the last 50 sanitized
failure events locally in `diagnostic-failures.json`; it never stores messages
or exception payloads there. Export revalidates persisted fields. Audio,
transcripts, API credentials, log messages, paths, hostnames, and paired-device
information are excluded. Raw application logs are intentionally not attached.

The report also summarizes the last 100 locally recorded numeric measurements
per supported metric (count, median, p95, maximum and latest). Missing samples
are omitted. Startup measures entry into the GUI bootstrap through the main
window appearing; it is explicitly named `startup_to_window_s` and does not
claim that a speech model is ready. Capture counts and stop/cancel/shutdown
timings are collected when the corresponding workflow completes. The bounded
`diagnostic-metrics.json` contains only allowlisted metric names and numbers.
Metric calls update bounded memory and wake a daemon writer; pending updates
coalesce into one snapshot if storage is slow. Logging shutdown gives the final
snapshot at most 100 ms to finish. Newest diagnostic samples may be lost on a
forced exit or stalled disk, without holding the GUI or Quit open for them.
