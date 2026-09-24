import type { Participant } from './types';

/** Muted speaker hues that share lightness/chroma so no one person shouts. */
export const SPEAKER_COLORS = ['#2f6b4f', '#28658f', '#a2603c', '#75509f', '#8a6d1f', '#3d6f7a'];

/**
 * Stable color for a participant. "Me" always gets the brand leaf; everyone
 * else is assigned by join order, so colors never shuffle mid-meeting.
 */
export function speakerColor(
  participantId: string | null | undefined,
  participants: Participant[],
): string {
  if (!participantId) return '#5b6b63';
  const person = participants.find((p) => p.id === participantId);
  if (person?.kind === 'me') return SPEAKER_COLORS[0];
  const others = participants
    .filter((p) => p.kind !== 'me')
    .sort((a, b) => (a.created_at || '').localeCompare(b.created_at || '') || a.id.localeCompare(b.id));
  const index = others.findIndex((p) => p.id === participantId);
  if (index < 0) return '#5b6b63';
  return SPEAKER_COLORS[1 + (index % (SPEAKER_COLORS.length - 1))];
}

/** One or two letters for an avatar, from a display name. */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return '?';
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}
