import type { CreateSessionOptions } from "../../sidecar/src/session";

/** Translate the app's endpoint contract, without loading OpenCode or credentials from disk. */
export function providerConfig(options: CreateSessionOptions) {
  const metadata = options.modelMetadata ?? {};
  const protocol = metadata.protocol ?? "chat";
  if (metadata.tools === false) throw new Error("This model cannot use meeting tools.");
  const kind = options.kind ?? options.provider;
  const native = kind === "openai";
  const route = kind === "openrouter" ? "openrouter" : ({
    chat: native ? "openai/chat" : "openai-compatible",
    responses: native ? "openai/responses" : "openai-compatible/responses",
    anthropic: "anthropic",
    google: "google",
  } as Record<string, string>)[protocol];
  if (!route) throw new Error("Unsupported meeting text protocol: " + protocol);
  const compat = metadata.pi_compat ?? {};
  const body: Record<string, unknown> = {};
  // Preserve transport requirements encoded in older profile metadata.
  if (protocol === "responses") body.store = false;
  const thinking = metadata.thinking_levels && Object.hasOwn(metadata.thinking_levels, "medium")
    ? metadata.thinking_levels.medium : "medium";
  if (metadata.reasoning && thinking && compat.supportsReasoningEffort !== false) {
    if (protocol === "responses") body.reasoning = { effort: thinking };
    else if (protocol === "chat" && kind !== "openrouter") body.reasoning_effort = thinking;
  }
  if (metadata.reasoning && kind === "openrouter") body.reasoning = { effort: thinking ?? "medium" };
  if (metadata.reasoning && protocol === "anthropic") {
    body.thinking = compat.forceAdaptiveThinking ? { type: "adaptive" }
      : { type: "enabled", budget_tokens: Math.min(8192, Math.max(1024, (metadata.max_output_tokens ?? 4096) - 1024)) };
  }
  if (metadata.reasoning && protocol === "google") {
    body.generationConfig = { thinkingConfig: {
      includeThoughts: true,
      ...(options.modelId.includes("gemini-3")
        ? (thinking ? { thinkingLevel: thinking.toUpperCase() } : {})
        : { thinkingBudget: Math.min(8192, Math.max(0, (metadata.max_output_tokens ?? 4096) - 1024)) }),
    } };
  }
  return {
    name: options.provider,
    package: "@opencode/ai/providers/" + route,
    settings: {
      ...(options.baseUrl ? { baseURL: options.baseUrl.replace(/\/$/, "") } : {}),
      apiKey: options.apiKey || "dummy",
    },
    headers: options.headers ?? {},
    body,
    models: {
      [options.modelId]: {
        name: options.modelId,
        modelID: options.modelId,
        capabilities: { tools: true, input: ["text"], output: ["text"] },
        compatibility: {
          ...(compat.maxTokensField ? { maxTokensField: compat.maxTokensField } : {}),
          ...(compat.requiresReasoningContentOnAssistantMessages ? { reasoningField: "reasoning_content", requireReasoning: true } : {}),
        },
        limit: { context: metadata.context_window ?? 32768, output: metadata.max_output_tokens ?? 4096 },
      },
    },
  };
}
