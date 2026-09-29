# OpenCode v2 meeting harness

OpenWhisper embeds the OpenCode SDK in a supervised Bun sidecar. Pi remains the default.
Select **OpenCode v2** as the agent core in Models → Meeting after installing its separate
Downloads component.
Payloads are built for Windows x64, Linux x86_64, and Linux aarch64 (glibc), the platforms
where Bun and every native SDK dependency ship prebuilt; macOS is not offered, as with Pi.
Provider, model, credentials, and cloud consent come from the existing OpenWhisper settings.

The SDK is embedded, as Pi's SDK is embedded in its Node sidecar. No globally installed CLI,
external OpenCode server, or OpenCode login is involved. The V2 SDK uses an in-memory HTTP
router; it does not open a listening socket. See the [official SDK docs](https://opencode.ai/v2/docs/build/sdk/).

## Runtime design

- meeting/agent/sidecar.py supervises both harnesses: authenticated NDJSON, process health,
  timeouts, restart limits, cancellation, and Python tool validation.
- meeting/agent/opencode_sidecar.py selects the bundled Bun executable, verifies the
  handshake, leases the component while in use, and isolates configuration and temporary data.
- sidecar/src/runner.ts, session.ts, rpc.ts, and tools.ts are shared. OpenCode receives the
  same host-built meeting prompts as Direct, with the pass's system charter supplied separately.
- src/adapter.ts is the only SDK integration. One embedded host lives for the agent's lifetime.
  Each cards, polish, notes, or consolidation pass gets a fresh session and deletes it afterward.
  Session prompt enqueues work; the adapter explicitly waits and checks the final assistant error.
- Only the five existing meeting tools are registered. OpenCode's default tools and agents are
  removed. Custom tools use codemode: false, so the model calls them directly. Python checks
  the request ID, generation, cancellation, pass policy, evidence, revisions, and human edits.
- Errors the SDK classifies as transient (rate limits, provider overload, dropped streams) get
  up to three retries of at most 10 s each, matching Pi's automatic retry. Other provider errors
  fail the pass at once. Python's pass deadline and cancellation still bound every retry.
- sessions.interrupt({resume: false}) cancels actual execution. Aborting the activity stream
  alone is insufficient. Late tool results cannot update a newer pass or emit its progress.
- OpenCode's database stays in memory. Config, cache, state, and temporary paths are private
  to the sidecar; ambient CLI settings, credential variables, and Bun preloads are excluded.
  Shutdown removes that root. Subsequent starts reap marked roots from exited app processes.
- OpenCode failure never silently selects another harness. Recording and transcription continue,
  and the existing meeting UI reports intelligence unavailable.

## Provider transport

src/provider.ts maps the app's four protocols to the SDK's native transports: Chat Completions,
Responses, Anthropic Messages, and Google GenerateContent. OpenRouter uses its native adapter
to retain signed reasoning details. App endpoint prefixes, headers, tool capability, context/output
budgets, and applicable reasoning compatibility metadata are preserved. Responses use store: false.
Native transports retain their own message and reasoning serialization.

## Versions and upgrades

The current tested pair is SDK **2.0.18** (the stable `latest` release on npm) and Bun **1.3.14**.
Exact pins are an OpenWhisper release/testing policy; neither SDK requires applications to freeze
versions permanently. Upstream publishes 2.0.x patches often; move only after the checks below pass.

Update both SDK package declarations and bun.lock, src/versions.ts, and
services/opencode_catalog.py together. Update Bun's version, its official archive hashes
(BUN_ARCHIVES in scripts/build_opencode_component.py), and the CI pins when changing the runtime. Bump the component version for any shipped code or dependency change.
Keep Pi's component and dependency versions independent.

## Checks and packaging

From this directory, with Bun 1.3.14:

    bun install --frozen-lockfile --ignore-scripts
    bun run typecheck
    bun test

From the repository root, on each target platform (the self-test runs the payload natively):

    python scripts/build_component.py meeting-agent-opencode
    python -m pytest tests/test_opencode_sidecar.py

The builder needs only the Python standard library. It downloads this platform's SHA-256-pinned
official Bun archive (baseline x64 builds, for CPUs without AVX2), installs production
dependencies into an empty staging tree with a frozen lockfile and disabled install scripts,
drops the unused node_modules/.bin command shims (symlinks on Linux), builds the runner, and
emits an inventory, dependency list, and license notices. It runs the offline SDK self-test
before producing dist/components/*.zip and sidecar-opencode/dist. The complete production
dependency tree is included; Bun runs with --no-install. CI builds and verifies all three
payloads (the aarch64 one on a native Arm runner) and uploads them as workflow artifacts.

Zip extraction drops POSIX modes, so Linux payloads list their executables in payload.json and
the installer restores them. Installer verification then checks the payload's platform, hashes
every inventoried file, and runs the same self-test from an empty working directory with
isolated configuration. The self-test uses an in-process loopback mock
provider, exercises actual SDK tool calls, checks the exact tool allowlist and system charter,
and proves consecutive passes do not inherit history. It makes no external model calls.

The Bun tests additionally cover all four wire protocols, endpoint/auth/header routing, provider
errors, streaming reasoning, cancellation during a provider request and during a tool call,
and reasoning replay. Python tests cover request authority, component leases, settings,
temporary data cleanup, and actual packaged SDK passes through the real meeting state store.

Optional synthetic product regression (uses the configured provider and incurs model charges):

    python -m benchmarks.meeting_mode.live_agent_eval --harness opencode --sidecar-dir sidecar-opencode/dist --output .tmp/opencode_live_agent_eval.json

It ends with one end-of-meeting consolidation under the app's own budget (300 s without
activity, 900 s cap); `--checks consolidation` runs only that pass. This script contains
invented meeting text and never reads recorded meetings. Release only after reviewing its results.

## Publishing a component version

1. Build each platform's archive from the commit that will ship (main.mjs bundles the shared
   sidecar/src runner, so rebuild after any change there). CI artifacts from that commit work too.
2. Create the release under the component tag, not an app version, and keep it off "Latest",
   which the app updater reads:

       gh release create component-opencode-2.0.18-1 --repo Knuckles92/OpenWhisper --latest=false --title "OpenCode meeting agent component 2.0.18-1" --notes "OpenCode SDK 2.0.18 with Bun 1.3.14 for OpenWhisper's meeting agent."
       gh release upload component-opencode-2.0.18-1 --repo Knuckles92/OpenWhisper dist/components/meeting-agent-opencode-*.zip

3. Download each asset back and compare its SHA-256 with the .catalog.json beside the build.
4. Pin the verified platforms, which also marks them published:

       python scripts/build_opencode_component.py --pin dist/components/meeting-agent-opencode-*.zip.catalog.json

Never overwrite a published archive; bump COMPONENT_VERSION and RELEASE_TAG instead.
