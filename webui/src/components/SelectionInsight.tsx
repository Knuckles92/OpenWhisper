import { useEffect, useRef, useState } from 'react';
import type { Op } from '../types';
import { insightOp } from '../corrections';
import './selectionInsight.css';

export default function SelectionInsight({ onSend, live, online }: {
  onSend: (op: Op) => Promise<boolean>; live: boolean; online: boolean;
}) {
  const [selected, setSelected] = useState('');
  const [draft, setDraft] = useState<string | null>(null);
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
      let text = window.getSelection()?.toString() ?? '';
      if (active instanceof HTMLTextAreaElement || (active instanceof HTMLInputElement && ['text', 'search'].includes(active.type))) {
        text = active.value.slice(active.selectionStart ?? 0, active.selectionEnd ?? 0);
      }
      setSelected(text.trim().slice(0, 1000));
    };
    document.addEventListener('pointerup', capture);
    document.addEventListener('keyup', capture);
    return () => {
      document.removeEventListener('pointerup', capture);
      document.removeEventListener('keyup', capture);
    };
  }, []);

  useEffect(() => {
    if (draft !== null) dialogRef.current?.showModal();
  }, [draft]);

  const close = () => {
    if (busy) return;
    dialogRef.current?.close();
    setDraft(null);
    setSelected('');
    focusRef.current?.focus();
  };

  return <>
    {selected && draft === null && <button className="selection-insight-trigger primary"
      onPointerDown={(event) => event.preventDefault()}
      onClick={() => {
        focusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
        setDraft(selected); setInsight(''); setReplacement(''); setMessage('');
      }}>Offer insight to agent</button>}
    {message && draft === null && <div className="selection-insight-message" role="status">{message}
      <button className="ghost" onClick={() => setMessage('')} aria-label="Dismiss insight status">×</button>
    </div>}
    <dialog ref={dialogRef} className="confirm-dialog selection-insight-dialog" aria-labelledby="selection-insight-title"
      onCancel={(event) => { event.preventDefault(); close(); }}>
      <form className="confirm-card" onSubmit={async (event) => {
        event.preventDefault();
        if (busy || draft === null || (!insight.trim() && !replacement.trim())) return;
        setBusy(true); setMessage('');
        try {
          const ok = await onSend(insightOp(draft, insight, replacement));
          if (!ok) { setMessage('Could not save. Your insight is still here; please retry.'); return; }
          dialogRef.current?.close(); setDraft(null); setSelected('');
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
        <p>Term corrections apply to whole words and phrases throughout this meeting, including future transcription.</p>
        <label>What should the agent understand?
          <textarea value={insight} maxLength={1500} rows={4} disabled={busy}
            placeholder="e.g. We are discussing Anthropic, the AI company. Reconsider the notes based on that."
            onChange={(event) => setInsight(event.target.value)} />
        </label>
        <p>{live && online ? 'Saved in Notes with attribution. The agent will review its current understanding.' : 'Saved in Notes. Agent review will happen when meeting intelligence runs again.'}</p>
        {message && <p role="alert">{message}</p>}
        <div className="confirm-actions">
          <button type="button" className="ghost" disabled={busy} onClick={close}>Cancel</button>
          <button type="submit" className="primary" disabled={busy || (!insight.trim() && !replacement.trim())}>{busy ? 'Saving…' : 'Send insight'}</button>
        </div>
      </form>
    </dialog>
  </>;
}
