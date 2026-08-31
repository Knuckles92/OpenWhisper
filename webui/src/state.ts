import {
  GENERIC_CARD_KEYS,
  type AgentActivityRecord,
  type CardItem,
  type CardKey,
  type Effect,
  type HelloMsg,
  type MeetingInfo,
  type MeetingStateDoc,
  type Participant,
  type PatchMsg,
  type Question,
  type Role,
  type Segment,
  type ServerMessage,
} from './types';
import type { SocketStatus } from './ws';

/** Retained live agent ticks, matching the engine's ring buffer size. */
export const AGENT_ACTIVITY_CAP = 50;

export interface MeetingUiState {
  role: Role | null;
  participantId: string | null;
  state: MeetingStateDoc | null;
  segments: Segment[];
  meeting: MeetingInfo | null;
  guestUrl: string | null;
  socketStatus: SocketStatus;
  meetingEnded: boolean;
  lastError: string | null;
  /** Dashboard-connected guests (presence events). */
  onlineIds: Set<string>;
  /** Newest event seq seen per target id — the handle host undo acts on. */
  lastSeqByTarget: Record<string, number>;
  /** Ephemeral Pi activity ticks, newest first, host sockets only. */
  agentActivity: AgentActivityRecord[];
}

export const initialUiState: MeetingUiState = {
  role: null,
  participantId: null,
  state: null,
  segments: [],
  meeting: null,
  guestUrl: null,
  socketStatus: 'closed',
  meetingEnded: false,
  lastError: null,
  onlineIds: new Set(),
  lastSeqByTarget: {},
  agentActivity: [],
};

/** Apply a single server effect into a MeetingStateDoc copy. */
export function applyEffect(doc: MeetingStateDoc, effect: Effect): MeetingStateDoc {
  const next = { ...doc, participants: { ...doc.participants }, cards: { ...doc.cards } };

  switch (effect.entity) {
    case 'item': {
      const item = effect.item;
      const list = [...(next.cards[item.card] ?? [])];
      const idx = list.findIndex((x) => x.id === item.id);
      if (item.status === 'removed') {
        if (idx >= 0) list.splice(idx, 1);
      } else if (idx >= 0) {
        list[idx] = item;
      } else {
        list.push(item);
      }
      next.cards = { ...next.cards, [item.card]: list };
      break;
    }
    case 'topic':
      next.topic = effect.topic;
      break;
    case 'rolling_summary':
      next.rolling_summary = effect.text;
      next.rolling_summary_evidence = effect.evidence;
      break;
    case 'title':
      next.title = effect.text;
      break;
    case 'cloud_enabled':
      next.cloud_enabled = effect.enabled;
      break;
    case 'participant':
      next.participants = { ...next.participants, [effect.participant.id]: effect.participant };
      break;
    case 'question': {
      const q = effect.question;
      const questions = [...next.questions];
      const idx = questions.findIndex((x) => x.id === q.id);
      if (idx >= 0) questions[idx] = q;
      else questions.push(q);
      next.questions = questions;
      break;
    }
    default:
      break;
  }
  return next;
}

/**
 * True when a result predates the state we already hold.
 *
 * Broadcasts can arrive out of order (racing writers, buffered replay after a
 * reconnect); the meeting seq is globally monotonic, so anything at or below
 * the document's seq has already been superseded and must not be re-applied.
 */
function isStale(doc: MeetingStateDoc, seq: number | null | undefined): boolean {
  return seq != null && seq <= doc.seq;
}

function applyPatchResults(doc: MeetingStateDoc, results: PatchMsg['results']): MeetingStateDoc {
  let next = doc;
  for (const r of results) {
    if (isStale(next, r.seq)) continue;
    if (r.effect) next = applyEffect(next, r.effect);
    if (r.seq != null) next = { ...next, seq: r.seq };
  }
  return next;
}

/** Record the newest seq per target so the host can undo that exact event. */
function trackSeqs(
  known: Record<string, number>,
  results: Array<{ target_id: string | null; seq: number | null }>,
): Record<string, number> {
  let next = known;
  for (const r of results) {
    if (!r.target_id || r.seq == null) continue;
    if ((next[r.target_id] ?? -1) >= r.seq) continue;
    if (next === known) next = { ...known };
    next[r.target_id] = r.seq;
  }
  return next;
}

