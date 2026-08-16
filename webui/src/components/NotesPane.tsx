import { useEffect, useRef, useState } from 'react';
import { ops, type CardItem, type Op } from '../types';
import { sortedNoteItems } from '../state';
import EvidenceChip from './EvidenceChip';

interface NotesPaneProps {
  notes: CardItem[];
  status: string;
  cloudEnabled: boolean;
  intelligenceOnline: boolean;
  onSendOp: (op: Op) => void;
  onEvidenceClick: (segmentId: string) => void;
  /** Host-only: revert the event at `seq`. Omitted for guests. */
  onUndo?: (seq: number) => void;
  /** Newest event seq per item id, from the reducer. */
  lastSeqByTarget: Record<string, number>;
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function noteHeading(item: CardItem): string {
  const heading = item.data.heading;
  return typeof heading === 'string' ? heading : '';
}

function noteStartS(item: CardItem): number | null {
  const startS = item.data.start_s;
  return typeof startS === 'number' && Number.isFinite(startS) ? startS : null;
}

function statusLabel(item: CardItem): string {
  if (item.status === 'proposed') return 'AI note';
  if (item.status === 'edited') return 'Edited';
  if (item.status === 'confirmed') return 'Confirmed';
  return '';
}

function NoteBlock({
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
  const [headingDraft, setHeadingDraft] = useState(noteHeading(item));
  const [bodyDraft, setBodyDraft] = useState(item.text);

  useEffect(() => {
    if (!editing) {
      setHeadingDraft(noteHeading(item));
      setBodyDraft(item.text);
    }
  }, [item.text, item.data, editing]);

  const canUndo = onUndo !== undefined && undoSeq !== undefined;
  const statusClass =
    item.status === 'confirmed' ? 'confirmed' : item.status === 'proposed' ? 'proposed' : '';
  const startS = noteStartS(item);

  const saveEdit = () => {
    const body = bodyDraft.trim();
    if (body && (body !== item.text || headingDraft.trim() !== noteHeading(item))) {
      const data = { ...item.data };
      if (headingDraft.trim()) data.heading = headingDraft.trim();
      else delete data.heading;
      onSendOp(ops.updateItem(item.id, { text: body, data }));
    }
    setEditing(false);
  };

  return (
    <article className={`note-block ${statusClass}${item.pinned ? ' pinned' : ''}`}>
      <div className="note-block-top">
        {startS !== null && <time className="note-time">{formatTime(startS)}</time>}
        {editing ? (
          <input
            className="note-heading-edit"
            value={headingDraft}
            onChange={(e) => setHeadingDraft(e.target.value)}
            placeholder="Heading (optional)"
            aria-label="Note heading"
          />
        ) : (
          <h4
            className={`note-heading${noteHeading(item) ? '' : ' empty'}`}
            onDoubleClick={() => setEditing(true)}
          >
            {noteHeading(item) || 'Untitled note'}
          </h4>
        )}
        {item.pinned && <span className="note-pin">Pinned</span>}
      </div>
      {editing ? (
        <textarea
          className="note-body-edit"
          value={bodyDraft}
          onChange={(e) => setBodyDraft(e.target.value)}
          onBlur={saveEdit}
          onKeyDown={(e) => {
            if (e.key === 'Escape') setEditing(false);
          }}
          autoFocus
        />
      ) : (
        <p className="note-body" onDoubleClick={() => setEditing(true)}>
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
      <div className="note-actions">
        <span className="note-status">{statusLabel(item)}</span>
        <div className="note-action-buttons">
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
              title="Undo the last change to this note"
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

function NoteComposer({ onSendOp }: { onSendOp: (op: Op) => void }) {
  const [heading, setHeading] = useState('');
  const [body, setBody] = useState('');

  const addNote = () => {
    const trimmed = body.trim();
    if (!trimmed) return;
    onSendOp(ops.addItem('live_notes', trimmed, heading.trim() ? { heading: heading.trim() } : undefined));
    setHeading('');
    setBody('');
  };

  return (
    <div className="note-composer">
      <input
        type="text"
        placeholder="Heading (optional)"
        value={heading}
        onChange={(e) => setHeading(e.target.value)}
        aria-label="Note heading"
      />
      <textarea
        placeholder="Add your own note…"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) addNote();
        }}
        aria-label="Note text"
      />
      <button type="button" className="primary" onClick={addNote} disabled={!body.trim()}>
        Add note
      </button>
    </div>
  );
}

function ghostText(status: string, cloudEnabled: boolean, intelligenceOnline: boolean): string {
  if (status === 'ending' || status === 'ended') return 'The note taker\u2019s page is complete.';
  if (!cloudEnabled) return 'Enable cloud insights and the AI note taker will keep minutes.';
  if (!intelligenceOnline) return 'The AI note taker is offline.';
  return 'The AI note taker is listening\u2026';
}

/** The AI note taker's page: live, chronological meeting minutes. */
export default function NotesPane({
  notes,
  status,
  cloudEnabled,
  intelligenceOnline,
  onSendOp,
  onEvidenceClick,
  onUndo,
  lastSeqByTarget,
}: NotesPaneProps) {
  const flowRef = useRef<HTMLDivElement | null>(null);
  const stickToLatest = useRef(true);
  const lastCountRef = useRef(0);
  const sorted = sortedNoteItems(notes.filter((item) => item.status !== 'removed'));

  // Follow the page like a note taker writes: keep the newest block in view
  // unless the reader scrolled up to re-read earlier notes.
  useEffect(() => {
    const flow = flowRef.current;
    if (!flow) return;
    if (sorted.length !== lastCountRef.current) {
      lastCountRef.current = sorted.length;
      if (stickToLatest.current) {
        flow.scrollTop = flow.scrollHeight;
      }
    }
  }, [sorted.length, sorted]);

  const handleScroll = () => {
    const flow = flowRef.current;
    if (!flow) return;
    stickToLatest.current = flow.scrollHeight - flow.scrollTop - flow.clientHeight < 80;
  };

  return (
    <section className="panel notes-pane">
      <div className="panel-header">
        <span>Meeting Notes</span>
        <span className="meta">
          AI note taker · {sorted.length} {sorted.length === 1 ? 'block' : 'blocks'}
        </span>
      </div>
      <div className="panel-body">
        <div className="notes-flow" ref={flowRef} onScroll={handleScroll}>
          {sorted.length === 0 ? (
            <p className="empty-state">{ghostText(status, cloudEnabled, intelligenceOnline)}</p>
          ) : (
            sorted.map((item) => (
              <NoteBlock
                key={item.id}
                item={item}
                onSendOp={onSendOp}
                onEvidenceClick={onEvidenceClick}
                onUndo={onUndo}
                undoSeq={lastSeqByTarget[item.id]}
              />
            ))
          )}
        </div>
        <NoteComposer onSendOp={onSendOp} />
      </div>
    </section>
  );
}
