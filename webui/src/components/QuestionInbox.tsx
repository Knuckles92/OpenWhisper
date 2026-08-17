import { useState } from 'react';
import { ops, type Op, type Question } from '../types';
import EvidenceChip from './EvidenceChip';

interface QuestionInboxProps {
  questions: Question[];
  onSendOp?: (op: Op) => void;
  onEvidenceClick: (segmentId: string) => void;
  /** When true, omit the outer panel chrome (embedded in Captured rail). */
  embedded?: boolean;
  /** Hide answer / dismiss actions (print / archive). */
  readOnly?: boolean;
}

const SUGGEST_HIGH = 0.8;
const SUGGEST_MEDIUM = 0.4;

export function QuestionRow({
  q,
  onSendOp,
  onEvidenceClick,
  readOnly = false,
}: {
  q: Question;
  onSendOp?: (op: Op) => void;
  onEvidenceClick: (segmentId: string) => void;
  readOnly?: boolean;
}) {
  const [answerDraft, setAnswerDraft] = useState(q.answer ?? '');

  const showSuggestion =
    q.status === 'open' &&
    q.suggested_answer &&
    (q.suggested_confidence ?? 0) >= SUGGEST_MEDIUM &&
    (q.suggested_confidence ?? 0) < SUGGEST_HIGH;

  const submitAnswer = () => {
    const trimmed = answerDraft.trim();
    if (!trimmed) return;
    onSendOp?.(ops.answerQuestion(q.id, trimmed));
  };

  const useSuggestion = () => {
    if (q.suggested_answer) {
      setAnswerDraft(q.suggested_answer);
      onSendOp?.(ops.answerQuestion(q.id, q.suggested_answer));
    }
  };

  const addToNotes = () => {
    const text = q.answer || q.suggested_answer || q.text;
    if (text) onSendOp?.(ops.addItem('user_notes', text, undefined, q.evidence));
  };

  return (
    <div className={`question-item ${q.status}`}>
      <div className="capture-tag">Question</div>
      <p className="question-text">{q.text}</p>

      {q.answer && (
        <p style={{ margin: '0 0 8px' }}>
          {q.answer}
          {q.answer_source === 'audio' && (
            <span className="audio-badge" title="Answered from meeting audio">
              from audio
            </span>
          )}
        </p>
      )}

      {showSuggestion && (
        <div className="suggested-answer medium">
          Suggested: {q.suggested_answer}
          {(q.suggested_confidence ?? 0) > 0 && (
            <span style={{ marginLeft: 8, opacity: 0.7 }}>
              ({Math.round((q.suggested_confidence ?? 0) * 100)}%)
            </span>
          )}
        </div>
      )}

      {q.evidence.length > 0 && (
        <div className="evidence-row">
          {q.evidence.map((id) => (
            <EvidenceChip key={id} segmentId={id} onClick={onEvidenceClick} />
          ))}
        </div>
      )}

      {!readOnly && q.status === 'open' && (
        <>
          <textarea
            placeholder="Your answer…"
            value={answerDraft}
            onChange={(e) => setAnswerDraft(e.target.value)}
            rows={2}
          />
          <div className="question-actions no-print">
            <button type="button" className="primary" onClick={submitAnswer}>
              Answer
            </button>
            {showSuggestion && (
              <button type="button" onClick={useSuggestion}>
                Use suggestion
              </button>
            )}
            <button type="button" onClick={() => onSendOp?.(ops.dismissQuestion(q.id))}>
              Dismiss
            </button>
            <button type="button" className="ghost" onClick={addToNotes}>
              Add to notes
            </button>
          </div>
        </>
      )}

      {!readOnly && q.status === 'dismissed' && (
        <div className="question-actions no-print">
          <button type="button" onClick={() => onSendOp?.(ops.reopenQuestion(q.id))}>
            Reopen
          </button>
        </div>
      )}
    </div>
  );
}

export default function QuestionInbox({
  questions,
  onSendOp,
  onEvidenceClick,
  embedded = false,
  readOnly = false,
}: QuestionInboxProps) {
  const open = questions.filter((q) => q.status === 'open');
  const rest = questions.filter((q) => q.status !== 'open');

  const body = (
    <>
      {questions.length === 0 ? (
        <p className="empty-state">No questions yet.</p>
      ) : (
        <>
          {open.map((q) => (
            <QuestionRow
              key={q.id}
              q={q}
              onSendOp={onSendOp}
              onEvidenceClick={onEvidenceClick}
              readOnly={readOnly}
            />
          ))}
          {rest.length > 0 && (
            <>
              <h3 className="card-section-title" style={{ marginTop: 16 }}>
                Resolved / dismissed
              </h3>
              {rest.map((q) => (
                <QuestionRow
                  key={q.id}
                  q={q}
                  onSendOp={onSendOp}
                  onEvidenceClick={onEvidenceClick}
                  readOnly={readOnly}
                />
              ))}
            </>
          )}
        </>
      )}
    </>
  );

  if (embedded) {
    return <div className="capture-questions">{body}</div>;
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <span>Questions</span>
        <span className="meta">{open.length} open</span>
      </div>
      <div className="panel-body">{body}</div>
    </section>
  );
}
