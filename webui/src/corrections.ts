import type { CardItem, Op, Segment } from './types';

/** Longest term a correction may replace; longer selections become insights only. */
export const MAX_TERM_CHARS = 120;

export type TextCorrector = (text: string) => string;

const identity: TextCorrector = (text) => text;

/**
 * Build the dashboard op for an insight offered on selected text.
 *
 * A non-empty replacement makes a `term_correction` (applied to the transcript
 * everywhere); otherwise the note is an `agent_insight` the agent reads as
 * human context. Both land on the human-only `user_notes` card.
 */
export function insightOp(selected: string, insight: string, replacement: string): Op {
  const source = selected.trim();
  const term = replacement.trim();
  const note = insight.trim();
  return {
    op: 'add_item', card: 'user_notes',
    text: term ? `Correction: “${source}” → “${term}”.${note ? ` ${note}` : ''}` : `Regarding “${source}”: ${note}`,
    data: { kind: term ? 'term_correction' : 'agent_insight', selected_text: source, replacement: term },
    evidence: [],
  };
}

/** Lower-cased misheard term -> replacement, from live human term corrections. */
export function termRules(notes: readonly CardItem[] | undefined): Map<string, string> {
  const rules = new Map<string, string>();
  for (const item of notes ?? []) {
    const data = item.data ?? {};
    if (item.status === 'removed' || item.author_type !== 'user' || data.kind !== 'term_correction') continue;
    if (typeof data.selected_text !== 'string' || typeof data.replacement !== 'string') continue;
    const source = data.selected_text.trim();
    const target = data.replacement.trim();
    if (source && target && source.length <= MAX_TERM_CHARS && target.length <= MAX_TERM_CHARS) rules.set(source.toLowerCase(), target);
  }
  return rules;
}

/**
 * Whole-word, case-insensitive corrector mirroring `meeting/corrections.py`.
 *
 * One regex pass means replacements never chain, and longest terms match first
 * so a phrase wins over one of its words.
 */
export function correctionText(notes: readonly CardItem[] | undefined): TextCorrector {
  const rules = termRules(notes);
  if (!rules.size) return identity;
  const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const terms = Array.from(rules.keys()).sort((a, b) => b.length - a.length).map(escape).join('|');
  const pattern = new RegExp(`(?<![\\p{L}\\p{N}_])(?:${terms})(?![\\p{L}\\p{N}_])`, 'giu');
  return (text) => text.replace(pattern, (match) => rules.get(match.toLowerCase()) ?? match);
}

/** Segments with corrections applied to the raw text the server exposes. */
export function correctedSegments(segments: Segment[], correct: TextCorrector): Segment[] {
  if (correct === identity) return segments;
  return segments.map((segment) => ({ ...segment, text: correct(segment.original_text ?? segment.text) }));
}
