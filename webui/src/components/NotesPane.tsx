import { useEffect, useRef, useState } from 'react';
import { isStuckToEnd, workspaceScroller } from '../scroll';
import { ops, type CardItem, type Op } from '../types';
import { sortedNoteItems } from '../state';
import { EvidenceRow } from './EvidenceChip';

interface NotesPaneProps {
  notes: CardItem[];
  status: string;
  cloudEnabled: boolean;
  intelligenceOnline: boolean;
  onSendOp?: (op: Op) => void;
  onEvidenceClick: (segmentId: string) => void;
  /** Host-only: revert the event at `seq`. Omitted for guests. */
  onUndo?: (seq: number) => void;
  /** Newest event seq per item id, from the reducer. */
  lastSeqByTarget: Record<string, number>;
  /** Hide composer and edit actions (print / archive). */
  readOnly?: boolean;
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
  readOnly = false,
}: {
  item: CardItem;
  onSendOp?: (op: Op) => void;
  onEvidenceClick: (segmentId: string) => void;
  onUndo?: (seq: number) => void;
  undoSeq?: number;
  readOnly?: boolean;
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
      onSendOp?.(ops.updateItem(item.id, { text: body, data }));
    }
    setEditing(false);
  };

  const beginEdit = () => {
    if (!readOnly) setEditing(true);
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
            onDoubleClick={beginEdit}
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
        <p className="note-body" onDoubleClick={beginEdit}>
          {item.text}
        </p>
      )}
      <EvidenceRow
        ids={item.evidence}
        onClick={onEvidenceClick}
        limit={readOnly ? 0 : undefined}
      />
      {!readOnly && (
        <div className="note-actions no-print">
          <span className="note-status">{statusLabel(item)}</span>
          <div className="note-action-buttons">
            <button type="button" onClick={() => setEditing(true)}>
              Edit
            </button>
            {item.status !== 'confirmed' && (
              <button type="button" onClick={() => onSendOp?.(ops.confirmItem(item.id))}>
                Confirm
              </button>
            )}
            <button
              type="button"
              onClick={() => onSendOp?.(item.pinned ? ops.unpinItem(item.id) : ops.pinItem(item.id))}
            >
              {item.pinned ? 'Unpin' : 'Pin'}
            </button>
            <button type="button" className="danger" onClick={() => onSendOp?.(ops.removeItem(item.id))}>
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
      )}
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
    <div className="note-composer no-print">
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
  readOnly = false,
}: NotesPaneProps) {
  const flowRef = useRef<HTMLDivElement | null>(null);
  // Stay put until the reader scrolls the workspace to the notes. Starting
  // "on" yanked the page to the first block and hid the topic hero.
  const stickToLatest = useRef(false);
  const lastCountRef = useRef(0);
  const sorted = sortedNoteItems(notes.filter((item) => item.status !== 'removed'));

  // Follow the newest block inside the workspace scroller only. Never move
  // the window — that hid Pause / End whenever the note taker wrote.
  useEffect(() => {
    if (readOnly) return undefined;
    const flow = flowRef.current;
    if (!flow) return undefined;
    const scroller = workspaceScroller(flow);
    if (!scroller) return undefined;
    const onScroll = () => {
      stickToLatest.current = isStuckToEnd(scroller);
    };
    scroller.addEventListener('scroll', onScroll, { passive: true });
    return () => scroller.removeEventListener('scroll', onScroll);
  }, [readOnly]);

  useEffect(() => {
    if (readOnly) return;
    const flow = flowRef.current;
    if (!flow || sorted.length === lastCountRef.current) return;
    lastCountRef.current = sorted.length;
    const scroller = workspaceScroller(flow);
    if (scroller && stickToLatest.current) {
      scroller.scrollTop = scroller.scrollHeight;
    }
  }, [readOnly, sorted.length]);

  return (
    <section className="panel notes-pane">
      <div className="panel-header">
        <span>Meeting Notes</span>
        <span className="meta">
          AI note taker · {sorted.length} {sorted.length === 1 ? 'block' : 'blocks'}
        </span>
      </div>
      <div className="panel-body">
        <div className="notes-flow" ref={flowRef}>
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
                readOnly={readOnly}
              />
            ))
          )}
        </div>
        {!readOnly && onSendOp && <NoteComposer onSendOp={onSendOp} />}
      </div>
    </section>
  );
}