function mergeSegments(existing: Segment[], incoming: Segment[]): Segment[] {
  const map = new Map(existing.map((s) => [s.id, s]));
  for (const seg of incoming) map.set(seg.id, seg);
  return [...map.values()].sort((a, b) => a.start_s - b.start_s);
}

/**
 * Turn the chronological hello snapshot into the newest-first list the panel
 * renders. The host already caps its buffer; the slice guards a larger one.
 */
function seedAgentActivity(snapshot: AgentActivityRecord[]): AgentActivityRecord[] {
  return [...snapshot].reverse().slice(0, AGENT_ACTIVITY_CAP);
}

function isTerminalStatus(status: string): boolean {
  return ['ended', 'failed', 'needs_recovery'].includes(status);
}

function applySegmentSpeaker(
  segments: Segment[],
  segmentId: string,
  participantId: string | null,
  source: string,
  pinned: boolean,
): Segment[] {
  return segments.map((s) =>
    s.id === segmentId
      ? { ...s, speaker_participant_id: participantId, speaker_source: source, speaker_pinned: pinned }
      : s,
  );
}

export type UiAction =
  | { type: 'socket_status'; status: SocketStatus }
  | { type: 'server_message'; msg: ServerMessage }
  | { type: 'hydrate_segments'; segments: Segment[] }
  | { type: 'client_error'; message: string }
  | { type: 'clear_error' };

export function meetingReducer(state: MeetingUiState, action: UiAction): MeetingUiState {
  switch (action.type) {
    case 'socket_status':
      return { ...state, socketStatus: action.status };

    case 'clear_error':
      return { ...state, lastError: null };

    case 'client_error':
      return { ...state, lastError: action.message };

    case 'hydrate_segments':
      return { ...state, segments: mergeSegments(state.segments, action.segments) };

    case 'server_message': {
      const msg = action.msg;
      switch (msg.type) {
        case 'hello': {
          const h = msg as HelloMsg;
          return {
            ...state,
            role: h.role,
            participantId: h.participant_id,
            state: h.state,
            segments: mergeSegments(state.segments, h.segments),
            meeting: h.meeting,
            guestUrl: h.urls.guest ?? null,
            meetingEnded: isTerminalStatus(h.state.status),
            lastError: null,
            lastSeqByTarget: {},
            // Guest hellos omit the key entirely; keeping what we hold means a
            // reconnect never blanks the strip on a snapshot that never had it.
            agentActivity: h.agent_activity
              ? seedAgentActivity(h.agent_activity)
              : state.agentActivity,
          };
        }
        case 'patch': {
          if (!state.state) return state;
          const doc = state.state;
          const nextState = applyPatchResults(doc, msg.results);
          let nextSegments = state.segments;
          for (const r of msg.results) {
            if (isStale(doc, r.seq)) continue;
            if (r.effect?.entity === 'segment_speaker') {
              const eff = r.effect;
              nextSegments = applySegmentSpeaker(
                nextSegments,
                eff.segment_id,
                eff.participant_id,
                eff.source,
                eff.pinned,
              );
            }
            if (r.effect?.entity === 'segment_text' && r.effect.segment) {
              nextSegments = mergeSegments(nextSegments, [r.effect.segment as Segment]);
            }
          }
          return {
            ...state,
            state: nextState,
            segments: nextSegments,
            lastSeqByTarget: trackSeqs(state.lastSeqByTarget, msg.results),
          };
        }
        case 'segments': {
          const withUpserts = mergeSegments(state.segments, msg.items ?? []);
          const removed = new Set(msg.removed_ids ?? []);
          const segments = removed.size
            ? withUpserts.filter((seg) => !removed.has(seg.id))
            : withUpserts;
          return { ...state, segments };
        }
        case 'presence': {
          const online = new Set(state.onlineIds);
          if (msg.event === 'joined') online.add(msg.participant.id);
          else online.delete(msg.participant.id);
          let nextState = state.state;
          if (nextState) {
            nextState = applyEffect(nextState, {
              entity: 'participant',
              participant: msg.participant,
            });
          }
          return { ...state, onlineIds: online, state: nextState };
        }
        case 'status':
          if (!state.state) return state;
          const status = msg.status ?? state.state.status;
          const nextFinalization =
            msg.finalization !== undefined
              ? msg.finalization
              : state.state.finalization;
          return {
            ...state,
            state: {
              ...state.state,
              status,
              intelligence_online:
                msg.intelligence_online ?? state.state.intelligence_online,
              diarization_available:
                msg.diarization_available ?? state.state.diarization_available,
              capture: msg.capture ?? state.state.capture,
              finalization: nextFinalization ?? null,
            },
            meetingEnded: isTerminalStatus(status),
          };
        case 'action_result': {
          let next = {
            ...state,
            lastSeqByTarget: trackSeqs(state.lastSeqByTarget, msg.results),
          };
          for (const r of msg.results) {
            if (!r.ok) {
              next = {
                ...next,
                lastError: (r.reason || 'Action was rejected').replace(/_/g, ' '),
              };
              continue;
            }
            // Optimistic echo only: apply the effect but never advance the
            // document seq, or a fast echo would mask a peer's older patch.
            if (next.state && isStale(next.state, r.seq)) continue;
            if (r.effect && next.state) {
              next = { ...next, state: applyEffect(next.state, r.effect) };
            }
            if (r.effect?.entity === 'segment_speaker') {
              const eff = r.effect;
              next = {
                ...next,
                segments: applySegmentSpeaker(
                  next.segments,
                  eff.segment_id,
                  eff.participant_id,
                  eff.source,
                  eff.pinned,
                ),
              };
            }
          }
          return next;
        }
        case 'agent_activity': {
          const record: AgentActivityRecord = {
            kind: msg.kind,
            label: msg.label,
            tool: msg.tool,
            pass_kind: msg.pass_kind,
            ts: msg.ts,
          };
          return {
            ...state,
            agentActivity: [record, ...state.agentActivity].slice(0, AGENT_ACTIVITY_CAP),
          };
        }
        case 'error':
          return { ...state, lastError: msg.message || msg.code };
        case 'meeting_ended':
          return {
            ...state,
            meetingEnded: true,
            state: state.state
              ? { ...state.state, status: msg.status ?? 'ended' }
              : null,
          };
        default:
          return state;
      }
    }
    default:
      return state;
  }
}

