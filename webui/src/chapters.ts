import type { TopicRevision } from './types';

/** One stretch of the meeting under a single topic, in seconds from its start. */
export interface Chapter {
  label: string;
  start_s: number;
  end_s?: number;
}

/** Topic revisions closer together than this are the agent refining a label. */
const FLICKER_S = 20;

const sameLabel = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

/**
 * Turn the topic history into chapters offset from the meeting start.
 *
 * Consecutive duplicates merge, a revision replaced within 20 s is treated as
 * a rewording of the one after it, and the first chapter opens the meeting at
 * 0:00. Returns [] when the start time is missing or unparsable.
 */
export function topicChapters(
  history: TopicRevision[],
  startedAt: string | null | undefined,
  durationS?: number,
): Chapter[] {
  const start = startedAt ? Date.parse(startedAt) : NaN;
  if (!Number.isFinite(start)) return [];
  const limit = durationS !== undefined && Number.isFinite(durationS) && durationS > 0 ? durationS : undefined;
  const revisions = (history ?? [])
    .map((rev) => ({ label: (rev.text ?? '').trim(), at: (Date.parse(rev.ts) - start) / 1000 }))
    .filter((rev) => rev.label && Number.isFinite(rev.at))
    .map((rev) => ({ ...rev, at: Math.min(Math.max(rev.at, 0), limit ?? Infinity) }))
    .sort((a, b) => a.at - b.at);

  const out: Chapter[] = [];
  for (const rev of revisions) {
    let startS = rev.at;
    const last = out[out.length - 1];
    if (last && rev.at - last.start_s < FLICKER_S) {
      out.pop();
      startS = last.start_s;
    }
    const prev = out[out.length - 1];
    if (prev && sameLabel(prev.label, rev.label)) continue;
    out.push({ label: rev.label, start_s: startS });
  }
  const kept = limit === undefined || out.length <= 1 ? out : out.filter((c) => c.start_s < limit);
  if (kept.length) kept[0].start_s = 0;
  return kept.map((chapter, i) => {
    const end = i < kept.length - 1 ? kept[i + 1].start_s : limit;
    return end === undefined ? chapter : { ...chapter, end_s: end };
  });
}
