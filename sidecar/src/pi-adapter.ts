/**
 * Thin adapter isolating every Pi SDK touchpoint.
 *
 * The rest of the sidecar (main.ts, tools.ts) never imports the Pi packages
 * directly; it only uses the neutral types and the four entry points exported
 * here (`createSession`, plus the returned handle's `runTurn`/`abort`/
 * `dispose`, and `piVersion`). If Pi API names shift between releases, this
 * file is the only one that changes.
 *
 * Written against https://pi.dev/docs/latest/sdk and
 * https://pi.dev/docs/latest/custom-provider as of 2026-08. Points where the
 * documentation was not explicit are marked TODO(pi-api).
 */
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

// Verified against @earendil-works/pi-coding-agent@0.84.1.
import {
  createAgentSession,
  defineTool,
  ModelRuntime,
  SessionManager,
} from "@earendil-works/pi-coding-agent";
// TypeBox >= 1.0 ships as the "typebox" package (also re-exported by pi-ai).
import { Type } from "typebox";

/** Injected at bundle time by build.mjs (esbuild `define`). */
declare const __PI_VERSION__: string | undefined;

// ---------------------------------------------------------------------------
// Neutral tool-definition types (no Pi imports needed by callers)
// ---------------------------------------------------------------------------

/** JSON-schema-shaped parameter spec, compiled to TypeBox inside the adapter. */
export type ParamSpec =
  | { type: "string"; description?: string }
  | { type: "number"; description?: string }
  | { type: "boolean"; description?: string }
  | { type: "array"; items: ParamSpec; description?: string }
  | ObjectSpec;

export interface ObjectSpec {
  type: "object";
  description?: string;
  properties?: Record<string, ParamSpec>;
  required?: string[];
  additionalProperties?: boolean;
}

/** A custom tool as the sidecar defines it, independent of the Pi SDK. */
export interface MeetingToolDef {
  name: string;
  label: string;
  description: string;
  parameters: ObjectSpec;
  /** Returns the text handed back to the model plus structured details. */
  execute: (params: Record<string, any>) => Promise<{ text: string; details?: unknown }>;
}

export interface TurnResult {
  /** True when the turn ended because `abort()` was called. */
  aborted: boolean;
  /** Best-effort token usage for the turn; empty when unavailable. */
  usage: Record<string, unknown>;
}

export interface PiSession {
  /** Send one user message and run the agentic loop until it settles. */
  runTurn(userMessage: string): Promise<TurnResult>;
  /** Abort the in-flight run, if any. */
  abort(): Promise<void>;
  /** Release the session and its temp agent directory. */
  dispose(): Promise<void>;
}

export interface CreateSessionOptions {
  /** Provider id; the sidecar registers "openrouter" via models.json. */
  provider: string;
  /** Model id as understood by the provider (e.g. "anthropic/claude-sonnet-4-5"). */
  modelId: string;
  /** API key for the provider (from OPENROUTER_API_KEY). */
  apiKey?: string;
  /** The ONLY tools the session gets; built-ins are disabled structurally. */
  tools: MeetingToolDef[];
  log: (level: "debug" | "info" | "warning" | "error", msg: string) => void;
}

/** Version string reported in the hello handshake. */
export function piVersion(): string {
  return typeof __PI_VERSION__ === "string" ? __PI_VERSION__ : "dev";
}

// ---------------------------------------------------------------------------
// Schema compilation
// ---------------------------------------------------------------------------

function toTypebox(spec: ParamSpec): any {
  const opts: Record<string, unknown> = {};
  if (spec.description) opts.description = spec.description;
  switch (spec.type) {
    case "string":
      return Type.String(opts);
    case "number":
      return Type.Number(opts);
    case "boolean":
      return Type.Boolean(opts);
    case "array":
      return Type.Array(toTypebox(spec.items), opts);
    case "object": {
      const required = new Set(spec.required ?? []);
      const props: Record<string, any> = {};
      for (const [key, child] of Object.entries(spec.properties ?? {})) {
        const compiled = toTypebox(child);
        props[key] = required.has(key) ? compiled : Type.Optional(compiled);
      }
      return Type.Object(props, {
        ...opts,
        additionalProperties: spec.additionalProperties ?? false,
      });
    }
  }
}

// ---------------------------------------------------------------------------
// Provider registration (custom provider via models.json in a private agent dir)
// ---------------------------------------------------------------------------

/** OpenAI-compatible API base URL per provider id. */
const PROVIDER_BASE_URLS: Record<string, string> = {
  openrouter: "https://openrouter.ai/api/v1",
  openai: "https://api.openai.com/v1",
};

/** Environment variable each provider's key arrives in (set by the Python host). */
const PROVIDER_KEY_ENVS: Record<string, string> = {
  openrouter: "OPENROUTER_API_KEY",
  openai: "OPENAI_API_KEY",
};

/** Base URL for a provider id, defaulting to OpenRouter for unknown ids. */
export function providerBaseUrl(provider: string): string {
  return PROVIDER_BASE_URLS[provider] ?? PROVIDER_BASE_URLS.openrouter;
}

/** Key-env reference for a provider id, defaulting to OpenRouter's. */
function providerKeyEnv(provider: string): string {
  return PROVIDER_KEY_ENVS[provider] ?? PROVIDER_KEY_ENVS.openrouter;
}

