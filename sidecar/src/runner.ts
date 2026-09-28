/**
 * OpenWhisper Meeting Mode sidecar entry point.
 *
 * Protocol (NDJSON JSON-RPC 2.0 over stdio; stdout is protocol-only):
 *  - FIRST line out: {"jsonrpc":"2.0","method":"hello","params":
 *      {"token":<OPENWHISPER_SIDECAR_TOKEN>,"protocol":1,"pi_version":"..."}}
 *      The hello also announces host_prompt:1 and request_scoped_tools:1;
 *      the host refuses a bundle without them.
 *  - Inbound requests: initialize {meeting_id, provider, model, system_prompt,
 *                    base_url?, api_key_env?, kind?}
 *      | checkpoint {request_id, user_prompt, system_prompt?, ...}  (the host
 *                    writes the whole prompt; a notes checkpoint carries the
 *                    note-taker system_prompt in place of the initialize one)
 *      | cancel {request_id} | ping {} | status {} | shutdown {}
 *  - Outbound tool-bridge requests: tool.patch_state / tool.ask_question /
 *      tool.resolve_question / tool.search_past_meetings /
 *      tool.search_context_files (see tools.ts), each carrying the active
 *      checkpoint's request_id.
 *  - Outbound notifications: log {level, msg} | progress {request_id, event,
 *      streaming} (Pi session hooks; host uses these to reset a stall timer).
 *
 * Checkpoints run one at a time; a checkpoint that arrives while another is
 * active waits its turn (the Python scheduler coalesces, so the queue stays
 * shallow). `cancel` aborts the active run by request_id, or pre-cancels a
 * queued one.
 */
import { RpcEndpoint } from "./rpc";
import { createMeetingTools, OpCounters } from "./tools";
import type { CreateSessionOptions, HarnessSession } from "./session";

export interface HarnessAdapter {
  harness: string;
  info(): Record<string, unknown>;
  createSession(options: CreateSessionOptions): Promise<HarnessSession>;
}

const PROTOCOL_VERSION = 1;

interface CheckpointResponse {
  applied: number;
  rejected: number;
  usage: Record<string, unknown>;
  canceled?: boolean;
}

function checkpointKind(params: any): string {
  if (params?.is_consolidation) return "consolidation";
  if (params?.is_polish) return "polish";
  if (params?.is_notes) return "note-taker";
  return "checkpoint";
}

