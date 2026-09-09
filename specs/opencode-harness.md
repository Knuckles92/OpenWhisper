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
6. Build a Windows x64 component containing the tested Bun binary, locked production
   dependencies, bundle, inventory, notices, and offline self-test. Verify before atomic
   installation and prevent replacement during active local jobs.
7. Add SDK wire/lifecycle tests, Python authority/lifecycle tests, UI selection tests,
   packaged integration tests, Windows CI, and a selectable synthetic product evaluator.
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

Implemented steps 1–7. The Windows component has been built from the frozen dependency
lockfile and passes the offline SDK self-test. The full Python suite passed 2,367 tests and
379 subtests, with 40 skips. Pi's 8 TypeScript tests and OpenCode's 11 SDK tests pass, as do
both TypeScript checks and the repository's Python correctness check.

The synthetic configured-model evaluation is awaiting explicit approval to send the script's
invented meeting text through OpenRouter using google/gemini-3.8-flash and the configured API
key. Automatic approval review rejected that run because the payload and destination needed
specific authorization. No recorded meetings are involved.

The component archive and measured catalog metadata are staged locally. The catalog's
published flag remains false until the remaining evaluation/release check is settled and the
immutable release URL is verified. The source build is available for selection locally.


The real component installer also passed a complete local archive installation, payload
resolution, and uninstall in an isolated component directory. No external download or model
call was made in that check. Final focused checks passed 75 harness/UI tests and 139
component/UI tests (one platform skip). The staged archive is 141,497,663 bytes, installs
527,680,768 bytes, and has SHA-256
b9b7669ad1985ff3e9b1a88ae107648b526ec52b77bcc8bc2576cc8a2fc00377.
