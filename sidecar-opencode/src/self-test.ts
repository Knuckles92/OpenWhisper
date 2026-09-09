/** Executed after extraction, from an empty cwd, with no provider credentials. */
import { createMeetingTools } from "../../sidecar/src/tools";
import { BUN_VERSION } from "./versions";

if (Bun.version !== BUN_VERSION || !process.env.OPENWHISPER_OPENCODE_ROOT) {
  throw new Error("Incorrect or unisolated runtime");
}
const { createSession } = await import("./adapter");
let calls = 0, applied = 0;
const seen: any[] = [];
const server = Bun.serve({ hostname: "127.0.0.1", port: 0, async fetch(req) {
  const body = await req.json() as any;
  seen.push(body);
  calls++;
  const done = body.messages.some((m: any) => m.role === "tool");
  const delta = done ? { content: "Done." } : {
    tool_calls: [{ index: 0, id: "call_test", type: "function", function: {
      name: "patch_state", arguments: JSON.stringify({ ops: [{ op: "set_topic", text: "Synthetic meeting", evidence: ["sg_test"] }] }),
    } }],
  };
  const chunks = [
    { choices: [{ index: 0, delta, finish_reason: null }] },
    { choices: [{ index: 0, delta: {}, finish_reason: done ? "stop" : "tool_calls" }], usage: { prompt_tokens: 5, completion_tokens: 5, total_tokens: 10 } },
  ];
  return new Response(chunks.map(x => "data: " + JSON.stringify({ id: "chatcmpl-test", object: "chat.completion.chunk", created: 1, model: "test", ...x }) + "\n\n").join("") + "data: [DONE]\n\n", { headers: { "content-type": "text/event-stream" } });
} });
const counters = { applied: 0, rejected: 0 };
let session: Awaited<ReturnType<typeof createSession>> | undefined;
try {
  session = await createSession({
    provider: "test", kind: "custom", modelId: "test", apiKey: "synthetic",
    baseUrl: "http://127.0.0.1:" + server.port + "/v1",
    modelMetadata: { protocol: "chat", tools: true },
    log() {},
    tools: createMeetingTools({
      log() {},
      async request(_method, params: any) {
        applied += params.ops.length;
        return { results: params.ops.map(() => ({ ok: true })) };
      },
    }, counters, { notesOnly: false, polishOnly: false, noteIds: new Set() }),
  });
  for (let i = 0; i < 2; i++) {
    const result = await session.runTurn("Update the meeting.", { requestId: "test-" + i, systemPrompt: "Synthetic host charter." });
    if (result.aborted || session.isBusy()) throw new Error("Turn did not settle");
  }
  if (calls !== 4 || applied !== 2 || counters.applied !== 2) throw new Error("Tool round trip failed");
  const expected = ["ask_question", "patch_state", "resolve_question", "search_context_files", "search_past_meetings"];
  for (const body of seen) {
    const names = body.tools.map((t: any) => t.function.name).sort();
    if (JSON.stringify(names) !== JSON.stringify(expected)) throw new Error("Unexpected tools exposed");
    const systems = body.messages.filter((m: any) => m.role === "system");
    if (systems.length !== 1 || systems[0].content !== "Synthetic host charter.") throw new Error("Ambient system prompt");
  }
  if (seen[2].messages.some((m: any) => m.role === "tool")) throw new Error("Session history leaked between passes");
} finally {
  await session?.dispose();
  server.stop(true);
}
console.log("OPENWHISPER_OPENCODE_OK");
