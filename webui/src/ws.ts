// WebSocket client: auto-reconnect with backoff; the server replays a full
// `hello` snapshot on every (re)connect, so resync is handled by the reducer.
import type { ActionResultItem, Op, ServerMessage } from './types';

export type SocketStatus =
  | 'connecting'
  | 'open'
  | 'closed'
  | 'unauthorized'
  | 'name_required';

const PING_INTERVAL_MS = 20_000;
const MAX_BACKOFF_MS = 15_000;
const ACTION_TIMEOUT_MS = 15_000;
const GUEST_ID_KEY = 'ow_meeting_guest_id';

interface PendingAction {
  resolve: (results: ActionResultItem[]) => void;
  reject: (error: Error) => void;
  timeout: number;
}

/** Terminal close codes must not enter the automatic reconnect loop. */
export function socketStatusForCloseCode(code: number): SocketStatus | null {
  if (code === 4400) return 'name_required';
  if (code === 4401) return 'unauthorized';
  return null;
}

export function socketStatusMessage(status: SocketStatus): string {
  if (status === 'unauthorized') {
    return 'This meeting link has expired. Ask the host for a new link.';
  }
  if (status === 'name_required') {
    return 'A display name is required. Reload the page and join again.';
  }
  if (status === 'connecting') return 'Reconnecting to the meeting…';
  if (status === 'closed') return 'You are offline. Your change was not sent.';
  return '';
}

/** Action id generator; crypto.randomUUID is unavailable on plain-HTTP LAN. */
export function newActionId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `a_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * Stable per-tab guest key, so reconnecting reuses one participant instead of
 * minting a new one each time. A convenience key only — the meeting token
 * remains the sole authority. Returns '' when storage is unavailable.
 */
export function guestSessionId(): string {
  try {
    const existing = sessionStorage.getItem(GUEST_ID_KEY);
    if (existing) return existing;
    const created = newActionId().replace(/[^A-Za-z0-9_-]/g, '').slice(0, 64);
    sessionStorage.setItem(GUEST_ID_KEY, created);
    return created;
  } catch {
    return '';
  }
}

export interface MeetingSocketOptions {
  token: string;
  /** Guest display name; omit/null for the host connection. */
  name?: string | null;
  onMessage: (msg: ServerMessage) => void;
  onStatus: (status: SocketStatus) => void;
}

export class MeetingSocket {
  private opts: MeetingSocketOptions;
  private ws: WebSocket | null = null;
  private closedByUser = false;
  private attempt = 0;
  private reconnectTimer: number | null = null;
  private pingTimer: number | null = null;
  private pendingActions = new Map<string, PendingAction>();
  private terminalStatus: SocketStatus | null = null;

  constructor(opts: MeetingSocketOptions) {
    this.opts = opts;
  }

  get isOpen(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }

  connect(): void {
    if (this.closedByUser || this.terminalStatus) return;
    this.clearReconnect();
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const params = new URLSearchParams({ token: this.opts.token });
    if (this.opts.name) {
      params.set('name', this.opts.name);
      const guestId = guestSessionId();
      if (guestId) params.set('guest_id', guestId);
    }
    const ws = new WebSocket(`${proto}://${location.host}/ws?${params.toString()}`);
    this.ws = ws;
    this.opts.onStatus('connecting');

    ws.onopen = () => {
      if (ws !== this.ws) return;
      this.attempt = 0;
      this.opts.onStatus('open');
      this.startPing();
    };
    ws.onmessage = (ev: MessageEvent) => {
      if (ws !== this.ws) return;
      let msg: ServerMessage;
      try {
        msg = JSON.parse(String(ev.data)) as ServerMessage;
      } catch {
        return;
      }
      if (msg.type === 'action_result') {
        this.resolvePending(msg.client_action_id, msg.results);
      }
      this.opts.onMessage(msg);
    };
    ws.onclose = (event: CloseEvent) => {
      if (ws !== this.ws) return;
      this.ws = null;
      this.stopPing();
      const terminalStatus = socketStatusForCloseCode(event.code);
      this.rejectPending(
        terminalStatus
          ? socketStatusMessage(terminalStatus)
          : 'Connection interrupted before the server acknowledged the change.',
      );
      if (terminalStatus) {
        this.terminalStatus = terminalStatus;
        this.clearReconnect();
        this.opts.onStatus(terminalStatus);
        return;
      }
      this.opts.onStatus('closed');
      if (!this.closedByUser) this.scheduleReconnect();
    };
    ws.onerror = () => {
      /* onclose always follows */
    };
  }

  /** Send a raw protocol message; false when the socket is not open. */
  send(message: unknown): boolean {
    if (!this.isOpen || !this.ws) return false;
    try {
      this.ws.send(JSON.stringify(message));
      return true;
    } catch {
      return false;
    }
  }

  /** Send a mutation and resolve only after its server acknowledgement arrives. */
  sendAction(op: Op): Promise<ActionResultItem[]> {
    const id = newActionId();
    return this.sendTracked(id, { type: 'action', client_action_id: id, op });
  }

  /** Host-only undo, resolved only after the server acknowledges it. */
  sendUndo(seq: number): Promise<ActionResultItem[]> {
    const id = newActionId();
    return this.sendTracked(id, { type: 'undo', client_action_id: id, seq });
  }

  close(): void {
    this.closedByUser = true;
    this.clearReconnect();
    this.stopPing();
    this.rejectPending('The meeting connection was closed before the change was acknowledged.');
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      try {
        ws.close();
      } catch {
        /* already closed */
      }
    }
  }

  private scheduleReconnect(): void {
    const delay =
      Math.min(MAX_BACKOFF_MS, 1000 * 2 ** Math.min(this.attempt, 6)) + Math.random() * 400;
    this.attempt += 1;
    this.reconnectTimer = window.setTimeout(() => this.connect(), delay);
  }

  private sendTracked(
    id: string,
    message: unknown,
  ): Promise<ActionResultItem[]> {
    return new Promise((resolve, reject) => {
      if (!this.isOpen) {
        reject(new Error(socketStatusMessage(this.terminalStatus ?? 'closed')));
        return;
      }
      const timeout = window.setTimeout(() => {
        this.pendingActions.delete(id);
        reject(new Error('The server did not acknowledge the change. Check whether it applied before retrying.'));
      }, ACTION_TIMEOUT_MS);
      this.pendingActions.set(id, { resolve, reject, timeout });
      if (!this.send(message)) {
        window.clearTimeout(timeout);
        this.pendingActions.delete(id);
        reject(new Error('You are offline. Your change was not sent.'));
      }
    });
  }

  private resolvePending(id: string, results: ActionResultItem[]): void {
    const pending = this.pendingActions.get(id);
    if (!pending) return;
    window.clearTimeout(pending.timeout);
    this.pendingActions.delete(id);
    pending.resolve(results);
  }

  private rejectPending(message: string): void {
    for (const pending of this.pendingActions.values()) {
      window.clearTimeout(pending.timeout);
      pending.reject(new Error(message));
    }
    this.pendingActions.clear();
  }

  private clearReconnect(): void {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private startPing(): void {
    this.stopPing();
    this.pingTimer = window.setInterval(() => this.send({ type: 'ping' }), PING_INTERVAL_MS);
  }

  private stopPing(): void {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }
}