export function startSidecar(adapter: HarnessAdapter): void {
  const token = process.env.OPENWHISPER_SIDECAR_TOKEN;
  if (!token) {
    process.stderr.write("fatal: OPENWHISPER_SIDECAR_TOKEN is not set\n");
    process.exit(1);
  }
  const apiKey = process.env.OPENWHISPER_LLM_API_KEY || "dummy";

  const rpc = new RpcEndpoint();

  // The handshake must be the first line on stdout, before the read loop can
  // possibly emit anything.
  rpc.notify("hello", { token, protocol: PROTOCOL_VERSION, ...adapter.info(), harness: adapter.harness, request_scoped_tools: 1, host_prompt: 1, text_protocols: ["chat", "responses", "anthropic", "google"] });

  let session: HarnessSession | null = null;
  let systemPrompt = "";
  let activeRequestId: string | null = null;
  let checkpointChain: Promise<unknown> = Promise.resolve();
  const canceledRequests = new Set<string>();
  const counters: OpCounters = { applied: 0, rejected: 0 };
  let lastProgressAt = 0;

  function emitProgress(info: { type: string; delta?: string; toolName?: string }): void {
    const now = Date.now();
    const frequent =
      info.delta === "thinking_delta" ||
      info.delta === "text_delta" ||
      info.delta === "toolcall_delta" ||
      info.type === "tool_execution_update";
    if (frequent && now - lastProgressAt < 2000) return;
    lastProgressAt = now;
    rpc.notify("progress", {
      request_id: activeRequestId,
      event: info.type,
      delta: info.delta,
      tool: info.toolName,
      streaming: Boolean(session?.isBusy()),
    });
  }

  rpc.onRequest("initialize", async (params) => {
    const provider = String(params?.provider || "openrouter");
    const modelId = String(params?.model || "");
    const baseUrl = String(params?.base_url || process.env.OPENWHISPER_LLM_BASE_URL || "");
    const kind = String(params?.kind || provider);
    systemPrompt = String(params?.system_prompt ?? "");
    if (!modelId) {
      throw new Error("no model configured (initialize.model is empty)");
    }
    if (session) {
      await session.dispose();
      session = null;
    }
    session = await adapter.createSession({
      provider,
      modelId,
      apiKey,
      baseUrl,
      kind,
      modelMetadata: params?.model_metadata,
      headers: params?.headers,
      tools: createMeetingTools({
        log: rpc.log.bind(rpc),
        request(method, params, timeout) {
          const requestId = activeRequestId;
          if (!requestId || canceledRequests.has(requestId)) {
            return Promise.reject(new Error("Meeting request is no longer active"));
          }
          return rpc.request(method, { ...(params as object), request_id: requestId }, timeout);
        },
      }, counters),
      log: (level, msg) => rpc.log(level, msg),
      onEvent: emitProgress,
    });
    rpc.log(
      "info",
      `initialized meeting ${String(params?.meeting_id ?? "?")} with ${provider}/${modelId}`,
    );
    return { ok: true, ...adapter.info() };
  });

  rpc.onRequest("checkpoint", (params): Promise<CheckpointResponse> => {
    // Serialize checkpoints: chain this run behind whatever is in flight.
    const receivedAt = Date.now();
    const requestId = String(params?.request_id ?? "");
    const kind = checkpointKind(params);
    const segments = Array.isArray(params?.new_segments)
      ? params.new_segments.length
      : 0;
    const queuedBehind = activeRequestId;
    rpc.log(
      "info",
      `${kind} ${requestId} received (${segments} new segments)` +
        (queuedBehind ? ` queued behind ${queuedBehind}` : ""),
    );
    const run = checkpointChain.then(
      () => runCheckpoint(params, receivedAt),
      () => runCheckpoint(params, receivedAt),
    );
    checkpointChain = run.catch(() => undefined);
    return run;
  });

  async function runCheckpoint(
    params: any,
    receivedAt: number,
  ): Promise<CheckpointResponse> {
    if (!session) {
      throw new Error("checkpoint before initialize");
    }
    const requestId = String(params?.request_id ?? "");
    if (requestId && canceledRequests.delete(requestId)) {
      rpc.log(
        "info",
        `checkpoint ${requestId} skipped (canceled while queued)`,
      );
      return { applied: 0, rejected: 0, usage: {}, canceled: true };
    }
    const startedAt = Date.now();
    const queueWaitMs = Math.max(0, startedAt - receivedAt);
    activeRequestId = requestId;
    counters.applied = 0;
    counters.rejected = 0;
    rpc.log(
      "info",
      `${checkpointKind(params)} ${requestId} started ` +
        `(${Array.isArray(params?.new_segments) ? params.new_segments.length : 0} new segments, ` +
        `queue_wait=${queueWaitMs}ms)`,
    );
    try {
      if (typeof params?.user_prompt !== "string") {
        throw new Error("checkpoint is missing the host prompt (user_prompt)");
      }
      // A notes pass swaps in the note-taker charter for this run only.
      const charter = typeof params.system_prompt === "string" ? params.system_prompt : systemPrompt;
      const turn = await session.runTurn(params.user_prompt, { requestId, systemPrompt: charter });
      const response: CheckpointResponse = {
        applied: counters.applied,
        rejected: counters.rejected,
        usage: turn.usage,
      };
      if (turn.aborted) response.canceled = true;
      rpc.log(
        "info",
        `checkpoint ${requestId} settled: ${response.applied} applied, ` +
          `${response.rejected} rejected${turn.aborted ? " (canceled)" : ""} ` +
          `queue_wait=${queueWaitMs}ms runtime=${Date.now() - startedAt}ms`,
      );
      return response;
    } finally {
      canceledRequests.delete(requestId);
      activeRequestId = null;
    }
  }

  rpc.onRequest("cancel", async (params) => {
    const requestId = String(params?.request_id ?? "");
    if (!requestId || requestId === activeRequestId) {
      if (activeRequestId) canceledRequests.add(activeRequestId);
      await session?.abort();
      return { ok: true };
    }
    // Not active: pre-cancel it in case it is queued or arrives late.
    canceledRequests.add(requestId);
    return { ok: true };
  });

  rpc.onRequest("ping", () => ({ ok: true }));

  rpc.onRequest("status", () => ({
    ok: true,
    streaming: Boolean(session?.isBusy()),
    request_id: activeRequestId,
  }));

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
    rpc.log("error", adapter.harness === "opencode" ? "OpenCode sidecar failed" : `uncaught exception: ${err?.stack ?? String(err)}`);
    process.exit(1);
  });
  process.on("unhandledRejection", (reason) => {
    rpc.log("error", adapter.harness === "opencode" ? "OpenCode sidecar failed" : `unhandled rejection: ${String(reason)}`);
    if (adapter.harness === "opencode") process.exit(1);
  });

  rpc.start();
}
