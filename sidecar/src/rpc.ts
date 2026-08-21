/**
 * NDJSON JSON-RPC 2.0 endpoint over stdio.
 *
 * stdout is the protocol channel: every outbound message is exactly one JSON
 * object per line, and NOTHING else may ever be written to stdout. All
 * diagnostics go through the `log` notification (or stderr as a last resort).
 *
 * The endpoint is symmetric:
 *  - Inbound requests (Python -> sidecar: initialize/checkpoint/cancel/ping/
 *    shutdown) are dispatched to handlers registered with `onRequest`.
 *  - Outbound requests (sidecar -> Python: tool.patch_state/tool.ask_question/
 *    tool.resolve_question) are issued with `request()` and correlated back to
 *    a Promise by id. Sidecar-generated ids use the "sc-<n>" string namespace
 *    so they can never be confused with the host's ids in logs.
 *  - Notifications flow both ways (`notify()` outbound; `onNotification`
 *    inbound).
 */
import * as readline from "node:readline";

const JSONRPC = "2.0";

export const RPC_PARSE_ERROR = -32700;
export const RPC_METHOD_NOT_FOUND = -32601;
export const RPC_INTERNAL_ERROR = -32603;

export type RequestHandler = (params: any) => unknown | Promise<unknown>;
export type NotificationHandler = (params: any) => void;

export class RpcRemoteError extends Error {
  readonly code: number;
  readonly data: unknown;

  constructor(code: number, message: string, data?: unknown) {
    super(message);
    this.name = "RpcRemoteError";
    this.code = code;
    this.data = data;
  }
}

interface PendingRequest {
  resolve: (value: any) => void;
  reject: (err: Error) => void;
  timer?: ReturnType<typeof setTimeout>;
}

export class RpcEndpoint {
  private nextId = 1;
  private readonly pending = new Map<string, PendingRequest>();
  private readonly requestHandlers = new Map<string, RequestHandler>();
  private readonly notificationHandlers = new Map<string, NotificationHandler>();
  private closeHandler: (() => void) | null = null;
  private reader: readline.Interface | null = null;
  private closed = false;

  constructor(
    private readonly input: NodeJS.ReadableStream = process.stdin,
    private readonly output: NodeJS.WritableStream = process.stdout,
  ) {}

  onRequest(method: string, handler: RequestHandler): void {
    this.requestHandlers.set(method, handler);
  }

  onNotification(method: string, handler: NotificationHandler): void {
    this.notificationHandlers.set(method, handler);
  }

  onClose(cb: () => void): void {
    this.closeHandler = cb;
  }

  start(): void {
    this.output.on("error", () => {
      // stdout is gone (EPIPE): the host process died; nothing sensible left.
      process.exit(1);
    });
    this.reader = readline.createInterface({ input: this.input, terminal: false });
    this.reader.on("line", (line) => {
      void this.handleLine(line);
    });
    this.reader.on("close", () => this.handleClose());
  }

  notify(method: string, params?: unknown): void {
    this.send({ jsonrpc: JSONRPC, method, params: params ?? {} });
  }

  request(method: string, params?: unknown, timeoutMs?: number): Promise<any> {
    const id = `sc-${this.nextId++}`;
    return new Promise((resolve, reject) => {
      const entry: PendingRequest = { resolve, reject };
      if (timeoutMs && timeoutMs > 0) {
        entry.timer = setTimeout(() => {
          this.pending.delete(id);
          reject(new Error(`RPC request '${method}' timed out after ${timeoutMs}ms`));
        }, timeoutMs);
      }
      this.pending.set(id, entry);
      try {
        this.send({ jsonrpc: JSONRPC, id, method, params: params ?? {} });
      } catch (err) {
        if (entry.timer) clearTimeout(entry.timer);
        this.pending.delete(id);
        reject(err instanceof Error ? err : new Error(String(err)));
      }
    });
  }

  log(level: "debug" | "info" | "warning" | "error", msg: string): void {
    try {
      this.notify("log", { level, msg });
    } catch {
      try {
        process.stderr.write(`[sidecar:${level}] ${msg}\n`);
      } catch {
        /* nothing left to report to */
      }
    }
  }

  private send(msg: Record<string, unknown>): void {
    if (this.closed) return;
    this.output.write(JSON.stringify(msg) + "\n");
  }

  private async handleLine(line: string): Promise<void> {
    const text = line.trim();
    if (!text) return;

    let msg: any;
    try {
      msg = JSON.parse(text);
    } catch {
      this.send({
        jsonrpc: JSONRPC,
        id: null,
        error: { code: RPC_PARSE_ERROR, message: "parse error" },
      });
      this.log("error", `unparseable RPC line (${text.length} bytes)`);
      return;
    }
    if (typeof msg !== "object" || msg === null) {
      this.log("error", "RPC message is not an object; ignored");
      return;
    }

    if (typeof msg.method === "string") {
      if (Object.prototype.hasOwnProperty.call(msg, "id")) {
        await this.dispatchRequest(msg);
      } else {
        this.dispatchNotification(msg);
      }
      return;
    }

    if (Object.prototype.hasOwnProperty.call(msg, "id")) {
      this.dispatchResponse(msg);
      return;
    }
    this.log("warning", "RPC message with neither method nor id; ignored");
  }

  private async dispatchRequest(msg: any): Promise<void> {
    const handler = this.requestHandlers.get(msg.method);
    if (!handler) {
      this.send({
        jsonrpc: JSONRPC,
        id: msg.id,
        error: { code: RPC_METHOD_NOT_FOUND, message: `method not found: ${msg.method}` },
      });
      return;
    }
    try {
      const result = await handler(msg.params ?? {});
      this.send({ jsonrpc: JSONRPC, id: msg.id, result: result ?? {} });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this.send({
        jsonrpc: JSONRPC,
        id: msg.id,
        error: { code: RPC_INTERNAL_ERROR, message },
      });
      this.log("error", `request '${msg.method}' failed: ${message}`);
    }
  }

  private dispatchNotification(msg: any): void {
    const handler = this.notificationHandlers.get(msg.method);
    if (!handler) return; // unknown notifications are ignored by design
    try {
      handler(msg.params ?? {});
    } catch (err) {
      this.log("error", `notification '${msg.method}' handler failed: ${String(err)}`);
    }
  }

  private dispatchResponse(msg: any): void {
    const key = String(msg.id);
    const entry = this.pending.get(key);
    if (!entry) {
      this.log("warning", `response for unknown request id ${key}; dropped`);
      return;
    }
    this.pending.delete(key);
    if (entry.timer) clearTimeout(entry.timer);
    if (Object.prototype.hasOwnProperty.call(msg, "error") && msg.error) {
      const e = msg.error;
      entry.reject(
        new RpcRemoteError(
          typeof e.code === "number" ? e.code : RPC_INTERNAL_ERROR,
          typeof e.message === "string" ? e.message : "remote error",
          e.data,
        ),
      );
    } else {
      entry.resolve(msg.result);
    }
  }

  private handleClose(): void {
    if (this.closed) return;
    this.closed = true;
    for (const [, entry] of this.pending) {
      if (entry.timer) clearTimeout(entry.timer);
      entry.reject(new Error("stdin closed"));
    }
    this.pending.clear();
    if (this.closeHandler) this.closeHandler();
  }
}
