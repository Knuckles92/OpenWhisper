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

declare const __PI_VERSION__: string | undefined;

// Neutral tool-definition types

export type ParamSpec =
  | { type: "string"; description?: string }
  | { type: "number"; description?: string }
  | { type: "boolean"; description?: string }
  | { type: "array"; items: ParamSpec; description?: string; minItems?: number }
  | ObjectSpec;

export interface ObjectSpec {
  type: "object";
  description?: string;
  properties?: Record<string, ParamSpec>;
  required?: string[];
  additionalProperties?: boolean;
}

export interface MeetingToolDef {
  name: string;
  label: string;
  description: string;
  parameters: ObjectSpec;
  execute: (params: Record<string, any>) => Promise<{ text: string; details?: unknown }>;
}

export interface TurnResult {
  aborted: boolean;
  /** Best-effort token usage for the turn; empty when unavailable. */
  usage: Record<string, unknown>;
}

export interface PiSession {
  runTurn(userMessage: string): Promise<TurnResult>;
  isBusy(): boolean;
  abort(): Promise<void>;
  dispose(): Promise<void>;
}

export interface CreateSessionOptions {
  provider: string;
  modelId: string;
  apiKey?: string;
  baseUrl?: string;
  kind?: string;
  /** The ONLY tools the session gets; built-ins are disabled structurally. */
  tools: MeetingToolDef[];
  log: (level: "debug" | "info" | "warning" | "error", msg: string) => void;
  /**
   * ``AgentSession.subscribe`` listener, matching the official SDK
   * (docs/sdk.md and examples/sdk). Hosts use these events as liveness,
   * not ``isStreaming`` alone — that stays true until the run settles,
   * including a hung provider stream (pi-agent-core#2381).
   */
  onEvent?: (info: SessionProgress) => void;
}

export interface SessionProgress {
  type: string;
  delta?: string;
  toolName?: string;
}

/**
 * Map a Pi ``AgentSessionEvent`` to the documented progress kinds.
 *
 * Official hosts (examples/sdk, RPC mode) switch on ``event.type`` and, for
 * ``message_update``, on ``event.assistantMessageEvent.type``. ``thinking_delta``
 * is the liveness signal while a reasoning model is still thinking and has
 * not produced text or tool calls yet.
 */
function describeSessionEvent(event: any): SessionProgress | null {
  const type = event?.type;
  if (typeof type !== "string") return null;
  if (type === "message_update") {
    const delta = event.assistantMessageEvent?.type;
    return typeof delta === "string" ? { type, delta } : { type };
  }
  if (
    type === "tool_execution_start" ||
    type === "tool_execution_update" ||
    type === "tool_execution_end"
  ) {
    return {
      type,
      toolName: typeof event.toolName === "string" ? event.toolName : undefined,
    };
  }
  if (
    type === "agent_start" ||
    type === "agent_end" ||
    type === "agent_settled" ||
    type === "turn_start" ||
    type === "turn_end" ||
    type === "message_start" ||
    type === "message_end" ||
    type === "auto_retry_start" ||
    type === "auto_retry_end" ||
    type === "compaction_start" ||
    type === "compaction_end" ||
    type === "auto_compaction_start" ||
    type === "auto_compaction_end"
  ) {
    return { type };
  }
  return null;
}

export function piVersion(): string {
  return typeof __PI_VERSION__ === "string" ? __PI_VERSION__ : "dev";
}

// Schema compilation

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
      if (spec.minItems !== undefined) opts.minItems = spec.minItems;
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

// Provider registration

const PROVIDER_BASE_URLS: Record<string, string> = {
  openrouter: "https://openrouter.ai/api/v1",
  openai: "https://api.openai.com/v1",
};

const GENERIC_KEY_ENV = "OPENWHISPER_LLM_API_KEY";

export function providerBaseUrl(provider: string, baseUrl?: string): string {
  const explicit = (baseUrl || "").replace(/\/$/, "");
  if (explicit) return explicit;
  const known = PROVIDER_BASE_URLS[provider];
  if (known) return known;
  throw new Error(`no base URL for provider ${provider}`);
}

/**
 * Provider/model compat so Pi actually streams thinking tokens.
 *
 * Official hosts (Pi RPC, OpenClaw ``subscribeEmbeddedPiSession``) get
 * liveness from ``message_update.assistantMessageEvent.thinking_delta``.
 * That event is only emitted when the completions stream contains
 * ``reasoning`` / ``reasoning_content``. Pi's openai-completions layer
 * requests those tokens only when ``model.reasoning`` is true *and* the
 * provider ``compat.thinkingFormat`` matches the API (``openrouter`` sends
 * ``reasoning: { effort }``). Custom OpenRouter DeepSeek presets that omit
 * this fail the same way (pi#5106): long silent thinks, then tool-call
 * 400s because ``reasoning_content`` was never captured to replay.
 */
