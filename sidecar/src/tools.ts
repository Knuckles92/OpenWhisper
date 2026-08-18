/**
 * The Pi custom tools: patch_state, ask_question, resolve_question,
 * and the read-only search_past_meetings.
 *
 * Each tool forwards its call over JSON-RPC to the Python host
 * (tool.patch_state / tool.ask_question / tool.resolve_question), awaits the
 * validation result, and returns it verbatim to the Pi session as the tool
 * result. Rejections (human_edited, revision_mismatch, low_confidence, ...)
 * are returned as normal results so the model can self-correct within the
 * same run or at the next checkpoint.
 *
 * This is the sidecar's ENTIRE authority surface: no shell, no filesystem,
 * no network tools exist — meeting-state-only authority is structural, and
 * Python validates every op again regardless.
 */
import type { RpcEndpoint } from "./rpc";
import type { MeetingToolDef } from "./pi-adapter";

/** How long a tool bridge call may wait on the Python host. */
const TOOL_RPC_TIMEOUT_MS = 30_000;

/** Applied/rejected tallies for the current checkpoint (reset by main.ts). */
export interface OpCounters {
  applied: number;
  rejected: number;
}

/** Mutable policy for the currently serialized checkpoint. */
export interface ToolPolicy {
  polishOnly: boolean;
  /** Notes pass: only live_notes item ops may pass (add_item with
   *  card=live_notes, or update/remove targeting a known note block). */
  notesOnly: boolean;
  /** live_notes block ids visible in the current pass's state snapshot. */
  noteIds: ReadonlySet<string>;
}

const PATCH_STATE_DESCRIPTION = `Apply targeted state-patch operations to the live meeting state document. Every op is validated by the host; results are returned per-op (ok, reason, target_id, seq, current_revision) so a rejected op never blocks the rest of the batch.

Allowed ops (each op is an object with an "op" key):
- {"op":"add_item","card":C,"text":T,"data":{},"evidence":[...]} — C is one of key_points | decisions | action_items | risks | timeline | live_notes. timeline items use data.start_s; action_items use data.owner_participant_id; risks may carry data.severity; live_notes blocks carry data.heading and data.start_s. user_notes is human-only.
- {"op":"update_item","id":I,"base_revision":R,"set":{"text":...,"data":...},"evidence":[...]} — base_revision MUST equal the item's current revision from the state snapshot; mismatches are rejected with the current revision echoed back.
- {"op":"remove_item","id":I,"base_revision":R,"evidence":[...]}
- {"op":"set_topic","text":T,"evidence":[...]}
- {"op":"set_rolling_summary","text":T,"evidence":[...]}
- {"op":"upsert_participant","id":P?,"display_name":N,"kind":"others_cluster","evidence":[...]}
- {"op":"suggest_participant_name","participant_id":P,"display_name":N,"evidence":[...]}
- {"op":"revise_segment_text","segment_id":S,"text":T,"evidence":[S]} — fix clear ASR text errors without changing meaning, segment structure, or speaker.

Evidence is a list of transcript segment ids (sg_...) that support the claim; unknown ids are rejected. Items a human pinned, edited, or confirmed — and humans' participant names — can never be overwritten; such ops are rejected with reason "human_edited"/"human_named".`;

const ASK_QUESTION_DESCRIPTION = `Add a question to the meeting's quiet question inbox (it never interrupts anyone; participants answer or dismiss it when they choose). Use it for genuine open points the meeting has not settled — missing owners, unresolved decisions, ambiguous commitments. At most 7 questions may be open at once; extras are rejected with reason "question_limit".`;

const RESOLVE_QUESTION_DESCRIPTION = `Resolve an open inbox question from transcript evidence. confidence >= 0.8 marks it resolved with an "answered from audio" badge; 0.4 to 0.8 records a greyed suggested answer instead; below 0.4 is rejected with reason "low_confidence". evidence must list the transcript segment ids (sg_...) containing the answer.`;

const SEARCH_PAST_MEETINGS_DESCRIPTION = `Search earlier OpenWhisper meetings for names, decisions, or phrasing that help the current pass. Read-only: it never changes the dashboard or transcript.

Use it for unfamiliar names, "as we decided last time", recurring projects, or to disambiguate ASR during polish. Hits are CONTEXT ONLY — never copy their past:… refs or any ids they mention into evidence. Evidence must still be sg_… ids from THIS meeting's transcript.

Parameters: query (keywords), optional meeting_id (return a short slice of one past meeting), optional limit (default 10). If the user has not enabled past-meeting recall the tool says so; do not retry.`;

