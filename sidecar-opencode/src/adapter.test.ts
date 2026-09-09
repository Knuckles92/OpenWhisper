import { afterAll, beforeAll, expect, test } from "bun:test";
import { mkdtemp, mkdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { providerConfig } from "./provider";
import type { CreateSessionOptions, HarnessSession } from "../../sidecar/src/session";

let root: string;
let createSession: typeof import("./adapter").createSession;
beforeAll(async () => {
  root = await mkdtemp(path.join(tmpdir(), "openwhisper-opencode-tests-"));
  process.env.OPENWHISPER_OPENCODE_ROOT = root;
  process.env.OPENCODE_CONFIG_DIR = root;
  process.env.OPENCODE_TEST_HOME = root;
  for (const key of ["XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "TMP", "TEMP"]) {
    process.env[key] = path.join(root, key);
    await mkdir(process.env[key]!, { recursive: true });
  }
  ({ createSession } = await import("./adapter"));
}, 30000);
afterAll(async () => { await rm(root, { force: true, recursive: true }); });

const tool = {
  name: "patch_state", label: "Patch", description: "Update meeting",
  parameters: { type: "object" as const, properties: { text: { type: "string" as const } }, required: ["text"] },
};
const base: CreateSessionOptions = {
  provider: "custom", kind: "custom", modelId: "synthetic", apiKey: "synthetic-secret",
  tools: [{ ...tool, execute: async () => ({ text: "Applied" }) }], log() {},
};
const context = { requestId: "test", systemPrompt: "Only the meeting host charter." };
function sse(chunks: any[], eventNames = false) {
  return new Response(chunks.map(x => (eventNames ? "event: " + x.type + "\n" : "") + "data: " + JSON.stringify(x) + "\n\n").join(""), { headers: { "content-type": "text/event-stream" } });
}
function reply(protocol: string, done: boolean): Response {
  if (protocol === "anthropic") {
    const content = done ? { type: "text", text: "" } : { type: "tool_use", id: "call_test", name: "patch_state", input: {} };
    return sse([
      { type: "message_start", message: { id: "msg_test", type: "message", role: "assistant", content: [], model: "synthetic", stop_reason: null, stop_sequence: null, usage: { input_tokens: 5, output_tokens: 0 } } },
      { type: "content_block_start", index: 0, content_block: content },
      { type: "content_block_delta", index: 0, delta: done ? { type: "text_delta", text: "Done" } : { type: "input_json_delta", partial_json: '{"text":"Synthetic"}' } },
      { type: "content_block_stop", index: 0 },
      { type: "message_delta", delta: { stop_reason: done ? "end_turn" : "tool_use", stop_sequence: null }, usage: { output_tokens: 5 } },
      { type: "message_stop" },
    ], true);
  }
  if (protocol === "google") return sse([{
    candidates: [{ content: { role: "model", parts: done ? [{ text: "Done" }] : [{ functionCall: { name: "patch_state", args: { text: "Synthetic" } } }] }, finishReason: "STOP", index: 0 }],
    usageMetadata: { promptTokenCount: 5, candidatesTokenCount: 5, totalTokenCount: 10 },
    modelVersion: "synthetic",
  }]);
  if (protocol === "responses") {
    const item = done ? { type: "message", id: "msg_test", role: "assistant", status: "completed", content: [{ type: "output_text", text: "Done", annotations: [] }] }
      : { type: "function_call", id: "fc_test", call_id: "call_test", name: "patch_state", arguments: '{"text":"Synthetic"}', status: "completed" };
    const response = { id: "resp_test", object: "response", created_at: 1, status: "completed", model: "synthetic", output: [item], usage: { input_tokens: 5, output_tokens: 5, total_tokens: 10 } };
    return sse([
      { type: "response.created", response: { ...response, status: "in_progress", output: [] } },
      { type: "response.output_item.added", output_index: 0, item: done ? { ...item, status: "in_progress", content: [] } : { ...item, arguments: "", status: "in_progress" } },
      done ? { type: "response.output_text.delta", item_id: "msg_test", output_index: 0, content_index: 0, delta: "Done" }
        : { type: "response.function_call_arguments.delta", item_id: "fc_test", output_index: 0, delta: '{"text":"Synthetic"}' },
      { type: "response.output_item.done", output_index: 0, item },
      { type: "response.completed", response },
    ]);
  }
  return sse([
    { id: "chatcmpl-test", object: "chat.completion.chunk", choices: [{ index: 0, delta: done ? { content: "Done" } : { tool_calls: [{ index: 0, id: "call_test", type: "function", function: { name: "patch_state", arguments: '{"text":"Synthetic"}' } }] }, finish_reason: null }] },
    { id: "chatcmpl-test", object: "chat.completion.chunk", choices: [{ index: 0, delta: {}, finish_reason: done ? "stop" : "tool_calls" }], usage: { prompt_tokens: 5, completion_tokens: 5, total_tokens: 10 } },
  ]);
}

test("provider routes preserve model capabilities and explicit budget", () => {
  for (const [kind, protocol, route] of [
    ["openai", "chat", "openai/chat"], ["custom", "chat", "openai-compatible"],
    ["openai", "responses", "openai/responses"], ["custom", "responses", "openai-compatible/responses"],
    ["custom", "anthropic", "anthropic"], ["custom", "google", "google"], ["openrouter", "chat", "openrouter"],
  ]) {
    const config = providerConfig({ ...base, kind, baseUrl: "http://localhost/prefix/v1/", modelMetadata: { protocol, context_window: 50000, max_output_tokens: 3000 } });
    expect(config.package).toBe("@opencode/ai/providers/" + route);
    expect(config.settings.baseURL).toBe("http://localhost/prefix/v1");
    expect(config.models.synthetic.limit).toEqual({ context: 50000, output: 3000 });
  }
  expect(() => providerConfig({ ...base, modelMetadata: { tools: false } })).toThrow();
  expect(() => providerConfig({ ...base, modelMetadata: { protocol: "invalid" } })).toThrow();
});

for (const protocol of ["chat", "responses", "anthropic", "google"]) {
  test(protocol + " tool round trip uses app endpoint, auth, headers, and charter", async () => {
    const seen: any[] = [];
    let applied = 0;
    const progress: any[] = [];
    const server = Bun.serve({ hostname: "127.0.0.1", port: 0, async fetch(req) {
      const body = await req.json() as any;
      seen.push({ body, headers: req.headers, path: new URL(req.url).pathname });
      return reply(protocol, applied > 0);
    } });
    let session: HarnessSession | undefined;
    try {
      session = await createSession({ ...base, baseUrl: "http://127.0.0.1:" + server.port + "/gateway/v1",
        headers: { "x-openwhisper-test": "yes" }, modelMetadata: { protocol, max_output_tokens: 3000 },
        tools: [{ ...tool, execute: async args => { expect(args.text).toBe("Synthetic"); applied++; return { text: "Applied" }; } }],
        onEvent: event => progress.push(event),
      });
      const result = await session.runTurn("Update", context);
      expect(result.aborted).toBe(false);
      expect(applied).toBe(1);
      expect(seen).toHaveLength(2);
      expect(progress.some(e => e.type === "tool_execution_start")).toBe(true);
      expect(progress.some(e => e.delta)).toBe(true);
      expect(seen[0].headers.get("x-openwhisper-test")).toBe("yes");
      expect(seen[0].path).toBe(protocol === "google" ? "/gateway/v1/models/synthetic:streamGenerateContent" : "/gateway/v1/" + ({ chat: "chat/completions", responses: "responses", anthropic: "messages" } as any)[protocol]);
      expect(seen[0].headers.get(protocol === "anthropic" ? "x-api-key" : protocol === "google" ? "x-goog-api-key" : "authorization")).toBe(protocol === "chat" || protocol === "responses" ? "Bearer synthetic-secret" : "synthetic-secret");
      const prompt = JSON.stringify(seen[0].body);
      expect(prompt).toContain(context.systemPrompt);
      expect(prompt).not.toContain("bash");
      expect(result.usage.input_tokens).toBeGreaterThan(0);
    } finally {
      await session?.dispose();
      server.stop(true);
    }
  }, 30000);
}

for (const status of [401, 429, 500]) {
  test("HTTP " + status + " fails once without leaking provider body or retrying", async () => {
    let calls = 0;
    const server = Bun.serve({ hostname: "127.0.0.1", port: 0, fetch() {
      calls++;
      return Response.json({ error: { message: "private provider diagnostic synthetic-secret", type: "api_error" } }, { status });
    } });
    const session = await createSession({ ...base, baseUrl: "http://127.0.0.1:" + server.port + "/v1" });
    try {
      await expect(session.runTurn("Update", context)).rejects.toThrow("OpenCode model turn failed");
      expect(calls).toBe(1);
      expect(session.isBusy()).toBe(false);
    } finally { await session.dispose(); server.stop(true); }
  }, 30000);
}

test("cancellation interrupts a stalled provider and next pass starts cleanly", async () => {
  let started!: () => void;
  const received = new Promise<void>(resolve => { started = resolve; });
  let stalled = true;
  const server = Bun.serve({ hostname: "127.0.0.1", port: 0, fetch() {
    started();
    if (stalled) return new Response(new ReadableStream({ start() {} }), { headers: { "content-type": "text/event-stream" } });
    return reply("chat", true);
  } });
  const session = await createSession({ ...base, baseUrl: "http://127.0.0.1:" + server.port + "/v1" });
  try {
    const turn = session.runTurn("Update", context);
    await received;
    await session.abort();
    expect((await turn).aborted).toBe(true);
    expect(session.isBusy()).toBe(false);
    stalled = false;
    expect((await session.runTurn("Next", { ...context, requestId: "next" })).aborted).toBe(false);
  } finally { await session.dispose(); server.stop(true); }
}, 30000);


test("reasoning streams report activity and preserve provider reasoning through a tool call", async () => {
  let applied = 0;
  const bodies: any[] = [];
  const progress: any[] = [];
  const server = Bun.serve({ hostname: "127.0.0.1", port: 0, async fetch(req) {
    bodies.push(await req.json());
    if (applied) return reply("chat", true);
    const frames = [
      { choices: [{ index: 0, delta: { reasoning_content: "Synthetic reasoning" }, finish_reason: null }] },
      { choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: "call_test", type: "function", function: { name: "patch_state", arguments: '{"text":"Synthetic"}' } }] }, finish_reason: null }] },
      { choices: [{ index: 0, delta: {}, finish_reason: "tool_calls" }] },
    ];
    return new Response(new ReadableStream({ async start(controller) {
      for (const frame of frames) {
        controller.enqueue(new TextEncoder().encode("data: " + JSON.stringify({ id: "chatcmpl-test", object: "chat.completion.chunk", ...frame }) + "\n\n"));
        await new Promise(resolve => setTimeout(resolve, 150));
      }
      controller.close();
    } }), { headers: { "content-type": "text/event-stream" } });
  } });
  const session = await createSession({
    ...base, baseUrl: "http://127.0.0.1:" + server.port + "/v1",
    modelMetadata: { reasoning: true, max_output_tokens: 2048,
      pi_compat: { maxTokensField: "max_tokens", requiresReasoningContentOnAssistantMessages: true },
      thinking_levels: { medium: null } },
    tools: [{ ...tool, execute: async () => { applied++; return { text: "Applied" }; } }],
    onEvent: e => progress.push(e),
  });
  try {
    await session.runTurn("Update", context);
    expect(progress.some(e => e.delta === "thinking_delta")).toBe(true);
    expect(bodies[0].max_tokens).toBe(2048);
    expect(bodies[0].reasoning_effort).toBeUndefined();
    expect(bodies[1].messages.find((m: any) => m.role === "assistant").reasoning_content).toBe("Synthetic reasoning");
  } finally { await session.dispose(); server.stop(true); }
}, 30000);

test("a canceled tool cannot emit progress into a subsequent request", async () => {
  let release!: () => void, entered!: () => void;
  const started = new Promise<void>(r => { entered = r; });
  const blocked = new Promise<void>(r => { release = r; });
  let complete = false;
  const progress: any[] = [];
  const server = Bun.serve({ hostname: "127.0.0.1", port: 0, fetch() { return reply("chat", complete); } });
  const session = await createSession({
    ...base, baseUrl: "http://127.0.0.1:" + server.port + "/v1",
    tools: [{ ...tool, execute: async () => { entered(); await blocked; return { text: "late result" }; } }],
    onEvent: e => progress.push(e),
  });
  try {
    const turn = session.runTurn("Update", context);
    await started;
    await session.abort();
    expect((await turn).aborted).toBe(true);
    complete = true;
    progress.length = 0;
    release();
    await session.runTurn("Next", { ...context, requestId: "next" });
    expect(progress.some(e => e.type === "tool_execution_end")).toBe(false);
  } finally { release(); await session.dispose(); server.stop(true); }
}, 30000);
