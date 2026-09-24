import { sortedCardItems } from '../../state';
import { clock, deriveSignalStandfirst, itemTime, ownerId } from '../../report';
import type { CardItem, MeetingStateDoc, Question, Segment } from '../../types';

/** Items that still count: not removed by a person or the agent. */
export function keptItems(items: CardItem[] | undefined): CardItem[] {
  return sortedCardItems((items ?? []).filter((item) => item.status !== 'removed'));
}

export function itemDeadline(item: CardItem): string {
  const raw = item.data.deadline || item.data.due_date;
  return typeof raw === 'string' ? raw.trim() : '';
}

/** Who owns an item: the linked participant, else a free-text owner, else nobody. */
export function itemOwnerName(item: CardItem, state: MeetingStateDoc): string | null {
  const id = ownerId(item);
  if (id && state.participants[id]) return state.participants[id].display_name;
  const loose = item.data.owner ?? item.data.owner_name;
  return typeof loose === 'string' && loose.trim() ? loose.trim() : null;
}

export interface OwnerGroup {
  /** Participant id, the free-text name, or null for unassigned work. */
  key: string | null;
  name: string;
  items: CardItem[];
}

export const UNASSIGNED = 'Unassigned';

/** Action items grouped by owner, in first-seen order, with unassigned work last. */
export function actionGroups(state: MeetingStateDoc): OwnerGroup[] {
  const groups = new Map<string, OwnerGroup>();
  const loose: CardItem[] = [];
  for (const item of keptItems(state.cards.action_items)) {
    const name = itemOwnerName(item, state);
    if (!name) {
      loose.push(item);
      continue;
    }
    const key = ownerId(item) ?? `name:${name.toLowerCase()}`;
    const group = groups.get(key) ?? { key, name, items: [] };
    group.items.push(item);
    groups.set(key, group);
  }
  const ordered = [...groups.values()];
  if (loose.length) ordered.push({ key: null, name: UNASSIGNED, items: loose });
  return ordered;
}

export function openQuestions(state: MeetingStateDoc): Question[] {
  return (state.questions ?? []).filter((question) => question.status === 'open');
}

export function answeredQuestions(state: MeetingStateDoc): Question[] {
  return (state.questions ?? []).filter(
    (question) => question.status === 'resolved' && Boolean((question.answer || '').trim()),
  );
}

function actionLine(item: CardItem, segs?: Map<string, Segment>): string {
  const due = itemDeadline(item);
  const at = segs ? itemTime(item, segs) : null;
  const extras = [due && `due ${due}`, at !== null && `at ${clock(at)}`].filter(Boolean);
  return `- ${item.text.trim()}${extras.length ? ` (${extras.join(', ')})` : ''}`;
}

/** Plain-text task list for one owner, ready to paste into a message. */
export function ownerEmailText(group: OwnerGroup, title: string, segs?: Map<string, Segment>): string {
  const heading = group.key === null
    ? `Unassigned follow-ups from "${title}":`
    : `${group.name} — your follow-ups from "${title}":`;
  return [heading, ...group.items.map((item) => actionLine(item, segs))].join('\n');
}

export interface RecapEmail {
  subject: string;
  body: string;
}

/** A recap email built only from what the meeting captured. */
export function recapEmail(state: MeetingStateDoc, title: string): RecapEmail {
  const lines: string[] = ['Hi all,', ''];
  const summary = deriveSignalStandfirst(state);
  lines.push(summary ? `Thanks for the time today. ${summary}` : `Thanks for the time today. Here is where "${title}" landed.`);

  const decisions = keptItems(state.cards.decisions);
  if (decisions.length) {
    lines.push('', 'Decisions');
    for (const item of decisions) lines.push(`- ${item.text.trim()}`);
  }

  const groups = actionGroups(state);
  if (groups.length) {
    lines.push('', 'Actions');
    for (const group of groups) {
      lines.push(`${group.name}:`);
      for (const item of group.items) lines.push(actionLine(item));
    }
  }

  const open = openQuestions(state);
  if (open.length) {
    lines.push('', 'Still open');
    for (const question of open) lines.push(`- ${question.text.trim()}`);
  }

  lines.push('', 'Thanks,');
  return { subject: `Recap: ${title}`, body: lines.join('\n') };
}

/** Longest body we hand to a mail client; long mailto links get cut off silently. */
export const MAILTO_BODY_LIMIT = 1500;

export function mailtoHref(email: RecapEmail): string {
  let body = email.body;
  if (body.length > MAILTO_BODY_LIMIT) {
    body = `${body.slice(0, MAILTO_BODY_LIMIT).trimEnd()}\n\n[Trimmed for your mail app. Use "Copy email" for the full recap.]`;
  }
  return `mailto:?subject=${encodeURIComponent(email.subject)}&body=${encodeURIComponent(body)}`;
}
