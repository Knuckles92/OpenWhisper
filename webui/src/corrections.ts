import type { CardItem, Op, Segment } from './types';

/** Longest term a correction may replace; longer selections become insights only. */
export const MAX_TERM_CHARS = 120;

export type TextCorrector = (text: string) => string;

const identity: TextCorrector = (text) => text;

/** UTF-8 FNV-1a, identical to `meeting.corrections.segment_fingerprint`. */
export function segmentFingerprint(text: string): string {
  let value = 0xcbf29ce484222325n;
  for (const byte of new TextEncoder().encode(text)) {
    value = ((value ^ BigInt(byte)) * 0x100000001b3n) & 0xffffffffffffffffn;
  }
  return `fnv1a64:${value.toString(16).padStart(16, '0')}`;
}

/**
 * Build the dashboard op for an insight offered on selected text.
 *
 * A replacement can correct one selected occurrence or the whole meeting;
 * otherwise the note is an `agent_insight` the agent reads as human context.
 * Both land on the human-only `user_notes` card.
 */
export type CorrectionScope = 'meeting' | { segmentId: string; occurrenceIndex: number; baseFingerprint: string };

export function insightOp(selected: string, insight: string, replacement: string, scope: CorrectionScope): Op {
  const source = selected.trim();
  const term = replacement.trim();
  const note = insight.trim();
  const scoped = Boolean(term && scope !== 'meeting');
  return {
    op: 'add_item', card: 'user_notes',
    text: term ? `${scoped ? 'Correction in this passage' : 'Correction throughout this meeting'}: “${source}” → “${term}”.${note ? ` ${note}` : ''}` : `Regarding “${source}”: ${note}`,
    data: { kind: term ? (scoped ? 'occurrence_correction' : 'term_correction') : 'agent_insight',
      selected_text: source, replacement: term,
      ...(scoped ? { occurrence_index: (scope as Exclude<CorrectionScope, 'meeting'>).occurrenceIndex,
        base_fingerprint: (scope as Exclude<CorrectionScope, 'meeting'>).baseFingerprint } : {}) },
    evidence: scoped ? [(scope as Exclude<CorrectionScope, 'meeting'>).segmentId] : [],
  };
}

/** Lower-cased misheard term -> replacement, from live human term corrections. */
export function termRules(notes: readonly CardItem[] | undefined): Map<string, string> {
  const rules = new Map<string, string>();
  for (const item of notes ?? []) {
    const data = item.data ?? {};
    const spoken = item.author_type === 'system' && item.author_id === 'voice_command' && data.source === 'voice_command' && data.command === 'fix_transcript';
    if (item.status === 'removed' || (item.author_type !== 'user' && !spoken) || data.kind !== 'term_correction') continue;
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

type ScopedCorrection = { source: string; replacement: string; index: number; fingerprint: string };
type ScopedEdit = { start: number; end: number; replacement: string };

const escapeTerm = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

function matchesFor(text: string, source: string): RegExpMatchArray[] {
  const pattern = new RegExp(`(?<![\\p{L}\\p{N}_])${escapeTerm(source)}(?![\\p{L}\\p{N}_])`, 'giu');
  return Array.from(text.matchAll(pattern));
}

function occurrenceRules(notes: readonly CardItem[] | undefined): Map<string, ScopedCorrection[]> {
  const grouped = new Map<string, ScopedCorrection[]>();
  for (const item of notes ?? []) {
    const data = item.data ?? {};
    const evidence = item.evidence ?? [];
    if (item.status === 'removed' || item.author_type !== 'user' || data.kind !== 'occurrence_correction') continue;
    if (evidence.length !== 1 || typeof evidence[0] !== 'string') continue;
    if (typeof data.selected_text !== 'string' || typeof data.replacement !== 'string') continue;
    if (typeof data.occurrence_index !== 'number' || !Number.isInteger(data.occurrence_index)
      || data.occurrence_index < 0 || data.occurrence_index > 1000) continue;
    if (typeof data.base_fingerprint !== 'string' || !/^fnv1a64:[0-9a-f]{16}$/.test(data.base_fingerprint)) continue;
    const source = data.selected_text.trim();
    const replacement = data.replacement.trim();
    if (!source || !replacement || source.length > MAX_TERM_CHARS || replacement.length > MAX_TERM_CHARS) continue;
    const items = grouped.get(evidence[0]) ?? [];
    items.push({ source, replacement, index: data.occurrence_index, fingerprint: data.base_fingerprint });
    grouped.set(evidence[0], items);
  }
  return grouped;
}

/** All indices refer to the same text, before any one-occurrence edit. */
function scopedEdits(base: string, rules: ScopedCorrection[]): ScopedEdit[] {
  if (!rules.length) return [];
  const edits = new Map<string, ScopedEdit>();
  const fingerprint = segmentFingerprint(base);
  for (const rule of rules) {
    if (rule.fingerprint !== fingerprint) continue;
    const match = matchesFor(base, rule.source)[rule.index];
    if (!match || match.index === undefined) continue;
    const start = match.index;
    const end = start + match[0].length;
    const key = `${start}:${end}`;
    if ([...edits.values()].some((prior) => key !== `${prior.start}:${prior.end}`
      && start < prior.end && prior.start < end)) continue;
    edits.set(key, { start, end, replacement: rule.replacement });
  }
  return [...edits.values()].sort((a, b) => a.start - b.start);
}

function applyScopedEdits(base: string, edits: ScopedEdit[]): string {
  let text = base;
  for (const edit of [...edits].reverse()) {
    text = text.slice(0, edit.start) + edit.replacement + text.slice(edit.end);
  }
  return text;
}

/** Map a displayed selection to an immutable base-text occurrence, if unambiguous. */
export function stableOccurrenceIndex(base: string, displayed: string, notes: readonly CardItem[] | undefined,
  segmentId: string, source: string, displayStart: number): number | null {
  const edits = scopedEdits(base, occurrenceRules(notes).get(segmentId) ?? []);
  if (applyScopedEdits(base, edits) !== displayed) return null;
  const matches = matchesFor(base, source);
  for (let index = 0; index < matches.length; index++) {
    const match = matches[index];
    const start = match.index;
    if (start === undefined) continue;
    const end = start + match[0].length;
    if (edits.some((edit) => start < edit.end && edit.start < end)) continue;
    const shifted = start + edits.filter((edit) => edit.end <= start)
      .reduce((delta, edit) => delta + edit.replacement.length - (edit.end - edit.start), 0);
    if (shifted === displayStart && displayed.slice(shifted, shifted + match[0].length).toLowerCase() === source.toLowerCase()) return index;
  }
  return null;
}

/** Segments with corrections applied to the raw text the server exposes. */
export function correctedSegments(segments: Segment[], correct: TextCorrector, notes?: readonly CardItem[]): Segment[] {
  const scoped = occurrenceRules(notes);
  if (correct === identity && !scoped.size) {
    // Server rows may already contain a correction applied when they were
    // fetched. Undo must restore raw text even before transcript rehydration.
    if (segments.every((segment) => segment.original_text === undefined
      || segment.text === segment.original_text)) return segments;
    return segments.map((segment) => segment.original_text !== undefined
      && segment.text !== segment.original_text
      ? { ...segment, text: segment.original_text } : segment);
  }
  return segments.map((segment) => {
    const base = correct(segment.original_text ?? segment.text);
    const text = applyScopedEdits(base, scopedEdits(base, scoped.get(segment.id) ?? []));
    return { ...segment, text };
  });
}
