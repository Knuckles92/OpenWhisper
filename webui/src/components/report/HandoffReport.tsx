import { useEffect, useRef, useState } from 'react';
import { initials, speakerColor } from '../../people';
import type { MeetingInfo, MeetingStateDoc, Segment } from '../../types';
import { ItemFlags } from './EditorialReport';
import {
  actionGroups,
  itemDeadline,
  keptItems,
  mailtoHref,
  openQuestions,
  ownerEmailText,
  recapEmail,
  type OwnerGroup,
} from './followThrough';
import ReportTimestamp from './ReportTimestamp';
import './handoffReport.css';

interface HandoffReportProps {
  state: MeetingStateDoc;
  segments: Segment[];
  segs: Map<string, Segment>;
  meeting?: MeetingInfo | null;
  onEvidenceClick?: (segmentId: string) => void;
  onSeek?: (seconds: number) => void;
}

type CopyState = { key: string; ok: boolean } | null;

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

export default function HandoffReport({
  state,
  segs,
  meeting,
  onEvidenceClick,
  onSeek,
}: HandoffReportProps) {
  const title = state.title || meeting?.display_title || meeting?.title || 'Meeting';
  const decisions = keptItems(state.cards.decisions);
  const groups = actionGroups(state);
  const actionCount = groups.reduce((total, group) => total + group.items.length, 0);
  const open = openQuestions(state);
  const email = recapEmail(state, title);
  const participants = Object.values(state.participants);
  const [copied, setCopied] = useState<CopyState>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(timer.current), []);

  const copy = async (key: string, text: string) => {
    clearTimeout(timer.current);
    try {
      await navigator.clipboard.writeText(text);
      setCopied({ key, ok: true });
    } catch {
      setCopied({ key, ok: false });
    }
    timer.current = setTimeout(() => setCopied(null), 2400);
  };

  const copyLabel = (key: string, idle: string) =>
    copied?.key === key ? (copied.ok ? 'Copied' : 'Copy failed') : idle;

  const avatar = (group: OwnerGroup) => (
    <span
      className={`ho-avatar${group.key === null ? ' none' : ''}`}
      style={group.key && state.participants[group.key]
        ? { background: speakerColor(group.key, participants) }
        : undefined}
      aria-hidden="true"
    >
      {group.key === null ? '?' : initials(group.name)}
    </span>
  );

  return (
    <div className="ho">
      <header className="ho-head">
        <span className="ho-kicker">Handoff</span>
        <h1 className="ho-title">{title}</h1>
        <p className="ho-summary">
          <span><b>{plural(decisions.length, 'decision')}</b></span>
          <span><b>{plural(actionCount, 'action')}</b></span>
          <span className={open.length ? 'ho-open' : undefined}>
            <b>{plural(open.length, 'open question')}</b>
          </span>
        </p>
      </header>

      <div className="ho-grid">
        <section className="ho-owners" aria-label="Actions by owner">
          {groups.length === 0 ? (
            <p className="ho-empty">No action items were captured in this meeting.</p>
          ) : (
            groups.map((group) => {
              const key = `owner:${group.key ?? 'none'}`;
              return (
                <article key={key} className="ho-card" aria-label={`${group.name}: ${plural(group.items.length, 'action')}`}>
                  <div className="ho-card-head">
                    {avatar(group)}
                    <h2 className="ho-owner">{group.name}</h2>
                    <span className="ho-count">{plural(group.items.length, 'task')}</span>
                  </div>
                  <ul className="ho-tasks">
                    {group.items.map((item) => {
                      const due = itemDeadline(item);
                      return (
                        <li key={item.id}>
                          <span className="ho-box" aria-hidden="true" />
                          <div className="ho-task">
                            <span>{item.text}</span>
                            <ItemFlags item={item} />
                            <span className="ho-task-meta">
                              {due && <span className="ho-due">Due: {due}</span>}
                              <ReportTimestamp
                                evidence={item.evidence}
                                segs={segs}
                                limit={1}
                                onEvidenceClick={onEvidenceClick}
                                onSeek={onSeek}
                              />
                            </span>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                  <div className="ho-card-actions no-print">
                    <button type="button" onClick={() => void copy(key, ownerEmailText(group, title, segs))}>
                      {copyLabel(key, 'Copy for email')}
                    </button>
                  </div>
                </article>
              );
            })
          )}

          {open.length > 0 && (
            <section className="ho-card ho-questions" aria-labelledby="ho-open-h">
              <h2 id="ho-open-h" className="ho-owner">Still open</h2>
              <ul>
                {open.map((question) => <li key={question.id}>{question.text}</li>)}
              </ul>
            </section>
          )}
        </section>

        <section className="ho-email" aria-labelledby="ho-email-h">
          <div className="ho-email-head">
            <h2 id="ho-email-h" className="ho-h">Recap email</h2>
            <span className="ho-note">Draft from what the meeting captured</span>
          </div>
          <div className="ho-letter">
            <div className="ho-letter-row"><span>Subject</span><strong>{email.subject}</strong></div>
            <pre className="ho-letter-body">{email.body}</pre>
          </div>
          <div className="ho-email-actions no-print">
            <button type="button" className="primary" onClick={() => void copy('email', `Subject: ${email.subject}\n\n${email.body}`)}>
              {copyLabel('email', 'Copy email')}
            </button>
            <a className="ho-mail-link" href={mailtoHref(email)}>Open in email app</a>
          </div>
        </section>
      </div>

      <p className="sr-only" role="status" aria-live="polite">
        {copied?.ok ? 'Copied to the clipboard.' : ''}
      </p>
      {copied && !copied.ok && (
        <p className="ho-error no-print" role="alert">
          The clipboard is unavailable here. Select the text and copy it instead.
        </p>
      )}
    </div>
  );
}
