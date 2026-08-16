import { sortedCardItems } from './state';
import type { CardItem, MeetingStateDoc, Participant, Segment } from './types';

export const DEFAULT_REPORT_VIEWS = ['ribbon', 'brief', 'signal'] as const;
export type ReportViewId = (typeof DEFAULT_REPORT_VIEWS)[number];

const SEVERITY_RANK: Record<string, number> = { high: 0, medium: 1, low: 2 };
const SPEAKER_PALETTE = ['#2f6b4f', '#a2603c', '#4a6b8a', '#8a7340', '#6b4a7a', '#3c6b6b'];

/** Return the views this meeting recorded, defaulting to all three. */
export function enabledReportViews(state: MeetingStateDoc): ReportViewId[] {
  const allowed = new Set<string>(DEFAULT_REPORT_VIEWS);
  const raw = Array.isArray(state.report_views) ? state.report_views : DEFAULT_REPORT_VIEWS;
  const views = raw.filter((view): view is ReportViewId => allowed.has(view));
  return views.length ? views : ['ribbon'];
}

export function liveItems(items: CardItem[] | undefined): CardItem[] {
  return (items ?? []).filter((item) => item.status !== 'removed');
}

export function segmentMap(segments: Segment[]): Map<string, Segment> {
  return new Map(segments.map((segment) => [segment.id, segment]));
}

/** Format meeting-clock seconds as `M:SS`, rounded. */
export function clock(seconds: number): string {
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  return `${minutes}:${String(total % 60).padStart(2, '0')}`;
}

/** Item time: explicit `data.start_s`, else the earliest cited segment. */
export function itemTime(item: CardItem, segs: Map<string, Segment>): number | null {
  const startS = item.data.start_s;
  if (typeof startS === 'number' && Number.isFinite(startS)) return startS;
  const times = (item.evidence || [])
    .map((id) => segs.get(id)?.start_s)
    .filter((value): value is number => typeof value === 'number');
  return times.length ? Math.min(...times) : null;
}

export function ownerId(item: CardItem): string | null {
  const raw = item.data.owner_participant_id;
  return typeof raw === 'string' && raw ? raw : null;
}

export function severity(item: CardItem): string | null {
  const raw = item.data.severity;
  return typeof raw === 'string' && raw ? raw : null;
}

export function speakerName(
  participants: Record<string, Participant> | Participant[],
  participantId: string | null,
  channel = '',
): string {
  if (participantId) {
    if (Array.isArray(participants)) {
      const match = participants.find((person) => person.id === participantId);
      if (match) return match.display_name;
    } else if (participants[participantId]) {
      return participants[participantId].display_name;
    }
  }
  if (channel === 'mic') return 'Me';
  if (channel === 'loopback') return 'Others';
  return 'Unknown';
}

/** Max segment `end_s`, matching the Markdown exporter's duration rule. */
export function meetingDuration(segments: Segment[]): number {
  return segments.reduce((max, segment) => Math.max(max, segment.end_s || 0), 0);
}

function clipWords(text: string, maxWords: number): string {
  const words = text.trim().split(/\s+/).filter(Boolean);
  if (words.length <= maxWords) return text.trim();
  return `${words.slice(0, maxWords).join(' ')}…`;
}

export function deriveSignalHeadline(state: MeetingStateDoc): string {
  const decisions = sortedCardItems(liveItems(state.cards.decisions));
  if (decisions[0]?.text) return clipWords(decisions[0].text, 14);
  if (state.topic?.current) return clipWords(state.topic.current, 14);
  return state.title || 'Meeting recap';
}

export function deriveSignalStandfirst(state: MeetingStateDoc): string {
  const summary = (state.rolling_summary || '').trim();
  if (!summary) return '';
  const sentences = summary.match(/[^.!?]+[.!?]+|[^.!?]+$/g) ?? [summary];
  return sentences.slice(0, 2).join(' ').trim();
}

export function deriveListenPicks(
  state: MeetingStateDoc,
  segs: Map<string, Segment>,
): Segment[] {
  const firstEvidence = (item: CardItem | undefined): Segment | undefined => {
    if (!item) return undefined;
    for (const id of item.evidence || []) {
      const segment = segs.get(id);
      if (segment) return segment;
    }
    return undefined;
  };
  const decisions = sortedCardItems(liveItems(state.cards.decisions));
  const risks = liveItems(state.cards.risks).sort(
    (left, right) =>
      (SEVERITY_RANK[severity(left) || ''] ?? 3) -
      (SEVERITY_RANK[severity(right) || ''] ?? 3),
  );
  const actions = liveItems(state.cards.action_items);
  const picks: Segment[] = [];
  const seen = new Set<string>();
  for (const item of [decisions[0], risks[0], actions[0]]) {
    const segment = firstEvidence(item);
    if (segment && !seen.has(segment.id)) {
      seen.add(segment.id);
      picks.push(segment);
    }
  }
  return picks;
}

export function speakerColor(participantId: string | null): string {
  if (!participantId) return SPEAKER_PALETTE[0];
  let hash = 0;
  for (let index = 0; index < participantId.length; index += 1) {
    hash = (hash * 31 + participantId.charCodeAt(index)) >>> 0;
  }
  return SPEAKER_PALETTE[hash % SPEAKER_PALETTE.length];
}

export function formatMeetingWhen(
  startedAt?: string | null,
  durationS?: number,
): string {
  const parts: string[] = [];
  if (startedAt) {
    const date = new Date(startedAt);
    if (!Number.isNaN(date.getTime())) {
      parts.push(
        date.toLocaleDateString(undefined, {
          month: 'short',
          day: 'numeric',
          year: 'numeric',
        }),
      );
    }
  }
  if (typeof durationS === 'number' && durationS > 0) {
    parts.push(`${Math.max(1, Math.round(durationS / 60))} min`);
  }
  return parts.join(' · ');
}

export function splitSummary(summary: string): { lede: string; rest: string[] } {
  const paras = summary.split(/\n+/).map((part) => part.trim()).filter(Boolean);
  if (paras.length > 1) return { lede: paras[0], rest: paras.slice(1) };
  const sentences = summary.match(/[^.!?]+[.!?]+|[^.!?]+$/g) ?? [summary];
  if (sentences.length <= 2) return { lede: summary.trim(), rest: [] };
  return {
    lede: sentences.slice(0, 2).join(' ').trim(),
    rest: [sentences.slice(2).join(' ').trim()],
  };
}
