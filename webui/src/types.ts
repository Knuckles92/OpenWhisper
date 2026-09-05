// Wire types mirroring the Python contracts:
//   meeting/state/schema.py  (MeetingState.to_dict shapes)
//   meeting/state/patches.py (op vocabulary)
//   meeting/web/server.py    (WS + REST payloads)

export type Role = 'host' | 'guest';

export type ItemStatus = 'proposed' | 'edited' | 'confirmed' | 'removed';
export type QuestionStatus = 'open' | 'resolved' | 'dismissed';
export type ParticipantKind = 'me' | 'others_cluster' | 'guest';

export type CardKey =
  | 'key_points'
  | 'decisions'
  | 'action_items'
  | 'risks'
  | 'timeline'
  | 'live_notes'
  | 'user_notes';

export const CARD_KEYS: CardKey[] = [
  'key_points',
  'decisions',
  'action_items',
  'risks',
  'timeline',
  'live_notes',
  'user_notes',
];

/**
 * Cards rendered by the generic Captured list / composer / spotlight.
 * `live_notes` is excluded: the dedicated NotesPane owns that card.
 */
export const GENERIC_CARD_KEYS: CardKey[] = CARD_KEYS.filter(
  (key) => key !== 'live_notes',
);

export interface CardItem {
  id: string;
  card: CardKey;
  text: string;
  data: Record<string, unknown>;
  status: ItemStatus;
  author_type: string;
  author_id: string | null;
  pinned: boolean;
  revision: number;
  evidence: string[];
  created_at: string;
  updated_at: string;
}

export interface Question {
  id: string;
  text: string;
  status: QuestionStatus;
  suggested_answer: string | null;
  suggested_confidence: number | null;
  answer: string | null;
  answer_source: 'user' | 'audio' | null;
  confidence: number | null;
  thread: Array<Record<string, unknown>>;
  evidence: string[];
  asked_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
}

export interface Participant {
  id: string;
  display_name: string;
  kind: ParticipantKind;
  name_source: 'default' | 'human' | 'agent_inferred';
  is_provisional: boolean;
  created_at: string;
  updated_at: string;
}

export interface TopicRevision {
  text: string;
  ts: string;
  evidence: string[];
  actor_type: string;
}

export interface TopicState {
  current: string;
  history: TopicRevision[];
}

export type FinalizationStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'disabled'
  | 'unavailable'
  | 'failed';

export interface FinalizationStep {
  id: string;
  name: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'skipped';
  detail: string;
}

export interface FinalizationState {
  status: FinalizationStatus;
  message: string;
  stage?: string;
  current_step?: number;
  total_steps?: number;
  step_details?: string;
  steps?: FinalizationStep[];
  summary_stats?: Record<string, unknown>;
  card_deferred?: boolean;
}

export interface MeetingStateDoc {
  meeting_id: string;
  seq: number;
  status: string; // active | paused | ending | ended | failed | needs_recovery
  cloud_enabled: boolean;
  intelligence_online: boolean;
  diarization_available: boolean;
  title: string;
  topic: TopicState;
  rolling_summary: string;
  rolling_summary_evidence: string[];
  capture: {
    mic_available: boolean;
    loopback_available: boolean;
    message: string;
  };
  participants: Record<string, Participant>;
  cards: Record<CardKey, CardItem[]>;
  questions: Question[];
  /** Optional post-meeting cloud consolidation outcome. */
  finalization?: FinalizationState | null;
  /** Enabled post-meeting report views. Legacy snapshots omit this. */
  report_views?: string[];
}

export interface Segment {
  id: string;
  meeting_id: string;
  chunk_id: number | null;
  channel: string; // mic | loopback
  start_s: number;
  end_s: number;
  text: string;
  speaker_participant_id: string | null;
  speaker_source: string; // channel | diarizer | human
  speaker_pinned: boolean;
  created_at?: string;
}

export type InsightsPillTone = 'warning' | 'success' | 'neutral';

