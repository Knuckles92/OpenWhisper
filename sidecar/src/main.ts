/**
 * OpenWhisper Meeting Mode sidecar entry point.
 *
 * Protocol (NDJSON JSON-RPC 2.0 over stdio; stdout is protocol-only):
 *  - FIRST line out: {"jsonrpc":"2.0","method":"hello","params":
 *      {"token":<OPENWHISPER_SIDECAR_TOKEN>,"protocol":1,"pi_version":"..."}}
 *  - Inbound requests: initialize {meeting_id, provider, model, system_prompt}
 *      | checkpoint {request_id, state, new_segments, is_consolidation}
 *      | cancel {request_id} | ping {} | shutdown {}
 *  - Outbound tool-bridge requests: tool.patch_state / tool.ask_question /
 *      tool.resolve_question (see tools.ts).
 *  - Outbound notifications: log {level, msg}.
 *
 * Checkpoints run one at a time; a checkpoint that arrives while another is
 * active waits its turn (the Python scheduler coalesces, so the queue stays
 * shallow). `cancel` aborts the active run by request_id, or pre-cancels a
 * queued one.
 */
import { RpcEndpoint } from "./rpc";
import { createMeetingTools, OpCounters } from "./tools";
import { createSession, PiSession, piVersion } from "./pi-adapter";

const PROTOCOL_VERSION = 1;

/** Topic-history entries kept in the prompt's state block (newest last). */
const MAX_TOPIC_HISTORY = 5;

interface CheckpointResponse {
  applied: number;
  rejected: number;
  usage: Record<string, unknown>;
  canceled?: boolean;
}

function main(): void {
  const token = process.env.OPENWHISPER_SIDECAR_TOKEN;
  if (!token) {
    process.stderr.write("fatal: OPENWHISPER_SIDECAR_TOKEN is not set\n");
    process.exit(1);
  }
  const apiKey = process.env.OPENROUTER_API_KEY;
  const envModel = process.env.PI_MODEL;

  const rpc = new RpcEndpoint();

  // The handshake must be the first line on stdout, before the read loop can
  // possibly emit anything.
  rpc.notify("hello", { token, protocol: PROTOCOL_VERSION, pi_version: piVersion() });

  let session: PiSession | null = null;
  let systemPrompt = "";
  let activeRequestId: string | null = null;
  let checkpointChain: Promise<unknown> = Promise.resolve();
  const canceledRequests = new Set<string>();
  const counters: OpCounters = { applied: 0, rejected: 0 };

  rpc.onRequest("initialize", async (params) => {
    const provider = String(params?.provider || "openrouter");
    const modelId = String(params?.model || envModel || "");
    systemPrompt = String(params?.system_prompt ?? "");
    if (!modelId) {
      throw new Error("no model configured (initialize.model and PI_MODEL are both empty)");
    }
    if (session) {
      await session.dispose();
      session = null;
    }
    session = await createSession({
      provider,
      modelId,
      apiKey,
      tools: createMeetingTools(rpc, counters),
      log: (level, msg) => rpc.log(level, msg),
    });
    rpc.log(
      "info",
      `initialized meeting ${String(params?.meeting_id ?? "?")} with ${provider}/${modelId}`,
    );
    return { ok: true, pi_version: piVersion() };
  });

  rpc.onRequest("checkpoint", (params): Promise<CheckpointResponse> => {
    // Serialize checkpoints: chain this run behind whatever is in flight.
    const run = checkpointChain.then(
      () => runCheckpoint(params),
      () => runCheckpoint(params),
    );
    checkpointChain = run.catch(() => undefined);
    return run;
  });

  async function runCheckpoint(params: any): Promise<CheckpointResponse> {
    if (!session) {
      throw new Error("checkpoint before initialize");
    }
    const requestId = String(params?.request_id ?? "");
    if (requestId && canceledRequests.delete(requestId)) {
      return { applied: 0, rejected: 0, usage: {}, canceled: true };
    }
    activeRequestId = requestId;
    counters.applied = 0;
    counters.rejected = 0;
    const isConsolidation = Boolean(params?.is_consolidation);
    rpc.log(
      "info",
      `${isConsolidation ? "consolidation" : "checkpoint"} ${requestId} started ` +
        `(${Array.isArray(params?.new_segments) ? params.new_segments.length : 0} new segments)`,
    );
    try {
      const turn = await session.runTurn(buildCheckpointPrompt(systemPrompt, params));
      const response: CheckpointResponse = {
        applied: counters.applied,
        rejected: counters.rejected,
        usage: turn.usage,
      };
      if (turn.aborted) response.canceled = true;
      rpc.log(
        "info",
        `checkpoint ${requestId} settled: ${response.applied} applied, ` +
          `${response.rejected} rejected${turn.aborted ? " (canceled)" : ""}`,
      );
      return response;
    } finally {
      activeRequestId = null;
    }
  }

  rpc.onRequest("cancel", async (params) => {
    const requestId = String(params?.request_id ?? "");
    if (!requestId || requestId === activeRequestId) {
      await session?.abort();
      return { ok: true };
    }
    // Not active: pre-cancel it in case it is queued or arrives late.
    canceledRequests.add(requestId);
    return { ok: true };
  });

  rpc.onRequest("ping", () => ({ ok: true }));

  rpc.onRequest("shutdown", () => {
    // Respond first, then tear down and exit once the response has flushed.
    setTimeout(() => {
      void (async () => {
        try {
          await session?.abort();
          await session?.dispose();
        } catch {
          /* best effort */
        }
        process.exit(0);
      })();
    }, 25);
    return { ok: true };
  });

  // Host closed our stdin (crashed or killed us politely): exit.
  rpc.onClose(() => {
    process.exit(0);
  });

  // An uncaught exception leaves the session in an unknown state while `ping`
  // keeps answering {ok:true}, so the host would never notice. Exit instead and
  // let the supervisor's restart-with-backoff do its job.
  process.on("uncaughtException", (err) => {
    rpc.log("error", `uncaught exception: ${err?.stack ?? String(err)}`);
    process.exit(1);
  });
  process.on("unhandledRejection", (reason) => {
    rpc.log("error", `unhandled rejection: ${String(reason)}`);
  });

  rpc.start();
}

