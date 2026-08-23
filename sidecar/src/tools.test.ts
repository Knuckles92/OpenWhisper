/**
 * polishOnly is the sidecar's only write filter on the Pi path — Python
 * backstops notes mode, not polish. These tests execute createMeetingTools
 * against a fake RPC host.
 */
import assert from "node:assert/strict";
import { test } from "node:test";
import type { RpcEndpoint } from "./rpc";
import {
  createMeetingTools,
  type OpCounters,
  type ToolPolicy,
} from "./tools";

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

function policy(overrides: Partial<ToolPolicy> = {}): ToolPolicy {
  return {
    polishOnly: false,
    notesOnly: false,
    noteIds: new Set(),
    ...overrides,
  };
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
  { op: "add_item", card: "key_points", text: "must not apply", evidence: ["sg_1"] },
  {
    op: "revise_segment_text",
    segment_id: "sg_1",
    text: "fixed",
    evidence: ["sg_1"],
  },
  { op: "set_topic", text: "must not apply", evidence: ["sg_1"] },
];

test("polishOnly forwards only revise_segment_text and tags the rest polish_only", async () => {
  const calls: RpcCall[] = [];
  const tally = counters();
  const tools = createMeetingTools(
    makeRpc(calls),
    tally,
    policy({ polishOnly: true }),
  );

  const result = await named(tools, "patch_state").execute({ ops: MIXED_OPS });
  const details = result.details as {
    results: Array<{ ok?: boolean; reason?: string }>;
  };

  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, "tool.patch_state");
  const forwarded = (calls[0].params as { ops: Array<{ op: string }> }).ops;
  assert.deepEqual(
    forwarded.map((op) => op.op),
    ["revise_segment_text"],
  );
  assert.equal(tally.applied, 1);
  assert.equal(tally.rejected, 2);
  const reasons = details.results.filter((row) => !row.ok).map((row) => row.reason);
  assert.deepEqual(reasons, ["polish_only", "polish_only"]);
});

test("polishOnly with no revise ops does not call the host", async () => {
  const calls: RpcCall[] = [];
  const tally = counters();
  const tools = createMeetingTools(
    makeRpc(calls),
    tally,
    policy({ polishOnly: true }),
  );

  const result = await named(tools, "patch_state").execute({
    ops: [
      { op: "add_item", card: "key_points", text: "nope", evidence: ["sg_1"] },
      { op: "set_topic", text: "nope", evidence: ["sg_1"] },
    ],
  });
  const details = result.details as {
    results: Array<{ ok?: boolean; reason?: string }>;
  };

  assert.equal(calls.length, 0);
  assert.equal(tally.applied, 0);
  assert.equal(tally.rejected, 2);
  assert.deepEqual(
    details.results.map((row) => row.reason),
    ["polish_only", "polish_only"],
  );
});

test("polishOnly rejects ask_question and resolve_question without RPC", async () => {
  const calls: RpcCall[] = [];
  const tally = counters();
  const tools = createMeetingTools(
    makeRpc(calls),
    tally,
    policy({ polishOnly: true }),
  );

  const asked = await named(tools, "ask_question").execute({
    text: "who owns this?",
    evidence: ["sg_1"],
  });
  const resolved = await named(tools, "resolve_question").execute({
    question_id: "q_1",
    answer_text: "Ada",
    confidence: 0.9,
    evidence: ["sg_1"],
  });

  assert.equal(calls.length, 0);
  assert.equal(tally.rejected, 2);
  assert.equal(tally.applied, 0);
  assert.equal((asked.details as { reason: string }).reason, "polish_only");
  assert.equal((resolved.details as { reason: string }).reason, "polish_only");
});

test("polishOnly still allows read-only search tools", async () => {
  const calls: RpcCall[] = [];
  const tools = createMeetingTools(
    makeRpc(calls, { ok: true, text: "hit" }),
    counters(),
    policy({ polishOnly: true }),
  );

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
});

test("without polishOnly mixed writes all reach the host", async () => {
  const calls: RpcCall[] = [];
  const tally = counters();
  const tools = createMeetingTools(
    makeRpc(calls, {
      results: MIXED_OPS.map(() => ({ ok: true })),
    }),
    tally,
    policy({ polishOnly: false }),
  );

  await named(tools, "patch_state").execute({ ops: MIXED_OPS });

  assert.equal(calls.length, 1);
  const forwarded = (calls[0].params as { ops: Array<{ op: string }> }).ops;
  assert.deepEqual(
    forwarded.map((op) => op.op),
    ["add_item", "revise_segment_text", "set_topic"],
  );
  assert.equal(tally.applied, 3);
  assert.equal(tally.rejected, 0);
});