export interface MeetingContentSummary {
  has_audio: boolean;
  has_loopback_audio: boolean;
  has_transcript: boolean;
  is_empty: boolean;
  audio_chunks: number;
  transcript_segments: number;
  can_rerun_speakers: boolean;
}

export interface MeetingInfo {
  id: string;
  title: string;
  display_title?: string;
  started_at: string | null;
  status: string;
  finalization_status?: FinalizationStatus | string;
  finalization_deferred?: boolean;
  insights_pill?: string;
  insights_tone?: InsightsPillTone | string;
  content_summary?: MeetingContentSummary;
  has_audio?: boolean;
  has_transcript?: boolean;
  can_rerun_speakers?: boolean;
  [key: string]: unknown;
}

export interface Op {
  op: string;
  [key: string]: unknown;
}

export const ops = {
  addItem(
    card: CardKey,
    text: string,
    data?: Record<string, unknown>,
    evidence?: string[],
  ): Op {
    const op: Op = { op: 'add_item', card, text };
    if (data && Object.keys(data).length) op.data = data;
    if (evidence && evidence.length) op.evidence = evidence;
    return op;
  },
  updateItem(id: string, set: { text?: string; data?: Record<string, unknown> }): Op {
    return { op: 'update_item', id, set };
  },
  removeItem(id: string): Op {
    return { op: 'remove_item', id };
  },
  pinItem(id: string): Op {
    return { op: 'pin_item', id };
  },
  unpinItem(id: string): Op {
    return { op: 'unpin_item', id };
  },
  confirmItem(id: string): Op {
    return { op: 'confirm_item', id };
  },
  setTopic(text: string): Op {
    return { op: 'set_topic', text };
  },
  setTitle(text: string): Op {
    return { op: 'set_title', text };
  },
  setCloudEnabled(enabled: boolean): Op {
    return { op: 'set_cloud_enabled', enabled };
  },
  renameParticipant(participantId: string, displayName: string): Op {
    return { op: 'rename_participant', participant_id: participantId, display_name: displayName };
  },
  answerQuestion(questionId: string, answerText: string): Op {
    return { op: 'answer_question', question_id: questionId, answer_text: answerText };
  },
  dismissQuestion(questionId: string): Op {
    return { op: 'dismiss_question', question_id: questionId };
  },
  reopenQuestion(questionId: string): Op {
    return { op: 'reopen_question', question_id: questionId };
  },
  reassignSegmentSpeaker(segmentId: string, participantId: string | null): Op {
    return { op: 'reassign_segment_speaker', segment_id: segmentId, participant_id: participantId };
  },
};

export type Effect =
  | { entity: 'item'; item: CardItem }
  | { entity: 'topic'; topic: TopicState }
  | { entity: 'rolling_summary'; text: string; evidence: string[] }
  | { entity: 'title'; text: string }
  | { entity: 'cloud_enabled'; enabled: boolean }
  | { entity: 'participant'; participant: Participant }
  | { entity: 'question'; question: Question }
  | {
      entity: 'segment_speaker';
      segment_id: string;
      participant_id: string | null;
      source: string;
      pinned: boolean;
    }
  | {
      entity: 'segment_text';
      segment_id: string;
      text: string;
      segment: Segment;
    };

export interface PatchResult {
  op: Op;
  target_id: string | null;
  effect: Effect | null;
  seq: number;
}

export interface ActionResultItem {
  ok: boolean;
  reason: string | null;
  target_id: string | null;
  seq: number | null;
  effect: Effect | null;
}

/**
 * Kind of live agent tick. `update` is the host's fallback; the trailing
 * `string` keeps a kind added later on the Python side from breaking the
 * union, so rendering degrades to the default tone instead of failing.
 */
export type AgentActivityKind =
  | 'thinking'
  | 'writing'
  | 'tool'
  | 'turn'
  | 'retry'
  | 'compaction'
  | 'start'
  | 'settled'
  | 'update'
  | (string & {});