async function writeAgentDir(provider: string, modelId: string): Promise<string> {
  const agentDir = await fs.mkdtemp(path.join(os.tmpdir(), "openwhisper-pi-"));
  const modelsJson = {
    providers: {
      [provider]: {
        baseUrl: providerBaseUrl(provider),
        apiKey: `$${providerKeyEnv(provider)}`,
        api: "openai-completions",
        models: [
          {
            id: modelId,
            name: modelId,
            reasoning: false,
            input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            // Conservative defaults; OpenRouter enforces real limits server-side.
            contextWindow: 131072,
            maxTokens: 16384,
          },
        ],
      },
    },
  };
  // TODO(pi-api): confirm models.json lives at <agentDir>/models.json.
  await fs.writeFile(
    path.join(agentDir, "models.json"),
    JSON.stringify(modelsJson, null, 2),
    "utf8",
  );
  return agentDir;
}

// ---------------------------------------------------------------------------
// Turn failure detection
// ---------------------------------------------------------------------------

/**
 * Detect a failed model turn on a settled session.
 *
 * `AgentSession.prompt()` resolves normally when the provider request fails:
 * the loop records an assistant message with `stopReason: "error"` and an
 * `errorMessage`, and surfaces the same text on `state.errorMessage`. Without
 * this check a dead API key, a network outage or a rate limit would be
 * reported to the Python host as a successful zero-op checkpoint, and the
 * scheduler would advance its watermark past transcript the agent never saw.
 *
 * @param session The Pi `AgentSession` after `prompt()` has settled.
 * @returns A failure message, or null when the last assistant turn is fine.
 */
function turnFailure(session: any): string | null {
  const state = session?.state;
  const messages: any[] = Array.isArray(state?.messages) ? state.messages : [];
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const message = messages[i];
    if (!message || message.role !== "assistant") continue;
    if (message.stopReason === "error") {
      return String(
        message.errorMessage ?? state?.errorMessage ?? "model turn failed",
      );
    }
    // The most recent assistant turn settled normally (stop/length/aborted).
    return null;
  }
  return String(
    state?.errorMessage ?? "model produced no assistant message",
  );
}

// ---------------------------------------------------------------------------
// Session
// ---------------------------------------------------------------------------

/**
 * Create a Pi session locked down to meeting-state authority.
 *
 * Built-in tools (shell, filesystem, network) are disabled via `noTools`, a
 * private temp `agentDir` prevents the user's global ~/.pi extensions from
 * injecting tools, and only the provided custom tools are registered.
 */
export async function createSession(opts: CreateSessionOptions): Promise<PiSession> {
  const agentDir = await writeAgentDir(opts.provider, opts.modelId);

  // Point ModelRuntime at our private agentDir so models.json (OpenRouter
  // custom provider) is picked up. getModel moved off the pi-ai root export
  // in 0.84 — resolve custom + builtin models via the runtime instead.
  const modelRuntime = await ModelRuntime.create({
    modelsPath: path.join(agentDir, "models.json"),
    authPath: path.join(agentDir, "auth.json"),
    refreshOnCreate: false,
  });
  if (opts.apiKey) {
    // Runtime override in addition to the $OPENROUTER_API_KEY env reference.
    await modelRuntime.setRuntimeApiKey(opts.provider, opts.apiKey);
  }

  const model = modelRuntime.getModel(opts.provider, opts.modelId);
  if (!model) {
    throw new Error(
      `model not found: ${opts.provider}/${opts.modelId} (check models.json in agent dir)`,
    );
  }

  const customTools = opts.tools.map((def) =>
    (defineTool as any)({
      name: def.name,
      label: def.label,
      description: def.description,
      parameters: toTypebox(def.parameters),
      execute: async (_toolCallId: string, params: Record<string, any>) => {
        const result = await def.execute(params ?? {});
        return {
          content: [{ type: "text", text: result.text }],
          details: (result.details as object) ?? {},
        };
      },
    }),
  );

  // TODO(pi-api): docs say noTools "all" disables all tools and "builtin"
  // disables only defaults; verify custom tools survive "builtin" (intended:
  // ONLY the three meeting tools are callable).
  const { session } = await (createAgentSession as any)({
    agentDir,
    model,
    modelRuntime,
    customTools,
    noTools: "builtin",
    sessionManager: (SessionManager as any).inMemory(),
  });

  let aborting = false;
  let lastUsage: Record<string, unknown> = {};

  // Best-effort usage capture; the SDK does not document a dedicated usage
  // event, so fish it out of message/agent lifecycle events when present.
  try {
    (session as any).subscribe?.((event: any) => {
      const usage = event?.message?.usage ?? event?.usage;
      if (usage && typeof usage === "object") {
        lastUsage = usage as Record<string, unknown>;
      }
    });
  } catch (err) {
    opts.log("warning", `usage subscription unavailable: ${String(err)}`);
  }

  return {
    async runTurn(userMessage: string): Promise<TurnResult> {
      aborting = false;
      lastUsage = {};
      try {
        await (session as any).prompt(userMessage);
      } catch (err) {
        if (aborting) {
          return { aborted: true, usage: lastUsage };
        }
        throw err;
      }
      if (aborting) {
        return { aborted: true, usage: lastUsage };
      }
      // prompt() does NOT reject on model errors — inspect the settled session.
      const failure = turnFailure(session);
      if (failure) {
        throw new Error(failure);
      }
      return { aborted: false, usage: lastUsage };
    },

    async abort(): Promise<void> {
      aborting = true;
      try {
        await (session as any).abort?.();
      } catch (err) {
        opts.log("warning", `abort failed: ${String(err)}`);
      }
    },

    async dispose(): Promise<void> {
      try {
        await (session as any).dispose?.();
      } catch {
        /* best effort */
      }
      try {
        await fs.rm(agentDir, { recursive: true, force: true });
      } catch {
        /* best effort */
      }
    },
  };
}
