import { useEffect, useRef, useState } from 'react';
import type { Op } from '../types';
import { insightOp } from '../corrections';
import './selectionInsight.css';

type PassageTarget = { segmentId: string; occurrenceIndex: number; baseFingerprint: string };

type PassageResolver = (segmentId: string, source: string, displayStart: number, displayed: string) =>
  { occurrenceIndex: number; baseFingerprint: string } | null;

export function transcriptTarget(selection: Selection | null, source: string, resolve: PassageResolver): PassageTarget | null {
  if (!selection || selection.isCollapsed || selection.rangeCount !== 1 || !source) return null;
  const range = selection.getRangeAt(0);
  const elementOf = (node: Node) => node instanceof Element ? node : node.parentElement;
  const startRow = elementOf(range.startContainer)?.closest<HTMLElement>('[data-transcript-id]');
  const endRow = elementOf(range.endContainer)?.closest<HTMLElement>('[data-transcript-id]');
  const text = startRow?.querySelector('.segment-text');
  if (!startRow?.dataset.transcriptId || startRow !== endRow || !text
      || !text.contains(range.startContainer) || !text.contains(range.endContainer)) return null;
  const before = range.cloneRange();
  before.selectNodeContents(text);
  before.setEnd(range.startContainer, range.startOffset);
  const selected = range.toString();
  const leading = selected.length - selected.trimStart().length;
  const target = resolve(startRow.dataset.transcriptId, source,
    before.toString().length + leading, text.textContent ?? '');
  return target === null ? null : { segmentId: startRow.dataset.transcriptId, ...target };
}

export default function SelectionInsight({ onSend, live, online, resolvePassageSelection }: {
  onSend: (op: Op) => Promise<boolean>; live: boolean; online: boolean;
  resolvePassageSelection: PassageResolver;
}) {
  const [selected, setSelected] = useState('');
  const [selectedTarget, setSelectedTarget] = useState<PassageTarget | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [draftTarget, setDraftTarget] = useState<PassageTarget | null>(null);
  const [scope, setScope] = useState<'occurrence' | 'meeting' | ''>('');
  const [insight, setInsight] = useState('');
  const [replacement, setReplacement] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const dialogRef = useRef<HTMLDialogElement>(null);
  const focusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const capture = () => {
      if (dialogRef.current?.open) return;
      const active = document.activeElement;
      const selection = window.getSelection();
      let text = selection?.toString() ?? '';
      let target = transcriptTarget(selection, text.trim(), resolvePassageSelection);
      if (active instanceof HTMLTextAreaElement || (active instanceof HTMLInputElement && ['text', 'search'].includes(active.type))) {
        text = active.value.slice(active.selectionStart ?? 0, active.selectionEnd ?? 0);
        target = null;
      }
      setSelected(text.trim().slice(0, 1000));
      setSelectedTarget(target);
    };
    document.addEventListener('pointerup', capture);
    document.addEventListener('keyup', capture);
    return () => {
      document.removeEventListener('pointerup', capture);
      document.removeEventListener('keyup', capture);
    };
  }, [resolvePassageSelection]);

  useEffect(() => {
    if (draft !== null) dialogRef.current?.showModal();
  }, [draft]);

  const close = () => {
    if (busy) return;
    dialogRef.current?.close();
    setDraft(null);
    setDraftTarget(null);
    setSelected('');
    focusRef.current?.focus();
  };

  return <>
    {selected && draft === null && <button className="selection-insight-trigger primary"
      onPointerDown={(event) => event.preventDefault()}
      onClick={() => {
        focusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        setDraft(selected); setDraftTarget(selectedTarget);
        setScope(selectedTarget ? 'occurrence' : '');
        setInsight(''); setReplacement(''); setMessage('');
      }}>Offer insight to agent</button>}
    {message && draft === null && <div className="selection-insight-message" role="status">{message}
      <button className="ghost" onClick={() => setMessage('')} aria-label="Dismiss insight status">×</button>
    </div>}
    <dialog ref={dialogRef} className="confirm-dialog selection-insight-dialog" aria-labelledby="selection-insight-title"
      onCancel={(event) => { event.preventDefault(); close(); }}>
      <form className="confirm-card" onSubmit={async (event) => {
        event.preventDefault();
        if (busy || draft === null || (!insight.trim() && !replacement.trim())
            || (replacement.trim() && (!scope || (scope === 'occurrence' && !draftTarget)))) return;
        setBusy(true); setMessage('');
        try {
          const target = scope === 'occurrence' && draftTarget ? draftTarget : 'meeting';
          const ok = await onSend(insightOp(draft, insight, replacement, target));
          if (!ok) { setMessage('Could not save. Your draft is still here. If the transcript changed, reselect the passage and try again.'); return; }
          dialogRef.current?.close(); setDraft(null); setDraftTarget(null); setSelected('');
          setMessage(live && online ? 'Saved. Agent review requested.' : 'Saved for the next agent pass.');
          focusRef.current?.focus();
        } catch (error) {
          setMessage(error instanceof Error ? error.message : 'Could not save. Please retry.');
        } finally { setBusy(false); }
      }}>
        <h2 id="selection-insight-title">Offer insight to agent</h2>
        <blockquote>{draft}</blockquote>
        <label>Correct this term (optional)
          <input autoFocus value={replacement} maxLength={120} disabled={busy || (draft?.length ?? 0) > 120}
            placeholder="e.g. Anthropic" onChange={(event) => setReplacement(event.target.value)} />
        </label>
        {replacement.trim() && <fieldset className="selection-insight-scope" disabled={busy}>
          <legend>Apply correction</legend>
          {draftTarget && <label><input type="radio" name="correction-scope" checked={scope === 'occurrence'}
            onChange={() => setScope('occurrence')} />Only this occurrence</label>}
          <label><input type="radio" name="correction-scope" checked={scope === 'meeting'}
            onChange={() => setScope('meeting')} />Throughout this meeting, including future transcription</label>
          {!draftTarget && <p>To correct one occurrence, select text inside a Conversation passage. Choose the meeting-wide option explicitly for other selections.</p>}
        </fieldset>}
        <label>What should the agent understand?
          <textarea value={insight} maxLength={1500} rows={4} disabled={busy}
            placeholder="e.g. We are discussing Anthropic, the AI company. Reconsider the notes based on that."
            onChange={(event) => setInsight(event.target.value)} />
        </label>
        <p>{live && online ? 'Saved in Notes with attribution. The agent will review its current understanding.' : 'Saved in Notes. Agent review will happen when meeting intelligence runs again.'}</p>
        {message && <p role="alert">{message}</p>}
        <div className="confirm-actions">
          <button type="button" className="ghost" disabled={busy} onClick={close}>Cancel</button>
          <button type="submit" className="primary" disabled={busy || (!insight.trim() && !replacement.trim())
            || Boolean(replacement.trim() && (!scope || (scope === 'occurrence' && !draftTarget)))}>{busy ? 'Saving…' : 'Send insight'}</button>
        </div>
      </form>
    </dialog>
  </>;
}
