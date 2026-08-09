import type {
  ActionResultItem,
  CardItem,
  CardKey,
  Effect,
  HelloMsg,
  MeetingInfo,
  MeetingStateDoc,
  Participant,
  PatchMsg,
  Role,
  Segment,
  ServerMessage,
} from './types';
import type { SocketStatus } from './ws';

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
  actionResults: Record<string, ActionResultItem[]>;
  /** Dashboard-connected guests (presence events). */
  onlineIds: Set<string>;
  /** Newest event seq seen per target id — the handle host undo acts on. */
  lastSeqByTarget: Record<string, number>;
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
  actionResults: {},
  onlineIds: new Set(),
  lastSeqByTarget: {},
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
  | { type: 'clear_error' };

export function meetingReducer(state: MeetingUiState, action: UiAction): MeetingUiState {
  switch (action.type) {
    case 'socket_status':
      return { ...state, socketStatus: action.status };

    case 'clear_error':
      return { ...state, lastError: null };

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
            segments: mergeSegments([], h.segments),
            meeting: h.meeting,
            guestUrl: h.urls.guest ?? null,
            meetingEnded: h.state.status === 'ended',
            lastError: null,
            lastSeqByTarget: {},
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
          }
          return {
            ...state,
            state: nextState,
            segments: nextSegments,
            lastSeqByTarget: trackSeqs(state.lastSeqByTarget, msg.results),
          };
        }
        case 'segments':
          return { ...state, segments: mergeSegments(state.segments, msg.items) };
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
          return {
            ...state,
            state: {
              ...state.state,
              status: msg.status,
              intelligence_online: msg.intelligence_online,
              diarization_available: msg.diarization_available,
            },
            meetingEnded: msg.status === 'ended',
          };
        case 'action_result': {
          const results = { ...state.actionResults, [msg.client_action_id]: msg.results };
          let next = {
            ...state,
            actionResults: results,
            lastSeqByTarget: trackSeqs(state.lastSeqByTarget, msg.results),
          };
          for (const r of msg.results) {
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
        case 'error':
          return { ...state, lastError: msg.message || msg.code };
        case 'meeting_ended':
          return {
            ...state,
            meetingEnded: true,
            state: state.state ? { ...state.state, status: 'ended' } : null,
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

export const CARD_LABELS: Record<CardKey, string> = {
  key_points: 'Key Points',
  decisions: 'Decisions',
  action_items: 'Action Items',
  risks: 'Risks & Disagreements',
  timeline: 'Timeline',
  user_notes: 'Notes',
};
