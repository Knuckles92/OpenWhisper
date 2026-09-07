import { test } from "node:test";
import assert from "node:assert/strict";
import { protocolConfig } from "./text-provider";

test("gateway routes match each SDK's URL construction", () => {
  const go = "https://opencode.ai/zen/go/v1";
  const zen = "https://opencode.ai/zen/v1";
  const messages = protocolConfig(go, { protocol: "anthropic" });
  assert.equal(messages.api, "anthropic-messages");
  assert.equal(messages.baseUrl + "/v1/messages", go + "/messages");
  const google = protocolConfig(zen, { protocol: "google" });
  assert.equal(google.api, "google-generative-ai");
  assert.equal(google.baseUrl + "/models/gemini:generateContent", zen + "/models/gemini:generateContent");
  assert.equal(protocolConfig(go, { protocol: "responses" }).api, "openai-responses");
  assert.equal(protocolConfig(go, { protocol: "chat" }).api, "openai-completions");
});

test("meeting snapshot budgets and signed-reasoning compatibility reach Pi", () => {
  const result = protocolConfig("https://opencode.ai/zen/v1", {
    protocol: "chat", tools: true, context_window: 1000000, max_output_tokens: 16384,
    reasoning: true, pi_compat: { requiresReasoningContentOnAssistantMessages: true },
    thinking_levels: { off: null, high: "high" },
  }, { "x-opencode-session": "meeting-1", "User-Agent": "OpenWhisper" });
  assert.equal(result.contextWindow, 1000000);
  assert.equal(result.maxTokens, 16384);
  assert.equal(result.reasoning, true);
  assert.equal(result.compat.requiresReasoningContentOnAssistantMessages, true);
  assert.deepEqual(result.thinkingLevelMap, { off: null, high: "high" });
  assert.equal(result.headers["x-opencode-session"], "meeting-1");
  assert.deepEqual(protocolConfig("http://localhost:11434/v1").headers, {});
});

test("unsupported models fail before Pi starts", () => {
  assert.throws(() => protocolConfig("http://localhost", { protocol: "invented" }), /Unsupported/);
  assert.throws(() => protocolConfig("http://localhost", { tools: false }), /meeting tools/);
});
