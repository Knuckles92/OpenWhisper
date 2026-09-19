import { useState } from 'react';
import type { CardItem, MeetingStateDoc, Op, ReviewQuestion } from '../types';
import { clock } from '../report';

export function ReviewCorrections({ state }: {state: MeetingStateDoc}) {
  const answers = (state.insight_review?.questions ?? []).filter(q => q.correction && !q.superseded);
  const uncertain = Object.values(state.cards).flat().filter(i => i.status !== 'removed' && ['provisional', 'unsupported'].includes(i.review?.state ?? '')).length;
  if (!answers.length && !uncertain) return null;
  return <section className="review-corrections" aria-label="User clarifications">
    <h3>{answers.length ? 'User clarifications' : 'Insights to verify'}</h3>
    {uncertain > 0 && <p>{uncertain} insights still need clarification. Treat their wording in this report as provisional.</p>}
    {answers.length > 0 && <p>These corrections supersede earlier wording on the same points.</p>}
    <ul>{answers.map(q => <li key={q.id}>{q.correction}</li>)}</ul>
  </section>;
}

function ReviewRow({q, item, onSend, onEvidenceClick}: {
  q: ReviewQuestion; item?: CardItem; onSend: (op: Op) => Promise<boolean>;
  onEvidenceClick: (id: string) => void;
}) {
  const [editing, setEditing] = useState(q.field === 'owner' || q.field === 'deadline');
  const [text, setText] = useState(item?.text ?? '');
  const [owner, setOwner] = useState(q.owners.some(p => p.id === item?.data.owner_participant_id) ? String(item?.data.owner_participant_id) : '');
  const [ownerName, setOwnerName] = useState('');
  const [deadline, setDeadline] = useState(String(item?.data.deadline ?? item?.data.due_date ?? ''));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const stale = q.superseded || !item || item.revision !== q.revision || (item.status === 'removed' && !q.correction);
  const send = async (op: Op) => {
    if (busy) return;
    setBusy(true); setError('');
    try {
      if (!await onSend(op)) setError('Could not save. The insight may have changed; refresh or retry the review. Your draft is kept.');
    } catch {
      setError('Could not save. Check the connection and try again. Your draft is kept.');
    } finally { setBusy(false); }
  };
  const answer = (choice: string) => {
    if (choice === 'edit' && !editing) {setEditing(true); return;}
    void send({op: 'review_answer', question_id: q.id, answer: choice,
      text: editing ? text.trim() : item?.text, owner_participant_id: owner && owner !== 'someone_else' ? owner : null,
      owner_name: owner === 'someone_else' ? ownerName.trim() : '', deadline: deadline.trim()});
  };
  return <article className="review-question">
    <h3>{q.text}</h3>
    {q.status === 'answered' ? <p>{q.correction}</p> : <blockquote>{item?.text ?? 'This insight is no longer available.'}</blockquote>}
    {q.status !== 'answered' && <p className="muted">{q.reason}</p>}
    <details><summary>Source excerpts</summary>
      {q.sources.length ? q.sources.map(source => <blockquote key={source.id}>
        <button type="button" className="ghost" onClick={() => onEvidenceClick(source.id)}>{clock(source.start_s)}</button> {source.text}
      </blockquote>) : <p>No source excerpt is available.</p>}
    </details>
    {q.status === 'open' && !stale && <>
      {editing && <div className="review-editor">
        {q.field === 'owner' && <>
          <label>Owner<select value={owner} disabled={busy} onChange={e => setOwner(e.target.value)}>
            <option value="">Unassigned</option>
            {q.owners.map(p => <option value={p.id} key={p.id}>{p.display_name}</option>)}
            <option value="someone_else">Someone else</option>
          </select></label>
          {owner === 'someone_else' && <label>Name<input value={ownerName} maxLength={120} disabled={busy} onChange={e => setOwnerName(e.target.value)} /></label>}
        </>}
        {q.field === 'deadline' && <label>Agreed deadline (leave blank if none)<input value={deadline} maxLength={120} disabled={busy} onChange={e => setDeadline(e.target.value)} /></label>}
        <label>Corrected insight wording<textarea value={text} maxLength={3500} rows={3} disabled={busy} onChange={e => setText(e.target.value)} /></label>
        {(q.field === 'owner' || q.field === 'deadline') && <p className="muted">Update the wording to match your clarification.</p>}
      </div>}
      <div className="question-actions">
        {Object.entries(q.choices).filter(([id]) => !editing || id === 'edit' || id === 'incorrect').map(([id, label]) =>
          <button key={id} type="button" disabled={busy || (editing && !text.trim()) || (owner === 'someone_else' && !ownerName.trim())}
            onClick={() => answer(id)}>{id === 'edit' && editing ? 'Save correction' : label}</button>)}
        <button type="button" disabled={busy} onClick={() => void send({op: 'review_skip', question_id: q.id})}>Skip</button>
      </div>
    </>}
    {q.status !== 'open' && !stale && <button type="button" disabled={busy} onClick={() => void send({op: 'review_reopen', question_id: q.id})}>
      {q.status === 'answered' ? 'Revise answer' : 'Reopen'}
    </button>}
    {stale && <p role="status">This insight changed. The earlier review no longer applies. Retry the review to use its latest wording.</p>}
    {error && <p role="alert">{error}</p>}
  </article>;
}

export default function InsightReview({state, onSendOp, onRetry, onEvidenceClick}: {
  state: MeetingStateDoc; onSendOp: (op: Op) => Promise<boolean>;
  onRetry: () => Promise<unknown>; onEvidenceClick: (id: string) => void;
}) {
  const [later, setLater] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const review = state.insight_review;
  if (!review?.enabled || state.status !== 'ended') return null;
  const items = Object.values(state.cards).flat();
  const questions = review.questions ?? [];
  const open = questions.filter(q => q.status === 'open');
  const closed = questions.filter(q => q.status !== 'open');
  const pending = review.status === 'running' || state.finalization?.status === 'running' || state.finalization?.status === 'pending';
  const retry = async () => {
    setBusy(true); setError('');
    try { await onRetry(); } catch { setError('Could not start the review. Try again when connected.'); }
    finally {setBusy(false);}
  };
  return <section className="panel insight-review no-print" aria-label="Review uncertain insights (Experimental)">
    <div className="panel-header"><span>Review uncertain insights (Experimental)</span><span>{open.length ? `${open.length} to review` : ''}</span></div>
    <div className="panel-body">
      <p role="status">{review.message || 'Review will begin after the final insights are saved.'}</p>
      <p className="muted">Your meeting is saved. Answers update the insight and notes. Skipping keeps the uncertainty.</p>
      {open.length > 0 && <button type="button" onClick={() => setLater(!later)}>{later ? 'Continue review' : 'Review later'}</button>}
      {!later && !pending && open.map(q => <ReviewRow key={`${q.id}:${q.revision}:${q.status}`} q={q} item={items.find(i => i.id === q.item_id) ?? q.insight} onSend={onSendOp} onEvidenceClick={onEvidenceClick} />)}
      {closed.length > 0 && <details><summary>Reviewed or skipped ({closed.length})</summary>
        {closed.map(q => <ReviewRow key={`${q.id}:${q.revision}:${q.status}`} q={q} item={items.find(i => i.id === q.item_id) ?? q.insight} onSend={onSendOp} onEvidenceClick={onEvidenceClick} />)}
      </details>}
      {!pending && <button type="button" disabled={busy || !state.cloud_enabled} onClick={() => void retry()}>{busy ? 'Starting…' : 'Retry review'}</button>}
      {error && <p role="alert">{error}</p>}
    </div>
  </section>;
}
