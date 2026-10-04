# AI cleanup and meeting intelligence

Choose separate text models in **Settings → AI cleanup** and **Settings → Intelligence**. Supported providers include OpenAI, OpenRouter, Ollama, Groq, OpenCode Go/Zen, and custom OpenAI-compatible endpoints. Ollama needs a separately managed server. Custom endpoints let you choose **Chat Completions** or **Responses**; existing custom endpoints keep Chat Completions. Supported modern OpenAI text models use Responses automatically.

Add credentials in **Settings → API keys**. Keys use the OS credential store. Environment variables or `.env` are a fallback: `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`, `OPENCODE_GO_API_KEY`, and `OPENCODE_ZEN_API_KEY`.

For dictation cleanup, enable **Settings → AI cleanup**, teach spelling and style with **Learned rules**, and use [Profiles](cleanup-profiles.md) for reusable formats.

## Meeting engines

In **Settings → Meeting Mode → Intelligence**, choose **Pi** (the default), an installed **Claude Code**, **Codex**, or **OpenCode** agent, or the packaged **OpenCode SDK** under **Agent core**. The Agent core tile shows **OpenCode SDK** when selected. Pi uses your selected text endpoint and API key. Install Pi or OpenCode SDK from **Downloads → Components** when available on your platform. OpenWhisper detects installed agents and their sign-in status; you can select a model or keep the agent's default. Agent passes use that agent's sign-in, providers, and models and count toward its account or API usage. Installed agents use a slower live cadence. OpenWhisper does not install, update, or sign into them for you.

Standard API is retired. Saved `direct` choices now resolve to Pi; existing `opencode` choices continue to select the packaged SDK. Installed OpenCode has a separate `opencode_cli` setting and uses the [Agent Client Protocol](https://agentclientprotocol.com/). If the chosen agent is missing, install or update it, or select another. Recording remains available without AI insights; OpenWhisper never silently falls back to direct API calls.

## Agent behavior

Claude Code and Codex run headlessly with built-in tools disabled and unrelated MCP servers excluded. Installed OpenCode runs with its own tools denied. Every engine uses the same meeting tools and validation rules for cards, notes, polish, finalization, and retries. Saved meetings recorded with an installed agent, and their custom reports, retain that agent.
