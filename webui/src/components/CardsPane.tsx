import { useEffect, useRef, useState, type KeyboardEvent } from 'react';
import { GENERIC_CARD_KEYS, ops, type CardItem, type CardKey, type MeetingStateDoc, type Op, type Question } from '../types';
import { CAPTURE_TAGS, CARD_LABELS, capturedRailFeed, sortedCardItems, type CapturedFeedEntry } from '../state';
import { EvidenceRow } from './EvidenceChip';
import CitationBadge from './CitationBadge';
import { useEvidenceLookup } from '../evidence';
import { clock } from '../report';
import { initials, speakerColor } from '../people';
import { QuestionRow } from './QuestionInbox';
import { InlineMarkdown } from './report/MarkdownView';
import './capturedLedger.css';

/** Ledger filter tabs. "other" gathers key points, timeline beats and notes. */
export type LedgerTab = 'all' | 'decisions' | 'actions' | 'risks' | 'questions' | 'other';

const LEDGER_TABS: Array<{ id: LedgerTab; label: string }> = [
  { id: 'all', label: 'All' },
  { id: 'decisions', label: 'Decisions' },
  { id: 'actions', label: 'Actions' },
  { id: 'risks', label: 'Risks' },
  { id: 'questions', label: 'Questions' },
  { id: 'other', label: 'Other' },
];

/** Which ledger tab a feed entry belongs to. */
export function ledgerTabOf(entry: CapturedFeedEntry): Exclude<LedgerTab, 'all'> {
  if (entry.kind === 'question') return 'questions';
  switch (entry.item.card) {
    case 'decisions': return 'decisions';
    case 'action_items': return 'actions';
    case 'risks': return 'risks';
    default: return 'other';
  }
}

/** Per-tab counts of what is visible (removed items never count). */
export function ledgerCounts(entries: CapturedFeedEntry[]): Record<LedgerTab, number> {
  const counts: Record<LedgerTab, number> = {
    all: 0, decisions: 0, actions: 0, risks: 0, questions: 0, other: 0,
  };
  for (const entry of entries) {
    if (entry.kind === 'item' && entry.item.status === 'removed') continue;
    counts.all += 1;
    counts[ledgerTabOf(entry)] += 1;
  }
  return counts;
}

/**
 * Due-date urgency. Only ISO-style dates (YYYY-MM-DD…) are judged; free text
 * like "next Tuesday" stays neutral rather than guessing.
 */
export function dueTone(due: string, now = new Date()): 'overdue' | 'soon' | 'neutral' {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(due.trim());
  if (!match) return 'neutral';
  const dueDay = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  if (Number.isNaN(dueDay.getTime())) return 'neutral';
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const days = Math.round((dueDay.getTime() - today.getTime()) / 86400000);
  if (days < 0) return 'overdue';
  if (days <= 2) return 'soon';
  return 'neutral';
}

