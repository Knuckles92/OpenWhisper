# Network and privacy

OpenWhisper records, transcribes, and stores on your computer. This page lists every network connection the app can make, what starts it, and what it sends. It describes the code as of 2.7.x; if you find a connection that is not listed here, please [report it](../SECURITY.md).

In short:

- **The only connection made without you turning something on is the update check** (below). Turn it off in **Settings → General → Updates**, or with **Don't check automatically** in the update dialog.
- There is no telemetry, analytics, or crash reporting. `--diagnostics` writes a local file and uploads nothing.
- Everything else starts from a feature you turn on, a download you approve, or an action you take.

## Outbound connections

| Destination | What starts it | Default | What is sent |
| --- | --- | --- | --- |
| `api.github.com` (GitHub releases API) | Update check, 8 seconds after launch and then every 4 hours while running (an hour after a failed check), never automatically in a git checkout; **Help → Check for Updates** | **On** | A `GET` for the latest release, with `User-Agent: OpenWhisper/<version>`. No account, device ID, or settings. |
| `github.com`, `release-assets.githubusercontent.com` | Update check when GitHub's API is rate-limited (reads the latest-release redirect and its `SHA256SUMS.txt`); downloading an update you accepted | Follows the update check | Same user agent. |
| `files.pythonhosted.org`, `download.pytorch.org`, `www.python.org`, `nodejs.org`, `github.com` (NVIDIA NeMo-Speech.cpp and OpenWhisper releases) | Installing a component from **Downloads → Components**, or accepting a runtime prompt | Off until you install | Same user agent; resumed downloads send a `Range` header. Each file is pinned to a size and SHA-256. |
| `huggingface.co`, `download.moonshine.ai` | Downloading a speech model, subject to **Settings → Downloads → When a model is missing from this computer** (Ask by default) | Ask | `huggingface_hub`'s standard user agent for Whisper models; the app's user agent for the others. |
| `huggingface.co` (WeSpeaker speaker model, about 27 MB) | Starting a meeting when the model is not on the computer yet | Downloads without a prompt unless the policy above is **Never connect** or `HF_HUB_OFFLINE=1` | `huggingface_hub`'s standard user agent. |
| `huggingface.co` (file listing) | Adding a custom Whisper model from a Hugging Face repository | Off | The repository name. |
| Speech server you choose: OpenAI (`api.openai.com`, the default server), Groq (`api.groq.com`), a server you run (Speaches, LocalAI, vLLM, whisper.cpp, preset to `127.0.0.1` and editable), or a custom server | Choosing the **API** speech engine, then a **Server** on **Dictation → Voice model** or in the main window; picking a server also asks it for its model list (not for OpenAI, Groq or whisper.cpp) | Off | Your recording (split into pieces over 23 MB), the model id, optional language and dictionary hints, and the server's API key if it has one. Audio sent to a server on `localhost`/`127.0.0.1` stays on this computer; the Server field and Settings name the destination. |
| `api.openai.com` (meeting speaker labels) | Choosing OpenAI speaker identification for meetings and allowing audio upload | Off | The other side's meeting audio as MP3, any reference clips, and your API key. |
| Text model provider you choose: OpenRouter (`openrouter.ai`), OpenAI, Groq (`api.groq.com`), OpenCode Go and Zen (`opencode.ai`), a local server (Ollama, LM Studio, vLLM, llama.cpp, LocalAI on `localhost`/`127.0.0.1`), or a custom endpoint | AI cleanup, Command Mode, transforms, cleanup profiles, meeting AI insights and reports; listing a provider's models or verifying a key | Off (cleanup and meeting AI insights each need you to turn them on) | The text being processed (transcript, selected text for Command Mode, app context if enabled), instructions, and the endpoint's API key, if it has one. OpenRouter requests carry `X-Title: OpenWhisper`. |
| Local server addresses on this computer | Opening the AI cleanup or Intelligence page in Settings, once per Settings window, to mark which local servers are running | When you open those pages | Short requests to each local preset's address and standard port, loopback addresses only: routes that identify the server and need no key. |
| `api.typesafe.ai`, or OpenRouter | Fast judgments on Jev (**Judge**: TypeSafe or OpenRouter), including Route by voice | Off | Up to 24,000 characters of transcript text and the questions asked, with your API key. |
| Your text model's endpoint (the AI insights or AI cleanup model) | Fast judgments with **Judge: Your text model**, including Route by voice | Off | The same excerpts and questions, up to 24,000 characters, with that endpoint's API key if it has one. On a server at `localhost`/`127.0.0.1` nothing leaves this computer, and meeting judgments run without the meeting's AI insights consent. On any other address, meeting judgments wait for AI insights to be on for that meeting, as they do on Jev. |
| Meeting agent's text provider | Meeting AI insights with the Pi agent core | Off | Meeting transcript and notes, to the text provider configured for meetings. OpenWhisper turns off Pi's install telemetry (`PI_TELEMETRY=0`), so Pi adds no attribution headers of its own. |
| Coding agents you have installed (Claude Code, Codex, OpenCode) | Meeting agent cores that use them, the agent hotkey, and actions that send words to an agent | Off | These programs run as themselves and talk to their own services. OpenWhisper passes them your words and meeting context, and, for an agent action that asks for them, your selected text and a screenshot of the window you were in. |
| Webhook URLs you enter | Hooks and webhook actions | None configured | Event JSON, which can include transcript text, signed with Standard Webhooks headers. Redirects are not followed. |
| Text filter URLs you enter | Hooks text filters | None configured | Dictated text before or after cleanup. |
| MCP Events callback URLs | An agent subscribing over MCP, with **MCP Events** on | Off | A signed verification challenge, then event payloads. HTTPS, or HTTP to this computer only. |
| A paired computer, TCP 47821 | Using a **Remote computer** engine; storing records on the host | Off | Your recordings, model choices, history queries, and (if you choose Host or Both) history and meetings, over TLS pinned to the host's certificate. |
| Local network, UDP 47821 broadcast; your Tailscale peers on TCP 47821 | Searching for computers in **Settings → Remote engine**; a paired client that cannot reach its host (at most every 30 seconds) | Off until you search or pair | A 512-byte discovery query with a random value. The optional local subnet scan connects to each address on your /24, only when you press it. |