/**
 * Trim a raw state snapshot down to what the model actually needs.
 *
 * The raw snapshot grows monotonically over a long meeting — `topic.history`
 * gains an entry on every set_topic, removed card items are soft-deleted (never
 * dropped) and resolved/dismissed questions accumulate — so embedding it
 * verbatim on every checkpoint eventually blows the context window. The
 * projection keeps live content only: non-removed items, open questions, and
 * the last few topic-history entries.
 *
 * @param state The raw `MeetingState.to_dict()` snapshot from the host.
 * @returns A structurally identical but bounded snapshot for the prompt.
 */
function projectStateForPrompt(state: any): any {
  if (!state || typeof state !== "object") return state ?? {};
  const projected: Record<string, any> = { ...state };

  const topic = state.topic;
  if (topic && typeof topic === "object") {
    const history = Array.isArray(topic.history) ? topic.history : [];
    projected.topic = {
      ...topic,
      history: history.slice(-MAX_TOPIC_HISTORY),
    };
  }

  const cards = state.cards;
  if (cards && typeof cards === "object") {
    const liveCards: Record<string, any[]> = {};
    for (const [card, items] of Object.entries(cards)) {
      liveCards[card] = (Array.isArray(items) ? items : []).filter(
        (item: any) => item?.status !== "removed",
      );
    }
    projected.cards = liveCards;
  }

  const questions = state.questions;
  if (questions && typeof questions === "object") {
    const openQuestions: Record<string, any> = {};
    for (const [qid, question] of Object.entries(questions as Record<string, any>)) {
      if (question?.status === "open") openQuestions[qid] = question;
    }
    projected.questions = openQuestions;
  }

  return projected;
}

/**
 * Compose the user message for one checkpoint run.
 *
 * The system prompt from `initialize` is prepended to the checkpoint content
 * (state snapshot, new transcript segments, consolidation flag) as a single
 * user message; the session's tool calls do the actual state mutation.
 */
function buildCheckpointPrompt(systemPrompt: string, params: any): string {
  const isConsolidation = Boolean(params?.is_consolidation);
  const state = projectStateForPrompt(params?.state ?? {});
  const segments = Array.isArray(params?.new_segments) ? params.new_segments : [];

  const parts: string[] = [];
  if (systemPrompt) {
    parts.push(systemPrompt, "");
  }
  if (isConsolidation) {
    parts.push(
      "## Final consolidation pass",
      "The meeting has ended. The transcript below is the COMPLETE final transcript.",
      "Review the entire meeting state for accuracy and completeness: fix wrong or",
      "stale items, fill gaps, finish the rolling summary, and resolve what the",
      "audio actually answered — using your tools. Keep every evidence link valid",
      "(only reference segment ids that exist).",
    );
  } else {
    parts.push(
      "## Rolling checkpoint",
      "New transcript segments have arrived since your last update. Update the",
      "meeting state via your tools: capture new key points, decisions, action",
      "items, risks and timeline entries; keep the topic and rolling summary",
      "current; ask or resolve inbox questions when warranted. Cite supporting",
      "segment ids as evidence on everything you add or change.",
    );
  }
  parts.push(
    "",
    "### Current meeting state (JSON)",
    "```json",
    JSON.stringify(state, null, 2),
    "```",
    "",
    isConsolidation
      ? "### Complete final transcript (JSON segments)"
      : "### New transcript segments (JSON)",
    "```json",
    JSON.stringify(segments, null, 2),
    "```",
    "",
    "Make all state changes through your tools now. Rejected ops return a reason",
    "(e.g. revision_mismatch with the current revision, human_edited) — correct",
    "and retry within this run when it makes sense. When you are done, reply",
    "with one short plain-text sentence summarizing what changed.",
  );
  return parts.join("\n");
}

main();