function dueLabel(due: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(due.trim());
  if (!match) return due.trim();
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  return date.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

/** Owner avatar + due chip under an action item. */
function ActionMeta({ item }: { item: CardItem }) {
  const { participants } = useEvidenceLookup();
  const ownerId = typeof item.data.owner_participant_id === 'string' ? item.data.owner_participant_id : null;
  const owner = ownerId ? participants.find((person) => person.id === ownerId) : undefined;
  const ownerName = owner?.display_name
    || (typeof item.data.owner === 'string' ? item.data.owner.trim() : '');
  const rawDue = item.data.deadline || item.data.due_date;
  const due = typeof rawDue === 'string' ? rawDue.trim() : '';
  if (!ownerName && !due) {
    return <div className="ledger-meta"><span className="ledger-owner unassigned">Unassigned</span></div>;
  }
  const tone = due ? dueTone(due) : 'neutral';
  return (
    <div className="ledger-meta">
      {ownerName && (
        <span className="ledger-owner">
          <span
            className="ledger-avatar"
            style={{ background: speakerColor(owner?.id ?? null, participants) }}
            aria-hidden="true"
          >
            {initials(ownerName)}
          </span>
          {ownerName}
        </span>
      )}
      {due && (
        <span className={`ledger-due ${tone}`}>
          {tone === 'overdue' ? 'Overdue · ' : ''}
          {dueLabel(due)}
        </span>
      )}
    </div>
  );
}

interface CardsPaneProps {
  cards: MeetingStateDoc['cards'];
  onSendOp?: (op: Op) => Promise<boolean>;
  onEvidenceClick: (segmentId: string) => void;
  /** Host-only: revert the event at `seq`. Omitted for guests. */
  onUndo?: (seq: number) => Promise<boolean>;
  /** Newest event seq per item id, from the reducer. */
  lastSeqByTarget: Record<string, number>;
  /** When true, omit the outer panel chrome (embedded in Captured rail). */
  embedded?: boolean;
  /** Hide composer and edit actions; skip removed items (print / archive). */
  readOnly?: boolean;
  /** Live rail: one newest-first stream instead of type-grouped sections. */
  newestFirst?: boolean;
  /** Interleaved into the newest-first feed when `newestFirst` is set. */
  questions?: Question[];
  /**
   * Live rail: lift this many top-ranked insights into a highlighted lead
   * group above the stream. 0 keeps the rail as one flat feed.
   */
  highlightTop?: number;
  /** Meeting status, for the lead group's empty-state copy. */
  status?: string;
  cloudEnabled?: boolean;
  intelligenceOnline?: boolean;
}

/** Why the highlighted lead is empty — the rail's only "nothing yet" copy. */
function leadGhostText(
  status: string,
  cloudEnabled: boolean,
  intelligenceOnline: boolean,
): string {
  if (status === 'ending') return 'Wrapping up insights…';
  if (!cloudEnabled) return 'Turn on AI insights to generate live insights.';
  if (!intelligenceOnline) return 'AI insights are offline';
  if (status === 'ended') return 'No substantive insights captured yet.';
  return 'Listening for insights…';
}

/** Metadata must survive the full-meeting print even when absent from the wording. */
function PrintedItemDetails({ item }: { item: CardItem }) {
  const { participants } = useEvidenceLookup();
  const details: string[] = [];
  if (item.card === 'action_items') {
    const owner = participants.find(person => person.id === item.data.owner_participant_id);
    details.push(`Owner: ${owner?.display_name || 'Unassigned'}`);
    const deadline = item.data.deadline || item.data.due_date;
    if (typeof deadline === 'string' && deadline.trim()) details.push(`Due: ${deadline.trim()}`);
  }
  if (item.card === 'risks' && typeof item.data.severity === 'string' && item.data.severity.trim()) {
    details.push(`Severity: ${item.data.severity}`);
  }
  if (item.card === 'timeline' && typeof item.data.start_s === 'number' && Number.isFinite(item.data.start_s)) {
    details.push(clock(item.data.start_s));
  }
  if (item.author_type === 'system' && item.author_id === 'voice_command') details.push('Spoken request');
  return details.length ? <p className="meta">{details.join(' · ')}</p> : null;
}

function CardItemRow({
  item,
  tag,
  onSendOp,
  onEvidenceClick,
  onUndo,
  undoSeq,
  readOnly = false,
  featured = false,
}: {
  item: CardItem;
  tag: string;
  onSendOp?: (op: Op) => Promise<boolean>;
  onEvidenceClick: (segmentId: string) => void;
  onUndo?: (seq: number) => Promise<boolean>;
  undoSeq?: number;
  readOnly?: boolean;
  /** Row sits in the rail's highlighted lead group. */
  featured?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.text);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!editing) setDraft(item.text);
  }, [editing, item.text]);

  const canUndo = onUndo !== undefined && undoSeq !== undefined;
  const confirmed = item.status === 'confirmed';
  // The ledger treatments are for the live rail; print keeps the plain row.
  const isAction = item.card === 'action_items' && !readOnly;
  const isDecision = item.card === 'decisions' && !readOnly;

  const statusClass =
    item.status === 'confirmed' ? 'confirmed' : item.status === 'proposed' ? 'proposed' : '';

  const saveEdit = async () => {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === item.text || !onSendOp) {
      setEditing(false);
      return;
    }
    setSaving(true);
    try {
      if (await onSendOp(ops.updateItem(item.id, { text: trimmed }))) {
        setEditing(false);
      }
    } catch {
      // Keep the editor and draft intact; the shared mutation banner explains why.
    } finally {
      setSaving(false);
    }
  };

  if (item.status === 'removed') {
    return (
      <div className={`card-item removed${item.pinned ? ' pinned' : ''}`}>
        <div className="capture-tag">{tag}</div>
        <p className="card-item-text">{item.text}</p>
      </div>
    );
  }

  return (
    <div
      className={`card-item ${statusClass}${item.pinned ? ' pinned' : ''}${featured ? ' featured' : ''}`}
      data-card={item.card}
    >
      <div className="capture-tag">
        {tag}
        {item.status === 'proposed' && <span className="suggested-badge">AI suggested</span>}
        {item.pinned && <span className="suggested-badge pinned-badge">Pinned</span>}
      </div>
      {(item.review?.state === 'provisional' || item.review?.state === 'unsupported') &&
        <span className="review-badge">{item.review.state === 'unsupported' ? 'Needs verification' : 'Provisional'}</span>}
      {item.review?.state === 'human' && <span className="review-badge">User clarified</span>}
      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => void saveEdit()}
          disabled={saving}
          aria-label={`Edit ${tag.toLowerCase()}`}
          autoFocus
        />
      ) : isAction ? (
        // Actions read as a to-do row. There is no "done" state in the record,
        // so the box confirms the action; confirmed stays checked.
        <div className="ledger-action">
          <input
            type="checkbox"
            className="ledger-check"
            checked={confirmed}
            disabled={confirmed || !onSendOp}
            onChange={() => { void onSendOp?.(ops.confirmItem(item.id)); }}
            aria-label={confirmed ? `Confirmed: ${item.text}` : `Confirm action: ${item.text}`}
            title={confirmed ? 'Confirmed' : 'Confirm this action'}
          />
          <div className="ledger-action-body">
            <p className="card-item-text" onDoubleClick={() => setEditing(true)}>
              <InlineMarkdown text={item.text} />
            </p>
            <ActionMeta item={item} />
          </div>
        </div>
      ) : (
        <p className="card-item-text" onDoubleClick={() => { if (!readOnly) setEditing(true); }}>
          <InlineMarkdown text={item.text} />
        </p>
      )}
      {isDecision && !editing && (
        <div className={`ledger-state${confirmed ? ' confirmed' : ''}`}>
          <span className={confirmed ? '' : 'current'}>Proposed</span>
          <svg width="14" height="8" viewBox="0 0 14 8" aria-hidden="true">
            <path d="M1 4h11M9 1l3 3-3 3" fill="none" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className={confirmed ? 'current' : ''}>Confirmed</span>
          {!confirmed && onSendOp && (
            <button type="button" className="ledger-confirm" onClick={() => { void onSendOp(ops.confirmItem(item.id)); }}>
              Confirm
            </button>
          )}
        </div>
      )}
      {readOnly && <PrintedItemDetails item={item} />}
      <CitationBadge item={item} />
      <EvidenceRow
        ids={item.evidence}
        onClick={onEvidenceClick}
        limit={readOnly ? 0 : undefined}
      />
      {!readOnly && (
        <div className="card-item-actions no-print">
          <button type="button" disabled={saving} onClick={() => setEditing(true)}>
            Edit
          </button>
          {item.status !== 'confirmed' && (
            <button type="button" onClick={() => { void onSendOp?.(ops.confirmItem(item.id)); }}>
              Confirm
            </button>
          )}
          <button
            type="button"
            onClick={() => { void onSendOp?.(item.pinned ? ops.unpinItem(item.id) : ops.pinItem(item.id)); }}
          >
            {item.pinned ? 'Unpin' : 'Pin'}
          </button>
          <button type="button" className="danger" onClick={() => { void onSendOp?.(ops.removeItem(item.id)); }}>
            Remove
          </button>
          {canUndo && (
            <button
              type="button"
              className="ghost"
              title="Undo the last change to this item"
              onClick={() => { void onUndo?.(undoSeq as number); }}
            >
              Undo
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function CardSection({
  cardKey,
  items,
  onSendOp,
  onEvidenceClick,
  onUndo,
  lastSeqByTarget,
  readOnly = false,
}: {
  cardKey: CardKey;
  items: CardItem[];
  onSendOp?: (op: Op) => Promise<boolean>;
  onEvidenceClick: (segmentId: string) => void;
  onUndo?: (seq: number) => Promise<boolean>;
  lastSeqByTarget: Record<string, number>;
  readOnly?: boolean;
}) {
  const tag = CAPTURE_TAGS[cardKey];
  const visible = readOnly ? items.filter((item) => item.status !== 'removed') : items;
  if (visible.length === 0) return null;

  return (
    <div className="card-section">
      <h4 className="card-section-title">
        {CARD_LABELS[cardKey]}
        <span className="card-section-count">{visible.length}</span>
      </h4>
      {sortedCardItems(visible).map((item) => (
        <CardItemRow
          key={item.id}
          item={item}
          tag={tag}
          onSendOp={onSendOp}
          onEvidenceClick={onEvidenceClick}
          onUndo={onUndo}
          undoSeq={lastSeqByTarget[item.id]}
          readOnly={readOnly}
        />
      ))}
    </div>
  );
}

/** The four kinds people add by hand most; the full list stays one step away. */
const QUICK_KINDS: Array<{ key: CardKey; label: string }> = [
  { key: 'user_notes', label: 'Note' },
  { key: 'decisions', label: 'Decision' },
  { key: 'action_items', label: 'Action' },
  { key: 'risks', label: 'Risk' },
];

/** Quick-add at the head of the ledger: one field and a small kind picker. */
function LedgerComposer({ onSendOp }: { onSendOp: (op: Op) => Promise<boolean> }) {
  const [cardKey, setCardKey] = useState<CardKey>('user_notes');
  const [newText, setNewText] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const label = QUICK_KINDS.find((kind) => kind.key === cardKey)?.label ?? CAPTURE_TAGS[cardKey];

  const addItem = async () => {
    const trimmed = newText.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    try {
      if (await onSendOp(ops.addItem(cardKey, trimmed))) setNewText('');
    } catch {
      // Preserve the draft for retry.
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="card-add-row ledger-composer no-print">
      <div className="ledger-kinds" role="radiogroup" aria-label="Capture type">
        {QUICK_KINDS.map((kind) => (
          <button
            key={kind.key}
            type="button"
            role="radio"
            aria-checked={cardKey === kind.key}
            className={cardKey === kind.key ? 'active' : undefined}
            data-card={kind.key}
            onClick={() => setCardKey(kind.key)}
          >
            {kind.label}
          </button>
        ))}
      </div>
      <div className="ledger-input">
        <input
          type="text"
          placeholder={`Add a ${label.toLowerCase()}…`}
          value={newText}
          aria-label={`New ${label.toLowerCase()}`}
          onChange={(e) => setNewText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void addItem();
          }}
        />
        <button
          type="button"
          className="primary"
          disabled={submitting || !newText.trim()}
          onClick={() => void addItem()}
        >
          {submitting ? 'Adding…' : 'Add'}
        </button>
      </div>
    </div>
  );
}

/** Filter tabs over the live rail, with counts; empty kinds stay hidden. */
function LedgerTabs({
  counts,
  active,
  onSelect,
}: {
  counts: Record<LedgerTab, number>;
  active: LedgerTab;
  onSelect: (tab: LedgerTab) => void;
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({});
  const tabs = LEDGER_TABS.filter((tab) => tab.id === 'all' || counts[tab.id] > 0 || tab.id === active);
  const move = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = tabs.findIndex((tab) => tab.id === active);
    let next = index;
    if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    else return;
    event.preventDefault();
    const target = tabs[next];
    onSelect(target.id);
    refs.current[target.id]?.focus();
  };
  return (
    <div className="ledger-tabs no-print" role="tablist" aria-label="Filter captured items" onKeyDown={move}>
      {tabs.map((tab) => (
        <button
          key={tab.id}
          ref={(el) => { refs.current[tab.id] = el; }}
          type="button"
          role="tab"
          id={`ledger-tab-${tab.id}`}
          aria-selected={tab.id === active}
          aria-controls="ledger-panel"
          tabIndex={tab.id === active ? 0 : -1}
          className={tab.id === active ? 'active' : undefined}
          data-tab={tab.id}
          onClick={() => onSelect(tab.id)}
        >
          {tab.label}
          <span className="ledger-tab-count">{counts[tab.id]}</span>
        </button>
      ))}
    </div>
  );
}

function CaptureComposer({ onSendOp }: { onSendOp: (op: Op) => Promise<boolean> }) {
  const [cardKey, setCardKey] = useState<CardKey>('key_points');
  const [newText, setNewText] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const addItem = async () => {
    const trimmed = newText.trim();
    if (!trimmed || submitting) return;
    setSubmitting(true);
    try {
      if (await onSendOp(ops.addItem(cardKey, trimmed))) setNewText('');
    } catch {
      // Preserve the draft for retry.
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="card-add-row no-print">
      <select
        value={cardKey}
        onChange={(e) => setCardKey(e.target.value as CardKey)}
        aria-label="Capture type"
      >
        {GENERIC_CARD_KEYS.map((key) => (
          <option key={key} value={key}>
            {CAPTURE_TAGS[key]}
          </option>
        ))}
      </select>
      <input
        type="text"
        placeholder={`Add ${CAPTURE_TAGS[cardKey].toLowerCase()}…`}
        value={newText}
        aria-label={`New ${CAPTURE_TAGS[cardKey].toLowerCase()}`}
        onChange={(e) => setNewText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') void addItem();
        }}
      />
      <button
        type="button"
        className="primary"
        disabled={submitting || !newText.trim()}
        onClick={() => void addItem()}
      >
        {submitting ? 'Adding…' : 'Add'}
      </button>
    </div>
  );
}

export default function CardsPane({
  cards,
  onSendOp,
  onEvidenceClick,
  onUndo,
  lastSeqByTarget,
  embedded = false,
  readOnly = false,
  newestFirst = false,
  questions = [],
  highlightTop = 0,
  status = 'active',
  cloudEnabled = true,
  intelligenceOnline = true,
}: CardsPaneProps) {
  const [tab, setTab] = useState<LedgerTab>('all');
  const rail = newestFirst
    ? capturedRailFeed(cards, questions, highlightTop)
    : { top: [], rest: [] };
  const feed = rail.rest.filter((entry) =>
    entry.kind === 'item' ? !readOnly || entry.item.status !== 'removed' : true,
  );
  // The live rail is a filterable ledger; print and archive keep the plain feed.
  const ledger = newestFirst && !readOnly;
  const everything = ledger ? capturedRailFeed(cards, questions, 0).rest : [];
  const counts = ledgerCounts(everything);
  const filtered = tab === 'all' ? [] : everything.filter((entry) => ledgerTabOf(entry) === tab);

  const grouped = (
    <>
      {GENERIC_CARD_KEYS.map((key) => {
        const items = cards[key] ?? [];
        if (items.length === 0) return null;
        return (
          <CardSection
            key={key}
            cardKey={key}
            items={items}
            onSendOp={onSendOp}
            onEvidenceClick={onEvidenceClick}
            onUndo={onUndo}
            lastSeqByTarget={lastSeqByTarget}
            readOnly={readOnly}
          />
        );
      })}
    </>
  );

  const lead = highlightTop > 0 && (
    <div className="capture-lead">
      <h4 className="card-section-title">
        Top insights
        {rail.top.length > 0 && <span className="card-section-count">{rail.top.length}</span>}
      </h4>
      {rail.top.length === 0 ? (
        <p className="capture-lead-ghost">
          {leadGhostText(status, cloudEnabled, intelligenceOnline)}
        </p>
      ) : (
        rail.top.map((item) => (
          <CardItemRow
            key={item.id}
            item={item}
            tag={CAPTURE_TAGS[item.card]}
            onSendOp={onSendOp}
            onEvidenceClick={onEvidenceClick}
            onUndo={onUndo}
            undoSeq={lastSeqByTarget[item.id]}
            readOnly={readOnly}
            featured
          />
        ))
      )}
    </div>
  );

  const renderEntry = (entry: CapturedFeedEntry) =>
    entry.kind === 'item' ? (
      <CardItemRow
        key={entry.item.id}
        item={entry.item}
        tag={CAPTURE_TAGS[entry.item.card]}
        onSendOp={onSendOp}
        onEvidenceClick={onEvidenceClick}
        onUndo={onUndo}
        undoSeq={lastSeqByTarget[entry.item.id]}
        readOnly={readOnly}
      />
    ) : (
      <QuestionRow
        key={entry.question.id}
        q={entry.question}
        onSendOp={onSendOp}
        onEvidenceClick={onEvidenceClick}
        readOnly={readOnly}
      />
    );

  const stream = (
    <>
      {lead}
      {highlightTop > 0 && feed.length > 0 && (
        <h4 className="card-section-title capture-rest-title">Everything captured</h4>
      )}
      {feed.map(renderEntry)}
    </>
  );

  const ledgerBody = (
    <>
      {onSendOp && <LedgerComposer onSendOp={onSendOp} />}
      <LedgerTabs counts={counts} active={tab} onSelect={setTab} />
      <div
        id="ledger-panel"
        role="tabpanel"
        aria-labelledby={`ledger-tab-${tab}`}
        className="ledger-panel"
      >
        {tab === 'all' ? stream : filtered.length ? (
          filtered.map(renderEntry)
        ) : (
          <p className="capture-lead-ghost">Nothing in this view yet.</p>
        )}
      </div>
    </>
  );

  const body = ledger ? ledgerBody : (
    <>
      {newestFirst ? stream : grouped}
      {!readOnly && onSendOp && <CaptureComposer onSendOp={onSendOp} />}
    </>
  );

  if (embedded) {
    return <div className="capture-cards">{body}</div>;
  }

  return (
    <section className="panel capture">
      <h3 className="capture-heading">Captured</h3>
      <div className="capture-body">{body}</div>
    </section>
  );
}