/** Lookup a segment by evidence id. */
export function segmentById(segments: Segment[], id: string): Segment | undefined {
  return segments.find((s) => s.id === id);
}

/** Card items grouped and sorted: pinned first, then by updated_at desc. */
export function sortedCardItems(items: CardItem[]): CardItem[] {
  return [...items].sort((a, b) => {
    if (a.pinned !== b.pinned) return a.pinned ? -1 : 1;
    return b.updated_at.localeCompare(a.updated_at);
  });
}

/** Generic Captured cards (excludes live_notes, which live in NotesPane). */
export function flattenCapturedItems(cards: MeetingStateDoc['cards']): CardItem[] {
  const items: CardItem[] = [];
  for (const key of GENERIC_CARD_KEYS) {
    items.push(...(cards[key] ?? []));
  }
  return items;
}

export type CapturedFeedEntry =
  | { kind: 'item'; item: CardItem }
  | { kind: 'question'; question: Question };

function feedTimestamp(entry: CapturedFeedEntry): string {
  if (entry.kind === 'item') return entry.item.created_at || entry.item.updated_at;
  return entry.question.asked_at || '';
}

function feedId(entry: CapturedFeedEntry): string {
  return entry.kind === 'item' ? entry.item.id : entry.question.id;
}

/**
 * Live Captured rail: pinned cards first, then newest capture / question
 * at the top so the side column matches Conversation.
 */
export function capturedFeedEntries(
  cards: MeetingStateDoc['cards'],
  questions: Question[] = [],
): CapturedFeedEntry[] {
  const entries: CapturedFeedEntry[] = [
    ...flattenCapturedItems(cards).map((item) => ({ kind: 'item' as const, item })),
    ...questions.map((question) => ({ kind: 'question' as const, question })),
  ];
  return entries.sort((a, b) => {
    const aPinned = a.kind === 'item' && a.item.pinned;
    const bPinned = b.kind === 'item' && b.item.pinned;
    if (aPinned !== bPinned) return aPinned ? -1 : 1;
    const byTime = feedTimestamp(b).localeCompare(feedTimestamp(a));
    if (byTime !== 0) return byTime;
    return feedId(b).localeCompare(feedId(a));
  });
}

