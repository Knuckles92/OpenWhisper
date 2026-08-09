import { useState } from 'react';
import { CARD_KEYS, ops, type CardItem, type CardKey, type MeetingStateDoc, type Op } from '../types';
import { CARD_LABELS, sortedCardItems } from '../state';
import EvidenceChip from './EvidenceChip';

interface CardsPaneProps {
  cards: MeetingStateDoc['cards'];
  onSendOp: (op: Op) => void;
  onEvidenceClick: (segmentId: string) => void;
  /** Host-only: revert the event at `seq`. Omitted for guests. */
  onUndo?: (seq: number) => void;
  /** Newest event seq per item id, from the reducer. */
  lastSeqByTarget: Record<string, number>;
}

function CardItemRow({
  item,
  onSendOp,
  onEvidenceClick,
  onUndo,
  undoSeq,
}: {
  item: CardItem;
  onSendOp: (op: Op) => void;
  onEvidenceClick: (segmentId: string) => void;
  onUndo?: (seq: number) => void;
  undoSeq?: number;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(item.text);

  // Host undo-anyone: quietly offered on agent-authored items, whose last
  // change the host can revert without hunting through the audit trail.
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
        <p className="card-item-text">{item.text}</p>
      </div>
    );
  }

  return (
    <div className={`card-item ${statusClass}${item.pinned ? ' pinned' : ''}`}>
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
  const [newText, setNewText] = useState('');

  const addItem = () => {
    const trimmed = newText.trim();
    if (!trimmed) return;
    onSendOp(ops.addItem(cardKey, trimmed));
    setNewText('');
  };

  return (
    <div className="card-section">
      <h3 className="card-section-title">{CARD_LABELS[cardKey]}</h3>
      {sortedCardItems(items).map((item) => (
        <CardItemRow
          key={item.id}
          item={item}
          onSendOp={onSendOp}
          onEvidenceClick={onEvidenceClick}
          onUndo={onUndo}
          undoSeq={lastSeqByTarget[item.id]}
        />
      ))}
      <div className="card-add-row">
        <input
          type="text"
          placeholder={`Add to ${CARD_LABELS[cardKey].toLowerCase()}…`}
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
    </div>
  );
}

export default function CardsPane({
  cards,
  onSendOp,
  onEvidenceClick,
  onUndo,
  lastSeqByTarget,
}: CardsPaneProps) {
  return (
    <section className="panel">
      <div className="panel-header">
        <span>Meeting cards</span>
      </div>
      <div className="panel-body">
        {CARD_KEYS.map((key) => (
          <CardSection
            key={key}
            cardKey={key}
            items={cards[key] ?? []}
            onSendOp={onSendOp}
            onEvidenceClick={onEvidenceClick}
            onUndo={onUndo}
            lastSeqByTarget={lastSeqByTarget}
          />
        ))}
      </div>
    </section>
  );
}
