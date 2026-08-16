/**
 * OpenWhisper Meeting Mode sidecar entry point.
 *
 * Protocol (NDJSON JSON-RPC 2.0 over stdio; stdout is protocol-only):
 *  - FIRST line out: {"jsonrpc":"2.0","method":"hello","params":
 *      {"token":<OPENWHISPER_SIDECAR_TOKEN>,"protocol":1,"pi_version":"..."}}
 *  - Inbound requests: initialize {meeting_id, provider, model, system_prompt}
 *      | checkpoint {request_id, state, new_segments, is_consolidation,
 *                    is_polish, is_notes, system_prompt?}  (a notes
 *                    checkpoint carries the note-taker system_prompt)
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
import { createMeetingTools, OpCounters, ToolPolicy } from "./tools";
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
  const toolPolicy: ToolPolicy = {
    polishOnly: false,
    notesOnly: false,
    noteIds: new Set<string>(),
  };

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
      tools: createMeetingTools(rpc, counters, toolPolicy),
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
    const isPolish = Boolean(params?.is_polish);
    const isNotes = Boolean(params?.is_notes);
    toolPolicy.polishOnly = isPolish;
    toolPolicy.notesOnly = isNotes;
    toolPolicy.noteIds = liveNoteIds(params?.state);
    rpc.log(
      "info",
      `${isConsolidation ? "consolidation" : isPolish ? "polish" : isNotes ? "note-taker" : "checkpoint"} ` +
        `${requestId} started ` +
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
      toolPolicy.polishOnly = false;
      toolPolicy.notesOnly = false;
      toolPolicy.noteIds = new Set<string>();
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
 * live_notes block ids visible in a state snapshot (notes-pass op gate).
 */
function liveNoteIds(state: any): Set<string> {
  const ids = new Set<string>();
  const blocks = state?.cards?.live_notes;
  if (Array.isArray(blocks)) {
    for (const item of blocks) {
      if (item?.status !== "removed" && typeof item?.id === "string") {
        ids.add(item.id);
      }
    }
  }
  return ids;
}

/**
 * Bounded projection for note-taker passes: only the notes page matters.
 *
 * @param state The raw `MeetingState.to_dict()` snapshot from the host.
 * @returns topic, rolling summary, and the non-removed live_notes blocks.
 */
function notesPageProjection(state: any): any {
  const cards = state?.cards ?? {};
  const blocks = Array.isArray(cards.live_notes)
    ? cards.live_notes.filter((item: any) => item?.status !== "removed")
    : [];
  return {
    topic: state?.topic ?? {},
    rolling_summary: state?.rolling_summary ?? "",
    live_notes: blocks,
  };
}

/**
 * Compose the user message for one checkpoint run.
 *
 * The system prompt from `initialize` is prepended to the checkpoint content
 * (state snapshot, new transcript segments, consolidation flag) as a single
 * user message; the session's tool calls do the actual state mutation. Notes
 * passes carry their own note-taker system prompt from the host, which
 * replaces the copilot charter for that run.
 */
