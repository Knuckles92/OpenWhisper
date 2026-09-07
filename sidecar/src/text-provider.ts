/** Protocol configuration shared by Pi registration and its offline tests. */
export interface TextModelMetadata {
  protocol?: string;
  tools?: boolean;
  context_window?: number;
  max_output_tokens?: number;
  reasoning?: boolean;
  pi_compat?: Record<string, unknown> | null;
  thinking_levels?: Record<string, string | null> | null;
}

const API_NAMES: Record<string, string> = {
  chat: "openai-completions",
  responses: "openai-responses",
  anthropic: "anthropic-messages",
  google: "google-generative-ai",
};

export function protocolConfig(
  baseUrl: string,
  metadata: TextModelMetadata = {},
  headers: Record<string, string> = {},
) {
  const protocol = metadata.protocol || "chat";
  const api = API_NAMES[protocol];
  if (!api) throw new Error("Unsupported meeting text protocol");
  if (metadata.tools === false) {
    throw new Error("This model cannot use meeting tools with the Pi engine.");
  }
  return {
    api,
    // Anthropic's SDK appends /v1/messages; Google's accepts a versioned base.
    baseUrl: protocol === "anthropic" ? baseUrl.replace(/\/v1\/?$/, "") : baseUrl,
    headers,
    contextWindow: metadata.context_window ?? 131072,
    maxTokens: metadata.max_output_tokens ?? 16384,
    reasoning: metadata.reasoning,
    compat: metadata.pi_compat ?? {},
    thinkingLevelMap: metadata.thinking_levels ?? undefined,
  };
}