/**
 * Note-taker blocks in page order: by data.start_s when stamped, falling
 * back to created_at, oldest first — the notes page reads chronologically.
 */
export function sortedNoteItems(items: CardItem[]): CardItem[] {
  const stamp = (item: CardItem): number => {
    const startS = (item.data as { start_s?: unknown }).start_s;
    if (typeof startS === 'number' && Number.isFinite(startS)) return startS;
    const created = Date.parse(item.created_at);
    return Number.isNaN(created) ? 0 : created / 1000;
  };
  return [...items].sort((a, b) => stamp(a) - stamp(b));
}

export const CARD_LABELS: Record<CardKey, string> = {
  key_points: 'Key Points',
  decisions: 'Decisions',
  action_items: 'Action Items',
  risks: 'Risks & Disagreements',
  timeline: 'Timeline',
  live_notes: 'Meeting Notes',
  user_notes: 'Notes',
};

/** Singular card labels for per-item tags. */
export const CAPTURE_TAGS: Record<CardKey, string> = {
  key_points: 'Key point',
  decisions: 'Decision',
  action_items: 'Action',
  risks: 'Risk',
  timeline: 'Timeline',
  live_notes: 'Note',
  user_notes: 'Note',
};

export interface SpotlightPick {
  key: CardKey;
  item: CardItem;
}

function normalizeSpotlightText(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
}

function isDuplicateSpotlightText(text: string, picks: SpotlightPick[]): boolean {
  const norm = normalizeSpotlightText(text);
  if (!norm) return false;
  const words = new Set(norm.split(' ').filter(Boolean));
  for (const pick of picks) {
    const pickNorm = normalizeSpotlightText(pick.item.text);
    if (!pickNorm) continue;
    if (norm === pickNorm) return true;
    const pickWords = new Set(pickNorm.split(' ').filter(Boolean));
    if (words.size === 0 || pickWords.size === 0) continue;
    let intersection = 0;
    for (const w of words) {
      if (pickWords.has(w)) intersection++;
    }
    const unionSize = new Set([...words, ...pickWords]).size;
    if (unionSize > 0 && intersection / unionSize >= 0.6) return true;
    const minSize = Math.min(words.size, pickWords.size);
    if (minSize >= 4 && intersection / minSize >= 0.75) return true;
  }
  return false;
}

/**
 * The up-to-three card items shown in the prominent spotlight row.
 * Ranked pinned → human-touched (edited/confirmed) → most recently updated,
 * preferring one item per card category and deduplicating by text similarity;
 * repeats only fill leftover slots when distinct.
 * Note-taker blocks are excluded — they live in the dedicated NotesPane.
 */
export function selectSpotlightItems(cards: MeetingStateDoc['cards'], limit = 3): SpotlightPick[] {
  const ranked: SpotlightPick[] = [];
  for (const key of Object.keys(cards) as CardKey[]) {
    if (key === 'live_notes' || key === 'user_notes') continue;
    for (const item of cards[key] ?? []) {
      if (item.status !== 'removed') ranked.push({ key, item });
    }
  }
  ranked.sort((a, b) => {
    if (a.item.pinned !== b.item.pinned) return a.item.pinned ? -1 : 1;
    const aTouched = a.item.status === 'edited' || a.item.status === 'confirmed';
    const bTouched = b.item.status === 'edited' || b.item.status === 'confirmed';
    if (aTouched !== bTouched) return aTouched ? -1 : 1;
    return b.item.updated_at.localeCompare(a.item.updated_at);
  });

  const picks: SpotlightPick[] = [];
  const usedCategories = new Set<CardKey>();
  const usedIds = new Set<string>();
  for (const pick of ranked) {
    if (picks.length >= limit) break;
    if (usedCategories.has(pick.key) || usedIds.has(pick.item.id)) continue;
    if (isDuplicateSpotlightText(pick.item.text, picks)) continue;
    picks.push(pick);
    usedCategories.add(pick.key);
    usedIds.add(pick.item.id);
  }
  for (const pick of ranked) {
    if (picks.length >= limit) break;
    if (usedIds.has(pick.item.id)) continue;
    if (isDuplicateSpotlightText(pick.item.text, picks)) continue;
    picks.push(pick);
    usedIds.add(pick.item.id);
  }
  return picks;
}
