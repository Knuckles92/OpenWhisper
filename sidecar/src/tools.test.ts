/**
 * The meeting tools forward every call to the host and report its verdicts.
 * Pass restrictions (polish_only, notes_only), question limits and
 * confidence thresholds are the host's; nothing is filtered here. These tests
 * execute createMeetingTools against a fake RPC host.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import type { RpcEndpoint } from "./rpc";
import { createMeetingTools, type OpCounters } from "./tools";

type RpcCall = { method: string; params: unknown };

function makeRpc(
  calls: RpcCall[],
  response: unknown = { results: [{ ok: true }] },
): RpcEndpoint {
  return {
    request: async (method: string, params?: unknown) => {
      calls.push({ method, params });
      return response;
    },
    log: () => {},
  } as unknown as RpcEndpoint;
}

function failingRpc(calls: RpcCall[]): RpcEndpoint {
  return {
    request: async (method: string, params?: unknown) => {
      calls.push({ method, params });
      throw new Error("Meeting request is no longer active");
    },
    log: () => {},
  } as unknown as RpcEndpoint;
}

function counters(): OpCounters {
  return { applied: 0, rejected: 0 };
}

function named(
  tools: ReturnType<typeof createMeetingTools>,
  name: string,
) {
  const found = tools.find((item) => item.name === name);
  assert.ok(found, `missing tool ${name}`);
  return found;
}

const MIXED_OPS = [
  { op: "add_item", card: "key_points", text: "off-pass", evidence: ["sg_1"] },
  {
    op: "revise_segment_text",
    segment_id: "sg_1",
    text: "fixed",
    evidence: ["sg_1"],
  },
  { op: "set_topic", text: "off-pass", evidence: ["sg_1"] },
];

test("patch_state forwards every op and reports the host's rejections", async () => {
  const calls: RpcCall[] = [];
  const tally = counters();
  const verdicts = {
    results: [
      { ok: false, reason: "polish_only" },
      { ok: true },
      { ok: false, reason: "polish_only" },
    ],
  };
  const tools = createMeetingTools(makeRpc(calls, verdicts), tally);

  const result = await named(tools, "patch_state").execute({ ops: MIXED_OPS });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "tool.patch_state");
  const forwarded = (calls[0].params as { ops: Array<{ op: string }> }).ops;
  assert.deepEqual(
    forwarded.map((op) => op.op),
    ["add_item", "revise_segment_text", "set_topic"],
  );
  assert.equal(tally.applied, 1);
  assert.equal(tally.rejected, 2);
  assert.deepEqual(result.details, verdicts);
  assert.match(result.text, /^1 applied, 2 rejected\n/);
  assert.match(result.text, /polish_only/);
});

test("question tools forward to the host and tally its verdict", async () => {
  const calls: RpcCall[] = [];
  const tally = counters();
  const tools = createMeetingTools(
    makeRpc(calls, { ok: false, reason: "notes_only" }),
    tally,
  );

  const asked = await named(tools, "ask_question").execute({
    text: "who owns this?",
    evidence: ["sg_1"],
  });
  await named(tools, "resolve_question").execute({
    question_id: "q_1",
    answer_text: "Ada",
    confidence: 0.9,
    evidence: ["sg_1"],
  });

  assert.deepEqual(
    calls.map((call) => call.method),
    ["tool.ask_question", "tool.resolve_question"],
  );
  assert.deepEqual(calls[1].params, {
    question_id: "q_1",
    answer_text: "Ada",
    confidence: 0.9,
    evidence: ["sg_1"],
  });
  assert.equal(tally.applied, 0);
  assert.equal(tally.rejected, 2);
  assert.equal((asked.details as { reason: string }).reason, "notes_only");
});

test("read-only search tools return the host's text and never tally", async () => {
  const calls: RpcCall[] = [];
  const tally = counters();
  const tools = createMeetingTools(makeRpc(calls, { ok: true, text: "hit" }), tally);

  const recalled = await named(tools, "search_past_meetings").execute({
    query: "budget",
  });
  const folder = await named(tools, "search_context_files").execute({
    query: "roadmap",
  });

  assert.deepEqual(
    calls.map((call) => call.method),
    ["tool.search_past_meetings", "tool.search_context_files"],
  );
  assert.equal(recalled.text, "hit");
  assert.equal(folder.text, "hit");
  assert.deepEqual(tally, { applied: 0, rejected: 0 });
});

test("a failed bridge call counts every op as rejected", async () => {
  const calls: RpcCall[] = [];
  const tally = counters();
  const tools = createMeetingTools(failingRpc(calls), tally);

  const result = await named(tools, "patch_state").execute({ ops: MIXED_OPS });

  assert.equal(calls.length, 1);
  assert.equal(tally.rejected, MIXED_OPS.length);
  assert.match(result.text, /no ops were applied/);
});

test("tool descriptions leave host limits to the host", () => {
  const tools = createMeetingTools(makeRpc([]), counters());
  for (const name of ["ask_question", "resolve_question"]) {
    const { description } = named(tools, name);
    assert.doesNotMatch(description, /\b0\.[48]\b|\bAt most \d/);
  }
});