function summarize(results: Array<{ ok?: boolean; reason?: string | null }>): string {
  const applied = results.filter((r) => r && r.ok).length;
  const rejected = results.length - applied;
  return `${applied} applied, ${rejected} rejected`;
}

/**
 * Build the three meeting tools bound to an RPC endpoint and shared counters.
 *
 * @param rpc Endpoint used to bridge tool calls to the Python host.
 * @param counters Mutable applied/rejected tallies, reset per checkpoint.
 */
export function createMeetingTools(
  rpc: RpcEndpoint,
  counters: OpCounters,
  policy: ToolPolicy,
): MeetingToolDef[] {
  const opSchema = {
    type: "object" as const,
    description: 'A single state-patch op; must include an "op" key.',
    properties: {
      op: { type: "string" as const, description: "Op name, e.g. add_item." },
      evidence: {
        type: "array" as const,
        items: { type: "string" as const },
        minItems: 1,
        description: "At least one supporting transcript segment id.",
      },
    },
    required: ["op", "evidence"],
    additionalProperties: true,
  };

  const patchState: MeetingToolDef = {
    name: "patch_state",
    label: "Patch meeting state",
    description: PATCH_STATE_DESCRIPTION,
    parameters: {
      type: "object",
      properties: {
        ops: {
          type: "array",
          items: opSchema,
          description: "State-patch operations, applied in order, validated per-op.",
        },
      },
      required: ["ops"],
      additionalProperties: false,
    },
    execute: async (params) => {
      const requestedOps: any[] = Array.isArray(params.ops) ? params.ops : [];
      const notesAllowed = (op: any): boolean =>
        op?.op === "add_item" && op?.card === "live_notes";
      const notesTargetsKnown = (op: any): boolean =>
        (op?.op === "update_item" || op?.op === "remove_item") &&
        policy.noteIds.has(String(op?.id ?? ""));
      let allowedOps = requestedOps;
      let policyRejectedResults: Array<{ ok: false; reason: string }> = [];
      if (policy.polishOnly) {
        allowedOps = requestedOps.filter((op) => op?.op === "revise_segment_text");
        policyRejectedResults = requestedOps
          .filter((op) => op?.op !== "revise_segment_text")
          .map(() => ({ ok: false, reason: "polish_only" }));
      } else if (policy.notesOnly) {
        allowedOps = requestedOps.filter(
          (op) => notesAllowed(op) || notesTargetsKnown(op),
        );
        policyRejectedResults = requestedOps
          .filter((op) => !(notesAllowed(op) || notesTargetsKnown(op)))
          .map(() => ({ ok: false, reason: "notes_only" }));
      }
      const policyRejected = policyRejectedResults.length;
      if (policyRejected) counters.rejected += policyRejected;
      try {
        if (!allowedOps.length && policyRejected) {
          const response = { results: policyRejectedResults };
          return {
            text: `${summarize(response.results)}\n${JSON.stringify(response)}`,
            details: response,
          };
        }
        const response = await rpc.request(
          "tool.patch_state",
          { ops: allowedOps },
          TOOL_RPC_TIMEOUT_MS,
        );
        const results: any[] = Array.isArray(response?.results) ? response.results : [];
        for (const r of results) {
          if (r && r.ok) counters.applied += 1;
          else counters.rejected += 1;
        }
        const combinedResults = [...results, ...policyRejectedResults];
        const combinedResponse = { ...response, results: combinedResults };
        return {
          text: `${summarize(combinedResults)}\n${JSON.stringify(combinedResponse)}`,
          details: combinedResponse,
        };
      } catch (err) {
        counters.rejected += allowedOps.length || 1;
        const message = err instanceof Error ? err.message : String(err);
        rpc.log("error", `patch_state bridge failed: ${message}`);
        return {
          text: `Tool bridge error, no ops were applied: ${message}`,
          details: { error: message },
        };
      }
    },
  };

  const askQuestion: MeetingToolDef = {
    name: "ask_question",
    label: "Ask a question",
    description: ASK_QUESTION_DESCRIPTION,
    parameters: {
      type: "object",
      properties: {
        text: { type: "string", description: "The question, phrased for meeting participants." },
        evidence: {
          type: "array",
          items: { type: "string", description: "Transcript segment id (sg_...)." },
          minItems: 1,
          description: "Segment ids motivating the question.",
        },
      },
      required: ["text", "evidence"],
      additionalProperties: false,
    },
    execute: async (params) => {
      if (policy.polishOnly) {
        counters.rejected += 1;
        return { text: "Rejected: polish_only", details: { reason: "polish_only" } };
      }
      if (policy.notesOnly) {
        counters.rejected += 1;
        return { text: "Rejected: notes_only", details: { reason: "notes_only" } };
      }
      return bridgeSingle(rpc, counters, "tool.ask_question", {
        text: params.text ?? "",
        evidence: params.evidence ?? [],
      });
    },
  };

  const searchPastMeetings: MeetingToolDef = {
    name: "search_past_meetings",
    label: "Search past meetings",
    description: SEARCH_PAST_MEETINGS_DESCRIPTION,
    parameters: {
      type: "object",
      properties: {
        query: {
          type: "string",
          description: "Keywords to find in earlier meeting transcripts.",
        },
        meeting_id: {
          type: "string",
          description: "Optional past meeting id to fetch a short transcript slice.",
        },
        limit: {
          type: "number",
          description: "Maximum hits to return (default 10, max 20).",
        },
      },
      required: [],
      additionalProperties: false,
    },
    execute: async (params) => {
      // Read-only: allowed on polish and notes passes. Do not tally
      // applied/rejected — those counters feed the checkpoint write score.
      try {
        const response = await rpc.request(
          "tool.search_past_meetings",
          {
            query: params.query ?? "",
            meeting_id: params.meeting_id ?? "",
            limit: params.limit ?? 10,
          },
          TOOL_RPC_TIMEOUT_MS,
        );
        const text =
          typeof response?.text === "string" && response.text
            ? response.text
            : JSON.stringify(response ?? {});
        return { text, details: response };
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        rpc.log("error", `search_past_meetings bridge failed: ${message}`);
        return {
          text: `Tool bridge error: ${message}`,
          details: { error: message },
        };
      }
    },
  };

  const resolveQuestion: MeetingToolDef = {
    name: "resolve_question",
    label: "Resolve a question",
    description: RESOLVE_QUESTION_DESCRIPTION,
    parameters: {
      type: "object",
      properties: {
        question_id: { type: "string", description: "Id of the open question (q_...)." },
        answer_text: { type: "string", description: "The answer, as stated in the meeting." },
        confidence: {
          type: "number",
          description: "0..1 confidence that the transcript truly answers the question.",
        },
        evidence: {
          type: "array",
          items: { type: "string", description: "Transcript segment id (sg_...)." },
          minItems: 1,
          description: "Segment ids containing the answer.",
        },
      },
      required: ["question_id", "answer_text", "confidence", "evidence"],
      additionalProperties: false,
    },
    execute: async (params) => {
      if (policy.polishOnly) {
        counters.rejected += 1;
        return { text: "Rejected: polish_only", details: { reason: "polish_only" } };
      }
      if (policy.notesOnly) {
        counters.rejected += 1;
        return { text: "Rejected: notes_only", details: { reason: "notes_only" } };
      }
      return bridgeSingle(rpc, counters, "tool.resolve_question", {
        question_id: params.question_id ?? "",
        answer_text: params.answer_text ?? "",
        confidence: params.confidence ?? 0,
        evidence: params.evidence ?? [],
      });
    },
  };

  return [patchState, askQuestion, resolveQuestion, searchPastMeetings];
}

/** Forward a single-result tool call and tally its outcome. */
async function bridgeSingle(
  rpc: RpcEndpoint,
  counters: OpCounters,
  method: string,
  params: Record<string, unknown>,
): Promise<{ text: string; details?: unknown }> {
  try {
    const result = await rpc.request(method, params, TOOL_RPC_TIMEOUT_MS);
    if (result && result.ok) counters.applied += 1;
    else counters.rejected += 1;
    return { text: JSON.stringify(result), details: result };
  } catch (err) {
    counters.rejected += 1;
    const message = err instanceof Error ? err.message : String(err);
    rpc.log("error", `${method} bridge failed: ${message}`);
    return { text: `Tool bridge error: ${message}`, details: { error: message } };
  }
}
