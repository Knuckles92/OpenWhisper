# Security policy

## Supported versions

Security fixes go into the latest release line, currently **2.7.x**, and ship as a new patch release. Older lines do not get fixes; update to the [latest release](https://github.com/Knuckles92/OpenWhisper/releases/latest). Source checkouts should track `main`.

## Reporting a vulnerability

Please report vulnerabilities privately, not in a public issue, discussion, or pull request.

Use GitHub's private vulnerability reporting: open the repository's **Security** tab and choose **Report a vulnerability** ([direct link](https://github.com/Knuckles92/OpenWhisper/security/advisories/new)). If that option is not available, open an issue that only asks the maintainer for a private contact, with no details of the problem.

Include what you can of:

- the version and platform, and whether it is an installer, package, or source checkout
- the feature involved and the settings it needs (for example, Host Mode on, MCP on with a permission switched on)
- steps to reproduce, and what an attacker gains
- whether the attacker needs to be on the same computer, the same network, a paired computer, or only able to send a webhook or a web page

Leave API keys, tokens, and private recordings out of the report. This is a one-maintainer project: expect an acknowledgement within a week. Once a fix is released, the advisory is published with credit unless you prefer otherwise.

## Scope

OpenWhisper runs on your computer and keeps recordings, transcripts, and settings in its data folder. Most features stay on the computer; the ones below listen on a network port, accept credentials, or download and run code, and are where reports are most useful. Defaults are as of 2.7.x. [Network and privacy](docs/network-and-privacy.md) lists every outbound connection.

### Host Mode and remote engines

Off by default. When **Share this computer** is on, the host listens for TLS WebSocket connections on port 47821 on all IPv4 interfaces (the port can be changed).

- The host creates a self-signed certificate on first use. Clients pin its SHA-256 fingerprint and refuse a different certificate before sending their token.
- Pairing uses either a 6-digit code (valid for 5 minutes, one use, closes after 5 wrong tries) or a request the host owner approves. Approval shows a short code on both computers, derived from both sides' random values and the certificate fingerprint, to compare before clicking Allow. Approval requests are accepted only from private, link-local, loopback, or Tailscale addresses and are rate-limited.
- With **Pair my Tailscale computers without a code** (on unless turned off), a computer that Tailscale reports as signed in by the same, untagged user pairs without a code.
- Each paired computer gets its own random token. The host keeps only its SHA-256 digest; the client keeps the token in the OS credential store. Every request re-checks it.
- Model management, keeping records for paired computers, and managing this computer's MCP settings are separate opt-ins, all off by default.

### LAN discovery

Runs only while Host Mode is on. The host answers UDP queries on port 47821 from private, link-local, loopback, or Tailscale addresses, rate-limited, with its name, port, certificate fingerprint, and a short engine summary. A discovery reply is never trusted on its own; pairing still pins the certificate. The optional local subnet scan runs only when you ask for it.

### Meeting dashboard

Served over plain HTTP by the app, bound to 127.0.0.1 on a random port by default. The **LAN** option binds all interfaces so other devices can join. Access is by unguessable per-meeting host and guest links (256-bit tokens in the URL); guests see only the current meeting. Links can be regenerated.

### MCP server and the local API

The MCP server is off by default. It listens on 127.0.0.1:8767 and, only with its Tailscale option on, also on this computer's Tailscale address. The headless History API (`--api`) listens on 127.0.0.1:8766 with a token from `OPENWHISPER_API_TOKEN`. Both require a bearer token, check the `Host` header, and reject browser requests that send an `Origin` header. The MCP token is kept in the OS credential store.

Reading history and following live events are on once MCP is on; running actions, controlling recording, text filters, sending words to coding agents, and MCP Events each have their own switch, all off by default. See [MCP](docs/mcp.md) and [Agent API](docs/agent-api.md).

### Automation: webhooks, programs, and text filters

Nothing is configured by default. Webhooks are signed with Standard Webhooks HMAC-SHA256 signatures and never follow redirects. Programs run without a shell, with a time limit, and receive events as JSON on standard input; they inherit your environment minus OpenWhisper's `.env` keys and internal tokens. A text filter added over the API must listen on this computer. MCP Events callbacks must pass a signed challenge and use HTTPS (or HTTP to loopback). Restoring a backup turns webhooks, programs, filters, actions that run something, Host Mode, MCP, and the API permission switches off. See [Automation](docs/automation.md) and [Backup and restore](docs/backup-restore.md).

### Component and model downloads

The list of downloadable components is built into the app, not fetched. Every component archive and native speech model is pinned to an HTTPS URL, an exact size, and a SHA-256 digest; a download that does not match is deleted. Archives are extracted with checks that reject paths outside the target folder. Whisper models are pinned to an exact Hugging Face revision and downloaded through `huggingface_hub`. **Settings → Downloads → When a model is missing from this computer** and `HF_HUB_OFFLINE=1` control whether model downloads can happen.

### App updates

The update check asks GitHub's releases API for the latest stable release (see [Network and privacy](docs/network-and-privacy.md) for what it sends). An update downloads and installs only after you click to do so. The package is checked against the SHA-256 digest GitHub publishes for the release asset, or the release's `SHA256SUMS.txt`; without a digest the app only tells you about the update. Redirects are limited to GitHub's hosts. On macOS the app also verifies the new bundle's code signature and identity before swapping it in.

### Credentials

API keys, the MCP token, remote engine tokens, and webhook secrets are stored in the OS credential store (Windows Credential Manager, macOS Keychain, or Secret Service on Linux), never in the settings file or the log. Keys can also come from the environment or a `.env` file in the data folder. Backups never include credential-store entries, `.env`, or the Host Mode TLS key.

## Known limits

These are known and not, on their own, vulnerabilities. Reports that show a practical attack through them are still welcome.

- Anyone who can run code as your user account can read your data folder, recordings, and credential store entries.
- The meeting dashboard is plain HTTP and its access token is in the link, so anyone who sees a dashboard link can open that meeting. In LAN mode, traffic on the local network is unencrypted.
- With code pairing, a client trusts the certificate it sees first; compare the fingerprint shown on both computers.
- Release checksums come from the same GitHub release as the files, so they show the download is intact, not who published it. Windows builds are not code-signed.
- Text sent to a cloud speech or text provider you choose is handled under that provider's terms.
