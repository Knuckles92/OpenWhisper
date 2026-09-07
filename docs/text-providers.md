# Text providers for cleanup and meetings

OpenWhisper supports OpenAI, OpenRouter, custom OpenAI-compatible endpoints, and the four providers below. Speech recognition is configured separately.

| Provider | Base URL | Optional environment fallback |
| --- | --- | --- |
| Ollama | `http://localhost:11434/v1` (editable) | No API key required |
| Groq | `https://api.groq.com/openai/v1` | `GROQ_API_KEY` |
| OpenCode Go | `https://opencode.ai/zen/go/v1` | `OPENCODE_GO_API_KEY` |
| OpenCode Zen | `https://opencode.ai/zen/v1` | `OPENCODE_ZEN_API_KEY` |

## Setup

1. For cloud providers, enter the corresponding key in **Settings → API keys**. The OS credential store takes precedence over the environment and `.env`. Keys are never stored in settings or meeting endpoint snapshots.
2. Open **Model Manager → On-demand text cleanup**, choose a provider, refresh its catalog and explicitly select a model. Manual IDs are allowed, but OpenCode IDs need a known protocol mapping.
3. Configure **Meeting intelligence** independently. Each destination remembers its own model for each provider.
4. Enable cleanup or meeting intelligence in Settings as usual. The selection also serves uploaded transcripts and rule polishing, or all meeting text passes respectively.

Go and Zen have separate credentials, catalogs and model assignments. Refresh runs in the background and retains the last successful catalog on failure. Unrecognized OpenCode protocol routes are disabled until the application catalog supports them. A public OpenCode model listing demonstrates connectivity, not key validity; the key-test result explains this distinction.

## Ollama prerequisites

Run your own Ollama server and make the desired text model available before connecting. Select Ollama and use **Edit** in either model picker to change the shared server URL. The URL is shown in the provider details. OpenWhisper does not install Ollama, pull models, start services, or unload models.

Catalog discovery inspects installed model capabilities through `/api/show` and excludes models without completion support. If discovery is unavailable, enter an installed text model ID manually. Set the model's `num_ctx` on the server for your workload; discovery records that configured value, with a conservative 4,096-token assumption when it is absent. A model's advertised maximum is not the same as its allocated context.

Standalone Ollama cleanup defaults to a 120-second request timeout. Meetings keep their existing scheduler and deadline limits. A cold server may time out; start it and warm the model yourself before a meeting. Remote Ollama URLs send text to that server.

See [OpenAI compatibility](https://docs.ollama.com/api/openai-compatibility), [Ollama documentation](https://docs.ollama.com/), and the [Ollama FAQ](https://docs.ollama.com/faq) for server configuration, context allocation and cloud/data behavior.

## Protocol and engine compatibility

| Provider | Model API formats | Standard engine | Pi engine |
| --- | --- | --- | --- |
| Ollama | Chat Completions | Native tools or validated JSON operations | Requires a tool-capable model |
| Groq | Chat Completions | Native tools or validated JSON operations | Requires a tool-capable model |
| OpenCode Go | Chat Completions, Responses, Anthropic Messages | Model-specific adapter | Matching Pi API and tools required |
| OpenCode Zen | Above, plus Google GenerateContent | Model-specific adapter | Matching Pi API and tools required |

The model ID determines the OpenCode route; similarly named Go and Zen models can use different formats. The reviewed route and capability metadata live in `services/text_model_catalog.py`. Supported reasoning controls are translated per model; unsupported controls are omitted. Tool IDs and signed reasoning data survive subsequent tool turns. Only final text becomes cleanup output.

The standard engine retains its existing identifier, `openrouter_direct`, for saved settings. It uses the shared provider adapters for live cards, notes, questions, recall/context tools, transcript polishing, consolidation, reports and reruns. Models without native tools retain the existing validated JSON-operations fallback; that fallback does not add native search-tool support.

Pi requires the updated bundle built from this source. Its hello handshake advertises supported text protocols, and Python rejects older bundles for these new providers with an actionable error. Pi also probes tool support before starting intelligence. There is no automatic engine or provider substitution. See [sidecar build instructions](../sidecar/README.md) and [component packaging](packaging.md) for building and distributing the updated component. Published component pins must be updated when that artifact is released.

## Saved meetings and failures

A meeting captures its provider, model, URL, protocol and model capabilities. Later selection changes affect the next meeting. Saved-meeting reruns use that snapshot and resolve credentials at execution time; old snapshots remain readable.

Refusals, empty answers, truncated output, authentication failures, exhausted quotas and connection errors retain the raw cleanup transcript. Meeting failures preserve audio, durable transcript segments and existing state. Generated edits still pass through the existing evidence validation, human-edit and confirmed-card protections. Cancellation prevents a late response from publishing tool calls.

## Provider billing and data use

- **Groq:** [API overview](https://console.groq.com/docs/overview), [pricing](https://groq.com/pricing), [data handling](https://console.groq.com/docs/your-data).
- **Go:** [endpoints](https://opencode.ai/docs/go/#endpoints), [usage limits](https://opencode.ai/docs/go/#usage-limits), [privacy](https://opencode.ai/docs/go/#privacy).
- **Zen:** [endpoints](https://opencode.ai/docs/zen/#endpoints), [pricing](https://opencode.ai/docs/zen/#pricing), [privacy](https://opencode.ai/docs/zen/#privacy).

Go states that it is intended for OpenCode and other coding agents with similar requests. OpenWhisper identifies itself as OpenWhisper and supplies a stable `x-opencode-session` per cleanup job or meeting session. This integration does not imply official OpenWhisper support or approval for meeting/cleanup workloads. Check the provider's current terms for your use. Go and Zen billing and data policies are separate.

## Validation

Offline tests use synthetic transcripts and in-memory HTTP responses across all four protocols, including tool round trips, signed reasoning, cancellation, truncation, authentication/quota failures and endpoint isolation. Pi has protocol-registration and meeting-tool tests. No live provider request is required by the test suite.

For an opt-in live smoke test, use a synthetic transcript, explicitly select a model and run cleanup, then a short meeting with notes, a question, recall, polishing and a final report. Check usage in that provider's dashboard. Live account access and model quality are not established by the mocked tests.
