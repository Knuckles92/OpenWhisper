import { useState } from 'react';
import { CARD_KEYS, ops, type CardItem, type CardKey, type MeetingStateDoc, type Op } from '../types';
import { sortedCardItems } from '../state';
import EvidenceChip from './EvidenceChip';

interface CardsPaneProps {
  cards: MeetingStateDoc['cards'];
  onSendOp: (op: Op) => void;
  onEvidenceClick: (segmentId: string) => void;
  /** Host-only: revert the event at `seq`. Omitted for guests. */
  onUndo?: (seq: number) => void;
  /** Newest event seq per item id, from the reducer. */
  lastSeqByTarget: Record<string, number>;
  /** When true, omit the outer panel chrome (embedded in Captured rail). */
  embedded?: boolean;
}

const CAPTURE_TAGS: Record<CardKey, string> = {
  key_points: 'Key point',
  decisions: 'Decision',
  action_items: 'Action',
  risks: 'Risk',
  timeline: 'Timeline',
  user_notes: 'Note',
};

function captureTag(cardKey: CardKey): string {
  return CAPTURE_TAGS[cardKey];
}

function CardItemRow({
  item,
  tag,
  onSendOp,
  onEvidenceClick,
  onUndo,
  undoSeq,
}: {
  item: CardItem;
  tag: string;
  onSendOp: (op: Op) => void;
  onEvidenceClick: (segmentId: string) => void;
  onUndo?: (seq: number) => void;
  undoSeq?: number;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.text);

  const canUndo = onUndo !== undefined && undoSeq !== undefined;

  const statusClass =
    item.status === 'confirmed' ? 'confirmed' : item.status === 'proposed' ? 'proposed' : '';

  const saveEdit = () => {
    const trimmed = draft.trim();
    if (trimmed && trimmed !== item.text) {
      onSendOp(ops.updateItem(item.id, { text: trimmed }));
    }
    setEditing(false);
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
          onBlur={saveEdit}
          autoFocus
        />
      ) : (
        <p className="card-item-text" onDoubleClick={() => setEditing(true)}>
          {item.text}
        </p>
      )}
      {item.evidence.length > 0 && (
        <div className="evidence-row">
          {item.evidence.map((id) => (
            <EvidenceChip key={id} segmentId={id} onClick={onEvidenceClick} />
          ))}
        </div>
      )}
      <div className="card-item-actions">
        <button type="button" onClick={() => setEditing(true)}>
          Edit
        </button>
        {item.status !== 'confirmed' && (
          <button type="button" onClick={() => onSendOp(ops.confirmItem(item.id))}>
            Confirm
          </button>
        )}
        <button
          type="button"
          onClick={() => onSendOp(item.pinned ? ops.unpinItem(item.id) : ops.pinItem(item.id))}
        >
          {item.pinned ? 'Unpin' : 'Pin'}
        </button>
        <button type="button" className="danger" onClick={() => onSendOp(ops.removeItem(item.id))}>
          Remove
        </button>
        {canUndo && (
          <button
            type="button"
            className="ghost"
            title="Undo the last change to this item"
            onClick={() => onUndo?.(undoSeq as number)}
          >
            Undo
          </button>
        )}
        {item.status === 'proposed' && (
          <span style={{ fontSize: 11, color: 'var(--text-muted)' }}>AI suggested</span>
        )}
      </div>
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
}: {
  cardKey: CardKey;
  items: CardItem[];
  onSendOp: (op: Op) => void;
  onEvidenceClick: (segmentId: string) => void;
  onUndo?: (seq: number) => void;
  lastSeqByTarget: Record<string, number>;
}) {
  const tag = captureTag(cardKey);

  return (
    <div className="card-section">
      {sortedCardItems(items).map((item) => (
        <CardItemRow
          key={item.id}
          item={item}
          tag={tag}
          onSendOp={onSendOp}
          onEvidenceClick={onEvidenceClick}
          onUndo={onUndo}
          undoSeq={lastSeqByTarget[item.id]}
        />
      ))}
    </div>
  );
}

function CaptureComposer({ onSendOp }: { onSendOp: (op: Op) => void }) {
  const [cardKey, setCardKey] = useState<CardKey>('key_points');
  const [newText, setNewText] = useState('');

  const addItem = () => {
    const trimmed = newText.trim();
    if (!trimmed) return;
    onSendOp(ops.addItem(cardKey, trimmed));
    setNewText('');
  };

  return (
    <div className="card-add-row">
      <select
        value={cardKey}
        onChange={(e) => setCardKey(e.target.value as CardKey)}
        aria-label="Capture type"
      >
        {CARD_KEYS.map((key) => (
          <option key={key} value={key}>
            {CAPTURE_TAGS[key]}
          </option>
        ))}
      </select>
      <input
        type="text"
        placeholder={`Add ${CAPTURE_TAGS[cardKey].toLowerCase()}…`}
        value={newText}
        onChange={(e) => setNewText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') addItem();
        }}
      />
      <button type="button" className="primary" onClick={addItem}>
        Add
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
}: CardsPaneProps) {
  const body = (
    <>
      {CARD_KEYS.map((key) => {
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
          />
        );
      })}
      <CaptureComposer onSendOp={onSendOp} />
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
