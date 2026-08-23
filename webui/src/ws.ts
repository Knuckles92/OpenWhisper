// WebSocket client: auto-reconnect with backoff; the server replays a full
// `hello` snapshot on every (re)connect, so resync is handled by the reducer.
import type { Op, ServerMessage } from './types';

export type SocketStatus = 'connecting' | 'open' | 'closed';

const PING_INTERVAL_MS = 20_000;
const MAX_BACKOFF_MS = 15_000;
const GUEST_ID_KEY = 'ow_meeting_guest_id';

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

  constructor(opts: MeetingSocketOptions) {
    this.opts = opts;
  }

  get isOpen(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }

  connect(): void {
    if (this.closedByUser) return;
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
      this.opts.onMessage(msg);
    };
    ws.onclose = () => {
      if (ws !== this.ws) return;
      this.stopPing();
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
    this.ws.send(JSON.stringify(message));
    return true;
  }

  /** Send a mutation op; returns the client_action_id, or null when offline. */
  sendAction(op: Op): string | null {
    const id = newActionId();
    return this.send({ type: 'action', client_action_id: id, op }) ? id : null;
  }

  /** Host-only undo of the event at `seq`; returns the action id or null. */
  sendUndo(seq: number): string | null {
    const id = newActionId();
    return this.send({ type: 'undo', client_action_id: id, seq }) ? id : null;
  }

  close(): void {
    this.closedByUser = true;
    this.clearReconnect();
    this.stopPing();
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