function modelCompat(kind: string, modelId: string): Record<string, unknown> {
  const compat: Record<string, unknown> = {};
  if (kind === "openrouter") {
    compat.thinkingFormat = "openrouter";
    compat.supportsDeveloperRole = false;
  }
  if (kind === "openrouter" && /deepseek-v4/i.test(modelId)) {
    compat.requiresReasoningContentOnAssistantMessages = true;
  }
  return compat;
}

async function writeAgentDir(
  provider: string,
  modelId: string,
  baseUrl?: string,
  kind?: string,
): Promise<string> {
  const agentDir = await fs.mkdtemp(path.join(os.tmpdir(), "openwhisper-pi-"));
  const resolvedKind = kind || provider;
  const deepseek = /deepseek/i.test(modelId);
  const reasoning = resolvedKind === "openrouter" || resolvedKind === "openai";
  const modelsJson = {
    providers: {
      [provider]: {
        baseUrl: providerBaseUrl(provider, baseUrl),
        apiKey: `$${GENERIC_KEY_ENV}`,
        api: "openai-completions",
        compat: modelCompat(resolvedKind, modelId),
        models: [
          {
            id: modelId,
            name: modelId,
            // Built-in cloud models think; custom endpoints default off.
            reasoning,
            input: ["text"],
            cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
            // Conservative defaults; OpenRouter enforces real limits server-side.
            contextWindow: 131072,
            maxTokens: 16384,
            // DeepSeek V4 cannot turn thinking off; do not send effort:"none".
            ...(deepseek ? { thinkingLevelMap: { off: null } } : {}),
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

// Turn failure detection

/**
 * `AgentSession.prompt()` resolves normally when the provider request fails:
 * the loop records an assistant message with `stopReason: "error"` and an
 * `errorMessage`, and surfaces the same text on `state.errorMessage`. Without
 * this check a dead API key, a network outage or a rate limit would be
 * reported to the Python host as a successful zero-op checkpoint, and the
 * scheduler would advance its watermark past transcript the agent never saw.
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

// Session

/**
 * Built-in tools (shell, filesystem, network) are disabled via `noTools`, a
 * private temp `agentDir` prevents the user's global ~/.pi extensions from
 * injecting tools, and only the provided custom tools are registered.
 */
export async function createSession(opts: CreateSessionOptions): Promise<PiSession> {
  const agentDir = await writeAgentDir(
    opts.provider,
    opts.modelId,
    opts.baseUrl,
    opts.kind,
  );
  const reasoning =
    (opts.kind || opts.provider) === "openrouter" ||
    (opts.kind || opts.provider) === "openai";

  // Point ModelRuntime at our private agentDir so models.json (OpenRouter
  // custom provider) is picked up. getModel moved off the pi-ai root export
  // in 0.84 — resolve custom + builtin models via the runtime instead.
  const modelRuntime = await ModelRuntime.create({
    modelsPath: path.join(agentDir, "models.json"),
    authPath: path.join(agentDir, "auth.json"),
    refreshOnCreate: false,
  });
  if (opts.apiKey) {
    // Runtime override in addition to the key environment reference.
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
  // disables only defaults; verify custom tools survive "builtin" so only the
  // registered meeting tools are callable.
  const { session } = await (createAgentSession as any)({
    agentDir,
    model,
    modelRuntime,
    customTools,
    noTools: "builtin",
    // Official SDK option; without it OpenRouter gets effort:"none" and
    // thinking tokens are excluded from the stream (no thinking_delta).
    ...(reasoning ? { thinkingLevel: "medium" } : {}),
    sessionManager: (SessionManager as any).inMemory(),
  });
  try {
    if (reasoning) {
      (session as any).setThinkingLevel?.("medium");
    }
  } catch {
    /* older SDKs omit the setter; createAgentSession option is enough */
  }

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
      const info = describeSessionEvent(event);
      if (info) opts.onEvent?.(info);
    });
  } catch (err) {
    opts.log("warning", `usage subscription unavailable: ${String(err)}`);
  }

  return {
    isBusy(): boolean {
      try {
        return Boolean((session as any).isStreaming);
      } catch {
        return false;
      }
    },

    async runTurn(userMessage: string): Promise<TurnResult> {
      aborting = false;
      lastUsage = {};
      try {
        if ((session as any)?.agent?.state?.messages) {
          (session as any).agent.state.messages = [];
        }
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
