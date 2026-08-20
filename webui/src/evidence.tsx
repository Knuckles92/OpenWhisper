import { createContext, useContext, useMemo, type ReactNode } from 'react';
import { clock, segmentMap, speakerName } from './report';
import type { Participant, Segment } from './types';

export interface EvidenceLookup {
  segs: Map<string, Segment>;
  participants: Participant[];
}

const EMPTY_LOOKUP: EvidenceLookup = {
  segs: new Map(),
  participants: [],
};

const EvidenceContext = createContext<EvidenceLookup>(EMPTY_LOOKUP);

const QUOTE_WORDS = 16;

/** Transcript segments + speakers so evidence chips can hide raw `sg_…` ids. */
export function EvidenceProvider({
  segments,
  participants,
  children,
}: {
  segments: Segment[];
  participants: Participant[];
  children: ReactNode;
}) {
  const value = useMemo<EvidenceLookup>(
    () => ({ segs: segmentMap(segments), participants }),
    [segments, participants],
  );
  return <EvidenceContext.Provider value={value}>{children}</EvidenceContext.Provider>;
}

export function useEvidenceLookup(): EvidenceLookup {
  return useContext(EvidenceContext);
}

function clipQuote(text: string): string {
  const words = text.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '';
  if (words.length <= QUOTE_WORDS) return words.join(' ');
  return `${words.slice(0, QUOTE_WORDS).join(' ')}…`;
}

/** Chip face: meeting-clock time, or an explicit override. */
export function evidenceLabel(segment: Segment | undefined, explicit?: string): string {
  if (explicit) return explicit;
  if (segment) return clock(segment.start_s);
  return 'Source';
}

/** Hover text: speaker, clock, and a short quote. */
export function evidenceTitle(
  segment: Segment | undefined,
  participants: Participant[],
): string {
  if (!segment) return 'Jump to conversation';
  const speaker = speakerName(participants, segment.speaker_participant_id, segment.channel);
  const time = clock(segment.start_s);
  const quote = clipQuote(segment.text || '');
  if (quote) return `${speaker} · ${time} — ${quote}`;
  return `Jump to ${speaker} at ${time}`;
}

/** Deduped evidence ids in meeting-clock order; unresolved ids sort last. */
export function sortEvidenceIds(ids: string[], segs: Map<string, Segment>): string[] {
  return [...new Set(ids.filter(Boolean))].sort((left, right) => {
    const leftTime = segs.get(left)?.start_s;
    const rightTime = segs.get(right)?.start_s;
    if (leftTime == null && rightTime == null) return left.localeCompare(right);
    if (leftTime == null) return 1;
    if (rightTime == null) return -1;
    if (leftTime !== rightTime) return leftTime - rightTime;
    return left.localeCompare(right);
  });
}