/** Agent pass that produced a tick; `''` when the host has no pass in flight. */
export type AgentActivityPassKind =
  | 'cards'
  | 'notes'
  | 'polish'
  | 'consolidation'
  | '';

/**
 * One ephemeral Pi activity tick. Never persisted, host sockets only.
 * `tool` and `pass_kind` are always present and are `''` rather than null.
 */
export interface AgentActivityRecord {
  kind: AgentActivityKind;
  /** Ready-to-display sentence — render as-is, do not rebuild from `kind`. */
  label: string;
  tool: string;
  pass_kind: AgentActivityPassKind;
  ts: string;
}

export interface AgentActivityMsg extends AgentActivityRecord {
  type: 'agent_activity';
}

export interface HelloMsg {
  type: 'hello';
  role: Role;
  participant_id: string | null;
  seq: number;
  state: MeetingStateDoc;
  segments: Segment[];
  urls: { guest?: string };
  meeting: MeetingInfo;
  /** Recent agent ticks, oldest first. Host sockets only; absent for guests. */
  agent_activity?: AgentActivityRecord[];
}

export interface PatchMsg {
  type: 'patch';
  seq: number;
  results: PatchResult[];
}

export interface SegmentsMsg {
  type: 'segments';
  items: Segment[];
  /** Segment ids removed by a rolling ASR revise pass. */
  removed_ids?: string[];
}

export interface PresenceMsg {
  type: 'presence';
  event: 'joined' | 'left';
  participant: Participant;
}

export interface StatusMsg {
  type: 'status';
  status?: string;
  intelligence_online?: boolean;
  diarization_available?: boolean;
  capture?: MeetingStateDoc['capture'];
  finalization?: FinalizationState | null;
}

export interface ActionResultMsg {
  type: 'action_result';
  client_action_id: string;
  results: ActionResultItem[];
}

export interface ErrorMsg {
  type: 'error';
  code: string;
  message: string;
}

export interface MeetingEndedMsg {
  type: 'meeting_ended';
  status?: string;
}

export interface PongMsg {
  type: 'pong';
}

export interface SpeechPreviewMsg {
  type: 'speech_preview';
  channel: string;
  text: string;
  start_s: number;
  end_s: number;
  final: boolean;
}

export type ServerMessage =
  | SpeechPreviewMsg
  | HelloMsg
  | PatchMsg
  | SegmentsMsg
  | PresenceMsg
  | StatusMsg
  | ActionResultMsg
  | AgentActivityMsg
  | ErrorMsg
  | MeetingEndedMsg
  | PongMsg;

export type ClientMessage =
  | { type: 'action'; client_action_id: string; op: Op }
  | { type: 'undo'; client_action_id: string; seq: number }
  | { type: 'ping' };

export interface SessionResponse {
  role: Role;
  meeting: MeetingInfo;
  state: MeetingStateDoc;
}

export type MeetingRow = MeetingInfo;

export interface MeetingDetailResponse {
  meeting: MeetingRow;
  state: MeetingStateDoc;
  segments: Segment[];
  transcript_next_cursor: string | null;
}

export interface TranscriptPage {
  items: Segment[];
  next_cursor: string | null;
}

export interface AuditEvent {
  seq: number;
  ts: string;
  actor_type: string;
  actor_id: string | null;
  action: string;
  target_id: string | null;
  payload?: Record<string, unknown>;
  undoable: boolean;
}

export interface RegenerateTokensResponse {
  ok: boolean;
  host_url: string;
  guest_url: string;
}

export interface RerunInsightsResponse {
  ok: boolean;
  state: MeetingStateDoc;
  applied: number;
  error: string | null;
}

export interface RerunSpeakersResponse {
  ok: boolean;
  state: MeetingStateDoc;
  applied: number;
  created?: number;
  windows?: number;
  error: string | null;
}

export type SearchRow = Record<string, unknown>;

export type ExportFormat = 'md' | 'json' | 'txt';