function buildCheckpointPrompt(systemPrompt: string, params: any): string {
  const isConsolidation = Boolean(params?.is_consolidation);
  const isPolish = Boolean(params?.is_polish);
  const isNotes = Boolean(params?.is_notes);
  const effectiveSystem =
    isNotes &&
    typeof params?.system_prompt === "string" &&
    params.system_prompt.trim()
      ? params.system_prompt
      : systemPrompt;
  if (isNotes) {
    return buildNotesPrompt(effectiveSystem, params);
  }

  const state = projectStateForPrompt(params?.state ?? {});
  const segments = Array.isArray(params?.new_segments) ? params.new_segments : [];

  const parts: string[] = [];
  if (effectiveSystem) {
    parts.push(effectiveSystem, "");
  }
  if (isPolish) {
    parts.push(
      "## Transcript polish pass",
      "Your only job this round is cleaning clear speech-to-text errors in the",
      "transcript below. Emit ONLY revise_segment_text operations. Keep meaning",
      "faithful; do not invent content, change speakers, merge/split segments,",
      "or touch cards, topic, summary, participants, or questions. Every revision",
      "must cite the same segment_id as evidence. Leave uncertain text unchanged.",
    );
  } else if (isConsolidation) {
    const views: string[] = Array.isArray(params?.state?.report_views)
      ? params.state.report_views
      : ["ribbon", "brief", "signal"];
    const wantRibbon = views.includes("ribbon");
    parts.push(
      "## Final consolidation pass",
      "The meeting has ended. The transcript below is the COMPLETE final transcript,",
      "and the dashboard state above includes the meeting notes (live_notes and",
      "user_notes) taken throughout the discussion.",
      "Finalize the dashboard as the durable record, actively taking into account",
      "the meeting notes alongside the complete final transcript:",
      "- Synthesize the meeting notes and transcript into the final topic and",
      "  a comprehensive rolling summary covering framing, major discussion points,",
      "  examples, decisions, and closing thesis.",
      "- Capture concrete key points, decisions, and action items (with owners)",
      "  cross-referencing commitments in the notes and transcript.",
    );
    if (wantRibbon) {
      parts.push(
        "- Populate the timeline with chronological story beats (using data.start_s).",
      );
    }
    parts.push("- Capture blockers on the risks card.");
    if (wantRibbon) {
      parts.push(
        "- Reconcile the live_notes page against this COMPLETE final transcript:",
        "  preserve accurate blocks, fix blocks that later discussion superseded,",
        "  contradicted, or clarified; merge fragments; give every block a concise",
        "  data.heading and chronological data.start_s; and remove redundant blocks.",
        "  If live_notes is empty but the meeting had speech, write the full notes",
        "  page from the complete transcript. Human-edited, confirmed, or pinned",
        "  blocks stay exactly as written — put corrections in a new block beside them.",
      );
    }
    parts.push(
      "- Keep every evidence link valid (only reference segment ids that exist).",
    );
  } else {
    parts.push(
      "## Rolling checkpoint",
      "New transcript segments have arrived since your last update. Participants",
      "are watching this live — do not wait for the meeting to end. Update the",
      "meeting state via your tools now:",
      "- If topic or rolling_summary is empty and the new segments contain real",
      "  speech, you MUST set_topic and set_rolling_summary immediately.",
      "- If key_points is empty and the speech has a concrete claim, example, or",
      "  plan, you MUST add at least one key_point.",
      "- Also capture new decisions, action items, risks, and timeline entries",
      "  when warranted; keep topic/summary current as discussion moves.",
      "- Ask or resolve inbox questions when warranted.",
      "Cite supporting segment ids as evidence on everything you add or change.",
      "Only skip tools when the dashboard already reflects this new speech.",
    );
  }
  parts.push(
    "",
    "### Current meeting state (JSON)",
    "```json",
    JSON.stringify(state, null, 2),
    "```",
    "",
    isConsolidation || isPolish
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

/**
 * Compose the user message for one note-taker pass: the notes page, the new
 * transcript segments, and the pass instructions.
 */
function buildNotesPrompt(systemPrompt: string, params: any): string {
  const page = notesPageProjection(params?.state ?? {});
  const segments = Array.isArray(params?.new_segments) ? params.new_segments : [];

  const parts: string[] = [];
  if (systemPrompt) {
    parts.push(systemPrompt, "");
  }
  parts.push(
    "## NOTE-TAKER PASS",
    "Extend the notes page from the new transcript segments:",
    "- If the newest block is still about the same subject, extend or refine it",
    "  with update_item (correct base_revision; keep its data.heading and",
    "  data.start_s).",
    "- Otherwise add a new block: fresh data.heading, data.start_s from the",
    "  earliest covering segment, and a concise professional body.",
    "- Cite evidence segment ids (sg_...) on every operation.",
    "- Never rewrite or remove human-touched blocks (edited, confirmed, pinned).",
    "A pass with meaningful new speech and zero operations is a failure — the",
    "notes page must keep up with the meeting.",
    "",
    "### Current notes page (JSON)",
    "```json",
    JSON.stringify(page, null, 2),
    "```",
    "",
    "### New transcript segments (JSON)",
    "```json",
    JSON.stringify(segments, null, 2),
    "```",
    "",
    "Make all changes through your tools now. Rejected ops return a reason —",
    "duplicate_item means you should have updated the existing block instead;",
    "human_edited means start a fresh block beside it. When you are done, reply",
    "with one short plain-text sentence summarizing what changed.",
  );
  return parts.join("\n");
}

main();
