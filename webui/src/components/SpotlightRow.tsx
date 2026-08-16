import { useEffect, useState } from 'react';
import { ops, type CardItem, type CardKey, type MeetingStateDoc, type Op } from '../types';
import { CAPTURE_TAGS, selectSpotlightItems } from '../state';
import EvidenceChip from './EvidenceChip';

interface SpotlightRowProps {
  cards: MeetingStateDoc['cards'];
  status: string;
  cloudEnabled: boolean;
  intelligenceOnline: boolean;
  onSendOp: (op: Op) => void;
  onEvidenceClick: (segmentId: string) => void;
  /** Host-only: revert the event at `seq`. Omitted for guests. */
  onUndo?: (seq: number) => void;
  /** Newest event seq per item id, from the reducer. */
  lastSeqByTarget?: Record<string, number>;
}

const SPOTLIGHT_LIMIT = 3;

/** Compact relative age for an ISO timestamp, e.g. "just now", "6m ago", "1h 14m ago". */
function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const seconds = Math.max(0, (Date.now() - then) / 1000);
  if (seconds < 45) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  const restMinutes = minutes % 60;
  if (hours < 24) return restMinutes ? `${hours}h ${restMinutes}m ago` : `${hours}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function statusLabel(item: CardItem): string {
  if (item.status === 'proposed') return 'AI suggested';
  if (item.status === 'edited') return 'Edited';
  if (item.status === 'confirmed') return 'Confirmed';
  return '';
}

function SpotlightCard({
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

  useEffect(() => {
    if (!editing) setDraft(item.text);
  }, [item.text, editing]);

  const canUndo = onUndo !== undefined && undoSeq !== undefined;
  const tag = CAPTURE_TAGS[item.card as CardKey] ?? item.card;
  const statusClass =
    item.status === 'confirmed' ? 'confirmed' : item.status === 'proposed' ? 'proposed' : '';

  const saveEdit = () => {
    const trimmed = draft.trim();
    if (trimmed && trimmed !== item.text) {
      onSendOp(ops.updateItem(item.id, { text: trimmed }));
    }
    setEditing(false);
  };

  return (
    <article className={`spotlight-card ${statusClass}${item.pinned ? ' pinned' : ''}`}>
      <div className="spotlight-card-top">
        <span className="capture-tag">{tag}</span>
        {item.pinned && <span className="spotlight-pin">Pinned</span>}
      </div>
      {editing ? (
        <textarea
          className="spotlight-edit"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={saveEdit}
          onKeyDown={(e) => {
            if (e.key === 'Escape') setEditing(false);
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) saveEdit();
          }}
          autoFocus
        />
      ) : (
        <p className="spotlight-text" title={item.text} onDoubleClick={() => setEditing(true)}>
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
      <div className="spotlight-foot">
        <span className="spotlight-meta">
          {statusLabel(item)}
          {statusLabel(item) ? ' · ' : ''}
          {relativeTime(item.updated_at)}
        </span>
        <div className="spotlight-actions">
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
          <button
            type="button"
            className="danger"
            onClick={() => onSendOp(ops.removeItem(item.id))}
          >
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
        </div>
      </div>
    </article>
  );
}

function ghostText(status: string, cloudEnabled: boolean, intelligenceOnline: boolean): string {
  if (status === 'ending') return 'Wrapping up insights…';
  if (!cloudEnabled) return 'Enable cloud insights to generate live insights.';
  if (!intelligenceOnline) return 'Cloud intelligence is offline';
  return 'Listening for insights…';
}

/** Prominent, always-evolving trio of insights ranked under the topic hero. */
export default function SpotlightRow({
  cards,
  status,
  cloudEnabled,
  intelligenceOnline,
  onSendOp,
  onEvidenceClick,
  onUndo,
  lastSeqByTarget,
}: SpotlightRowProps) {
  const picks = selectSpotlightItems(cards, SPOTLIGHT_LIMIT);
  const ghosts = SPOTLIGHT_LIMIT - picks.length;
  const ghostMessage = ghostText(status, cloudEnabled, intelligenceOnline);

  if (picks.length === 0) {
    return (
      <div className="spotlight-row">
        <div className="spotlight-ghost">{ghostMessage}</div>
      </div>
    );
  }

  return (
    <div className="spotlight-row">
      {picks.map(({ item }) => (
        <SpotlightCard
          key={item.id}
          item={item}
          onSendOp={onSendOp}
          onEvidenceClick={onEvidenceClick}
          onUndo={onUndo}
          undoSeq={lastSeqByTarget?.[item.id]}
        />
      ))}
      {Array.from({ length: ghosts }).map((_, i) => (
        <div key={`ghost-${i}`} className="spotlight-ghost">
          {ghostMessage}
        </div>
      ))}
    </div>
  );
}
