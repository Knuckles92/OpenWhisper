import { useEffect, useState } from 'react';
import { GENERIC_CARD_KEYS, ops, type CardItem, type CardKey, type MeetingStateDoc, type Op, type Question } from '../types';
import { CAPTURE_TAGS, CARD_LABELS, capturedFeedEntries, sortedCardItems } from '../state';
import { EvidenceRow } from './EvidenceChip';
import { QuestionRow } from './QuestionInbox';

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
}

function CardItemRow({
  item,
  tag,
  onSendOp,
  onEvidenceClick,
  onUndo,
  undoSeq,
  readOnly = false,
}: {
  item: CardItem;
  tag: string;
  onSendOp?: (op: Op) => Promise<boolean>;
  onEvidenceClick: (segmentId: string) => void;
  onUndo?: (seq: number) => Promise<boolean>;
  undoSeq?: number;
  readOnly?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.text);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!editing) setDraft(item.text);
  }, [editing, item.text]);

  const canUndo = onUndo !== undefined && undoSeq !== undefined;

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
    <div className={`card-item ${statusClass}${item.pinned ? ' pinned' : ''}`}>
      <div className="capture-tag">{tag}</div>
      {editing ? (
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={() => void saveEdit()}
          disabled={saving}
          aria-label={`Edit ${tag.toLowerCase()}`}
          autoFocus
        />
      ) : (
        <p className="card-item-text" onDoubleClick={() => { if (!readOnly) setEditing(true); }}>
          {item.text}
        </p>
      )}
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
          {item.status === 'proposed' && (
            <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>AI suggested</span>
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
}: CardsPaneProps) {
  const feed = newestFirst
    ? capturedFeedEntries(cards, questions).filter((entry) =>
        entry.kind === 'item' ? !readOnly || entry.item.status !== 'removed' : true,
      )
    : [];

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

  const stream = (
    <>
      {feed.map((entry) =>
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
        ),
      )}
    </>
  );

  const body = (
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
