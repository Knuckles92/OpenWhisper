# OpenCode harness implementation plan and acceptance

## Decision

Embed OpenCode's V2 SDK in a separate, app-managed Bun sidecar, following
[OpenCode's documented SDK ownership](https://opencode.ai/v2/docs/build/sdk/).
This matches the existing Pi integration's use of its
[embedded SDK](https://pi.dev/docs/latest/sdk), while retaining process isolation
from the Python application.

Version pins define a tested downloadable component. They are not an SDK-imposed
architecture requirement. The adapter isolates future SDK migrations. Pi remains the
default and its component is unchanged.

## Implementation sequence

1. Extract common Python supervision and TypeScript meeting protocol/tools without
   changing the Pi boundary. Preserve old installed Pi handshake compatibility.
2. Add OpenCode's embedded host, fresh session per pass, explicit wait/error inspection,
   cancellation, progress mapping, and shutdown. Replace ambient coding instructions.
3. Restrict tools structurally and validate every request in Python. Bind tool authority
   to the request and child generation, retain real operation results for the scheduler,
   and protect notes-only and polish-only passes independently of the SDK.
4. Translate the existing endpoint snapshots, four protocols, headers, credentials,
   budgets, and reasoning requirements. Retain cloud consent in the Python meeting host.
5. Add the optional settings selection and payload resolver for live meetings,
   refinalization, and archive insight regeneration. Missing OpenCode is an explicit
   intelligence failure; it does not silently choose Pi or Direct.
6. Build Windows x64, Linux x86_64, and Linux aarch64 components, each containing the tested
   Bun binary, locked production dependencies, bundle, inventory, notices, and offline
   self-test. Verify before atomic installation and prevent replacement during active local jobs.
7. Add SDK wire/lifecycle tests, Python authority/lifecycle tests, UI selection tests,
   packaged integration tests, Windows and Linux CI, and a selectable synthetic product evaluator.
8. Run local checks and the synthetic configured-model evaluation, inspect the archive,
   publish an immutable component asset, and activate the measured Downloads catalog pin.

## Acceptance

- Existing Pi behavior and default survive regression tests.
- Five meeting tools only; no coding agent tool or ambient configuration enters a pass.
- Cards, questions, notes, polish, consolidation, and regenerated insights use the selected
  harness through the existing meeting interfaces.
- Current host instructions, transcript evidence, human edits, and consent rules remain
  authoritative.
- Cancellation stops SDK execution; late tools and activity cannot affect another request.
- Error/length/filter terminal states are surfaced. Recording continues when intelligence fails.
- Actual operation results reach the scheduler, including transcript corrections.
- Installed payload works from an empty cwd without a system Bun, Node, or OpenCode install.
- Package hashes and self-test pass; broken/mismatched payloads cannot initialize.
- Failed startup releases the process, runtime directory, and component lease.
- Published component metadata contains measured sizes and verified immutable hashes.

See [the harness README](../sidecar-opencode/README.md) for commands, version update steps,
and release checks. Real-model evaluation and release publication remain distinct from offline
test completion; record any pending approval or failed check before activating Downloads.


## Implementation status

Steps 1–7 are implemented; step 8 waits only on uploading the release assets.

- **SDK.** Pinned to the stable `@opencode/sdk`, `@opencode/core`, and `@opencode/plugin`
  2.0.18 (npm `latest`), replacing the 0.0.0-dev-19291 build, with Bun 1.3.14.
- **Consolidation.** The benchmark row "cancelled at 120 s, no items" came from the probe's own
  120 s cancel timer. OpenCode uses the same sidecar budget as Pi: 300 s without activity and a
  900 s cap, with reasoning, text, and tool events resetting the silence clock. Real
  DeepSeek V4.1 Flash consolidations of the synthetic meeting completed in 92 s and 167 s on
  Windows and in 47 s on Linux, keeping the corrected deadline, cap, and decisions.
  `live_agent_eval.py` now includes that pass under the real budget.
- **Retries.** Transient provider errors get up to three short retries, matching Pi's
  automatic retry; the adapter previously failed a pass on the first 429 or 5xx.
- **Platforms.** Windows x64 and Linux x86_64 payloads were built and self-tested locally (the
  Linux one in WSL Ubuntu 24.04, through the real installer, the Python supervisor with a mock
  provider, and one real OpenRouter consolidation). The aarch64 payload is built and verified in
  CI on a native Arm runner. macOS is not offered.
- **Release.** Each platform stays unpublished in `services/opencode_catalog.py` until its
  archive is uploaded under `component-opencode-2.0.18-1`, downloaded back, and pinned with
  `scripts/build_opencode_component.py --pin`.
