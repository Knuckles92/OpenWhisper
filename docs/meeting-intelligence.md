# AI cleanup and meeting intelligence

Choose separate text models in **Settings → AI cleanup** and **Settings → Intelligence**. Supported providers include OpenAI, OpenRouter, Ollama, LM Studio, vLLM, llama.cpp, LocalAI, Groq, OpenCode Go/Zen, and custom OpenAI-compatible endpoints. Custom endpoints let you choose **Chat Completions** or **Responses**; existing custom endpoints keep Chat Completions. Supported modern OpenAI text models use Responses automatically.

Add credentials in **Settings → API keys**. Keys use the OS credential store. Environment variables or `.env` are a fallback: `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `OPENCODE_GO_API_KEY`, and `OPENCODE_ZEN_API_KEY`.

## Local servers

The local servers ship preconfigured with their standard addresses. Each one is a separate server process that you install, start and load models in yourself.

| Server | Preset address |
| --- | --- |
| Ollama | `http://localhost:11434/v1` |
| LM Studio | `http://127.0.0.1:1234/v1` |
| vLLM | `http://127.0.0.1:8000/v1` |
| llama.cpp (`llama-server`) | `http://127.0.0.1:9931/v1`, the default since October 2026 |
| LocalAI | `http://127.0.0.1:8080/v1` |

- **Edit** in the endpoint picker changes a server's address when it runs on another port or computer. A server needs no key unless you started it with one; then name the environment variable that holds the key in the same dialog and enter the key in **Settings → API keys**.
- Opening AI cleanup or Intelligence checks once whether each preset's address, and each server's standard address, answers. The picker marks a server that is running as **Running**, and names the address when a server answers somewhere other than its preset, for example an older llama.cpp build on port 8080: choose Edit to use it.
- Each server's own model routes decide which models appear: embedding, speech and rerank models are hidden, and the server's context size, tool support and thinking support are used instead of guesses. A server that will not describe its models still lists them, judged by name.
- **Thinking level** applies to a thinking model. **Off** sends `reasoning_effort: "none"` to Ollama, vLLM, llama.cpp and LocalAI, which turns thinking off. LM Studio offers only the levels it reports for the model, and Off leaves the model's default.
- Cleanup requests to a server you run use temperature 0 (dropped when thinking is on), and no request forces an output limit on these servers, so the server's own limits apply. Cleanup waits up to 120 seconds per attempt, since the server may first load the model.

Text sent to a server on `localhost` or `127.0.0.1` stays on this computer. A server on another computer is remote, even on your own network.

For dictation cleanup, enable **Settings → AI cleanup**, teach spelling and style with **Learned rules**, and use [Profiles](cleanup-profiles.md) for reusable formats.

## Meeting engines

In **Settings → Meeting Mode → Intelligence**, choose **Pi** (the default), an installed **Claude Code**, **Codex**, or **OpenCode** agent, or the packaged **OpenCode SDK** under **Agent core**. The Agent core tile shows **OpenCode SDK** when selected. Pi uses your selected text endpoint and API key. Install Pi or OpenCode SDK from **Downloads → Components** when available on your platform. OpenWhisper detects installed agents and their sign-in status; you can select a model or keep the agent's default. Agent passes use that agent's sign-in, providers, and models and count toward its account or API usage. Installed agents use a slower live cadence. OpenWhisper does not install, update, or sign into them for you.

Standard API is retired. Saved `direct` choices now resolve to Pi; existing `opencode` choices continue to select the packaged SDK. Installed OpenCode has a separate `opencode_cli` setting and uses the [Agent Client Protocol](https://agentclientprotocol.com/). If the chosen agent is missing, install or update it, or select another. Recording remains available without AI insights; OpenWhisper never silently falls back to direct API calls.

## Agent behavior

Claude Code and Codex run headlessly with built-in tools disabled and unrelated MCP servers excluded. Installed OpenCode runs with its own tools denied. Every engine uses the same meeting tools and validation rules for cards, notes, polish, finalization, and retries. Saved meetings recorded with an installed agent, and their custom reports, retain that agent.

Reruns and custom reports use the endpoint a meeting was recorded with. If that endpoint has since been deleted and the meeting has no saved snapshot of it, they use the meeting endpoint chosen in Intelligence now, never a cloud provider you did not choose.

## Fast judgments on your text model

**Meeting Mode → Fast judgments → Judge** runs the meeting's fast judgments (live highlights, the open questions radar, voice commands, semantic topic changes, citation checks, semantic history search, insight review) and Route by voice on Jev, or on **Your text model**: the AI insights model or the AI cleanup model, as configured above. A text model is slower than Jev (a judgment may take up to 30 seconds) and its stated probabilities are not calibrated like Jev's, so the features' thresholds are untested policy on it. See [TypeSafe fast judgments](typesafe-fast-judgments.md#your-text-model).

## Privacy

- With AI insights on, the transcript and dashboard state go to the meeting's text endpoint. On a local server they stay on this computer; the consent dialog names the destination.
- Fast judgments on Jev or a hosted text model receive short excerpts too, and the consent dialog names that judge as well. Meeting judgments on those judges run only while AI insights are on for the meeting, and semantic history search ranks only saved meetings that had AI insights on.
- Fast judgments on a local server send nothing. Live highlights, the questions radar and voice commands run in meetings with AI insights off, and semantic history search ranks every saved meeting. Features that check AI insights' own output (citation checks, insight review, topic-change checkpoints) still need AI insights on.
- The Pi agent core runs with Pi's install telemetry off (`PI_TELEMETRY=0`).

[Network and privacy](network-and-privacy.md) lists every destination.