Links in the app, such as release notes and provider sign-up pages, only open your browser when you click them. **Open a link** actions (for example a ChatGPT or Perplexity search) open your browser or app at an address with your words in it, so that site receives them when the page loads.

## Listening ports

| Listener | Address | Default |
| --- | --- | --- |
| Host Mode engine (TLS WebSocket) | All IPv4 interfaces, TCP 47821 (changeable) | Off |
| LAN discovery responder | All IPv4 interfaces, UDP 47821; answers private, link-local, loopback, and Tailscale addresses only | Only while Host Mode is on |
| MCP server and live API | 127.0.0.1:8767, plus this computer's Tailscale address if you turn that on | Off |
| Meeting dashboard (HTTP and WebSocket) | 127.0.0.1 on a random port; all interfaces with the **LAN** option | Runs while a meeting or its dashboard is open |
| History API (`--api`) | 127.0.0.1:8766 | Only when started with `--api` |
| Internal worker connections | 127.0.0.1, random ports, token-protected | Internal |

See the [security policy](../SECURITY.md) for how each one is authenticated.

The meeting dashboard's scripts and fonts are served by the app itself, so opening it in a browser contacts nothing outside this computer.

## Working offline

Downloaded speech models load from the local cache without contacting Hugging Face. To keep the app off the network entirely: turn off the update check, set **When a model is missing from this computer** to **Never connect**, and use only local speech engines (or the **API** engine with a speech server on this computer), local text servers, and, if you use Fast judgments, **Judge: Your text model** on a local server. Setting `HF_HUB_OFFLINE=1` before launch blocks model downloads regardless of settings.

"On this computer" means a `localhost`, `127.0.0.1` or `::1` address. A server you run on another computer on your network counts as remote: audio and text sent to it leave this computer, and meeting features treat it like a hosted service.
