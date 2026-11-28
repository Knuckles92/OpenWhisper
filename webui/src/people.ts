import type { Participant } from './types';

/**
 * Warm speaker hues at one OKLCH lightness (~0.53) so no one person shouts.
 * Each carries white initials and reads as name text on cream at WCAG AA.
 */
export const SPEAKER_COLORS = ['#257b51', '#b94c28', '#2c6fae', '#7951ab', '#9b661a', '#017b80', '#b64466'];

/** Neutral for turns nobody has been matched to yet. */
export const UNKNOWN_SPEAKER = '#7a6d62';

/**
 * Precomputed participant-id -> color lookup. Sorting once avoids O(P log P)
 * work per row/bar in hot render paths like TimelineMinimap.
 */
export function speakerColorMap(participants: Participant[]): Map<string, string> {
  const map = new Map<string, string>();
  const others: Participant[] = [];
  for (const p of participants) {
    if (p.kind === 'me') map.set(p.id, SPEAKER_COLORS[0]);
    else others.push(p);
  }
  others.sort((a, b) => (a.created_at || '').localeCompare(b.created_at || '') || a.id.localeCompare(b.id));
  for (let index = 0; index < others.length; index += 1) {
    map.set(others[index].id, SPEAKER_COLORS[1 + (index % (SPEAKER_COLORS.length - 1))]);
  }
  return map;
}

/**
 * Stable color for a participant. "Me" always gets the brand leaf; everyone
 * else is assigned by join order, so colors never shuffle mid-meeting.
 */
export function speakerColor(
  participantId: string | null | undefined,
  participants: Participant[],
): string {
  if (!participantId) return UNKNOWN_SPEAKER;
  return speakerColorMap(participants).get(participantId) ?? UNKNOWN_SPEAKER;
}

/** One or two letters for an avatar, from a display name. */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}
