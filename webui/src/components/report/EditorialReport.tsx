import { useId } from 'react';
import { topicChapters } from '../../chapters';
import {
  clock,
  formatMeetingWhen,
  meetingDuration,
  speakerName,
  splitSummary,
} from '../../report';
import type { CardItem, HighlightPulse, MeetingInfo, MeetingStateDoc, Segment } from '../../types';
import { citationLabel } from '../CitationBadge';
import { PULSE_LABELS } from '../HighlightPulseStrip';
import {
  answeredQuestions,
  itemDeadline,
  itemOwnerName,
  keptItems,
  openQuestions,
} from './followThrough';
import ReportTimestamp from './ReportTimestamp';
import './editorialReport.css';

interface EditorialReportProps {
  state: MeetingStateDoc;
  segments: Segment[];
  segs: Map<string, Segment>;
  meeting?: MeetingInfo | null;
  onEvidenceClick?: (segmentId: string) => void;
  onSeek?: (seconds: number) => void;
}

/** Review and citation caveats, spelled out so they survive print and paste. */
export function ItemFlags({ item }: { item: CardItem }) {
  const review = item.review?.state === 'unsupported'
    ? 'Needs verification'
    : item.review?.state === 'provisional' ? 'Provisional' : '';
  const citation = citationLabel(item);
  if (!review && !citation) return null;
  return (
    <span className="ed-flags">
      {review && <span className="ed-flag">{review}</span>}
      {citation && <span className="ed-flag">Advisory: {citation}</span>}
    </span>
  );
}

/** The quote worth hearing: a decision if there is one, else the surest highlight. */
export function pickPullQuote(pulses: HighlightPulse[] | undefined): HighlightPulse | null {
  const usable = (pulses ?? []).filter((pulse) => pulse.text?.trim() && Number.isFinite(pulse.start_s));
  if (!usable.length) return null;
  const rank = (pulse: HighlightPulse) => (pulse.kind === 'decision' ? 1 : 0);
  return [...usable].sort(
    (a, b) => rank(b) - rank(a) || (b.probability ?? 0) - (a.probability ?? 0) || a.start_s - b.start_s,
  )[0];
}

export default function EditorialReport({
  state,
  segments,
  segs,
  meeting,
  onEvidenceClick,
  onSeek,
}: EditorialReportProps) {
  const anchor = useId().replace(/:/g, '');
  const title = state.title || meeting?.display_title || meeting?.title || 'Meeting';
  const people = Object.values(state.participants);
  const recorded = Number(meeting?.duration_s);
  const duration = Number.isFinite(recorded) && recorded > 0 ? recorded : meetingDuration(segments);
  const startedAt = typeof meeting?.started_at === 'string' ? meeting.started_at : null;
  const dek = [
    formatMeetingWhen(startedAt, duration),
    people.length ? `${people.length} ${people.length === 1 ? 'person' : 'people'}` : '',
  ].filter(Boolean).join(' · ');
  const { lede, rest } = splitSummary(state.rolling_summary || '');
  const decisions = keptItems(state.cards.decisions);
  const actions = keptItems(state.cards.action_items);
  const answered = answeredQuestions(state);
  const open = openQuestions(state);
  const chapters = topicChapters(state.topic?.history ?? [], startedAt, duration || undefined);
  const quote = pickPullQuote(state.live_highlights);
  const quoteSegment = quote ? segs.get(quote.segment_id) : undefined;
  const quoteSpeaker = quoteSegment
    ? speakerName(state.participants, quoteSegment.speaker_participant_id, quoteSegment.channel)
    : '';

  const sections = [
    { id: `${anchor}-lead`, label: 'The lead', show: Boolean(lede) },
    { id: `${anchor}-decided`, label: 'Decided', show: decisions.length > 0 },
    { id: `${anchor}-actions`, label: 'Actions', show: actions.length > 0 },
    { id: `${anchor}-answered`, label: 'Answered', show: answered.length > 0 },
    { id: `${anchor}-open`, label: 'Still open', show: open.length > 0 },
    { id: `${anchor}-chapters`, label: 'Chapters', show: chapters.length > 0 },
  ].filter((section) => section.show);

  return (
    <article className="ed" aria-labelledby={`${anchor}-title`}>
      <div className="ed-grid">
      {sections.length > 1 && (
        <nav className="ed-toc no-print" aria-label="Report sections">
          <span className="ed-toc-label">In this report</span>
          <ol>
            {sections.map((section) => (
              <li key={section.id}><a href={`#${section.id}`}>{section.label}</a></li>
            ))}
          </ol>
        </nav>
      )}

      <div className="ed-page">
        <header className="ed-head">
          <span className="ed-kicker">Meeting report</span>
          <h1 id={`${anchor}-title`} className="ed-title">{title}</h1>
          {dek && <p className="ed-dek">{dek}</p>}
          {people.length > 0 && (
            <p className="ed-byline">{people.map((person) => person.display_name).join(', ')}</p>
          )}
        </header>

        <div className="ed-columns">
          <div className="ed-main">
            {lede ? (
              <section id={`${anchor}-lead`} className="ed-lead">
                <p className="ed-lede">{lede}</p>
                {rest.map((paragraph) => <p key={paragraph.slice(0, 32)}>{paragraph}</p>)}
              </section>
            ) : (
              <p className="ed-empty">No summary was written for this meeting.</p>
            )}

            {decisions.length > 0 && (
              <section id={`${anchor}-decided`} className="ed-decided" aria-labelledby={`${anchor}-decided-h`}>
                <h2 id={`${anchor}-decided-h`} className="ed-h">Decided</h2>
                <ol>
                  {decisions.map((item) => (
                    <li key={item.id}>
                      <div>
                        <span className="ed-statement">{item.text}</span>
                        <ItemFlags item={item} />
                        <ReportTimestamp
                          evidence={item.evidence}
                          segs={segs}
                          limit={2}
                          onEvidenceClick={onEvidenceClick}
                          onSeek={onSeek}
                        />
                      </div>
                    </li>
                  ))}
                </ol>
              </section>
            )}

            {actions.length > 0 && (
              <section id={`${anchor}-actions`} aria-labelledby={`${anchor}-actions-h`}>
                <h2 id={`${anchor}-actions-h`} className="ed-h">Actions</h2>
                <div className="ed-table-wrap">
                  <table className="ed-table">
                    <thead>
                      <tr>
                        <th scope="col">Owner</th>
                        <th scope="col">Task</th>
                        <th scope="col">Due</th>
                        <th scope="col">Source</th>
                      </tr>
                    </thead>
                    <tbody>
                      {actions.map((item) => {
                        const due = itemDeadline(item);
                        return (
                          <tr key={item.id}>
                            <td className="ed-owner">{itemOwnerName(item, state) ?? 'Unassigned'}</td>
                            <td>
                              {item.text}
                              <ItemFlags item={item} />
                            </td>
                            <td className="ed-due">{due ? <><span className="sr-only">Due: </span>{due}</> : '—'}</td>
                            <td>
                              <ReportTimestamp
                                evidence={item.evidence}
                                segs={segs}
                                limit={1}
                                onEvidenceClick={onEvidenceClick}
                                onSeek={onSeek}
                              />
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </section>
            )}

            {answered.length > 0 && (
              <section id={`${anchor}-answered`} aria-labelledby={`${anchor}-answered-h`}>
                <h2 id={`${anchor}-answered-h`} className="ed-h">Answered during the meeting</h2>
                <dl className="ed-qa">
                  {answered.map((question) => (
                    <div key={question.id}>
                      <dt>{question.text}</dt>
                      <dd>
                        {question.answer}
                        <ReportTimestamp
                          evidence={question.evidence}
                          segs={segs}
                          limit={1}
                          onEvidenceClick={onEvidenceClick}
                          onSeek={onSeek}
                        />
                      </dd>
                    </div>
                  ))}
                </dl>
              </section>
            )}

            {open.length > 0 && (
              <section id={`${anchor}-open`} aria-labelledby={`${anchor}-open-h`}>
                <h2 id={`${anchor}-open-h`} className="ed-h">Still open</h2>
                <ul className="ed-open">
                  {open.map((question) => <li key={question.id}>{question.text}</li>)}
                </ul>
              </section>
            )}
          </div>

          {(quote || chapters.length > 0) && (
            <aside className="ed-aside" aria-label="Moment and chapters">
              {quote && (
                <figure className="ed-quote">
                  <span className="ed-quote-kind">{PULSE_LABELS[quote.kind] ?? 'Highlight'}</span>
                  <blockquote>{quote.text}</blockquote>
                  <figcaption>
                    {quoteSpeaker && <span>{quoteSpeaker}</span>}
                    <span className="ed-mono">{clock(quote.start_s)}</span>
                  </figcaption>
                  {onSeek && (
                    <button
                      type="button"
                      className="ed-play no-print"
                      onClick={() => {
                        onSeek(quote.start_s);
                        if (quoteSegment) onEvidenceClick?.(quoteSegment.id);
                      }}
                    >
                      <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true">
                        <path d="M3 1.5l7.5 4.5L3 10.5z" fill="currentColor" />
                      </svg>
                      Play from {clock(quote.start_s)}
                    </button>
                  )}
                </figure>
              )}

              {chapters.length > 0 && (
                <section id={`${anchor}-chapters`} aria-labelledby={`${anchor}-chapters-h`}>
                  <h2 id={`${anchor}-chapters-h`} className="ed-h">Chapters</h2>
                  <ol className="ed-chapters">
                    {chapters.map((chapter, index) => {
                      const end = chapter.end_s ?? duration;
                      const length = Math.max(0, end - chapter.start_s);
                      const share = duration > 0 ? Math.max(2, (length / duration) * 100) : 0;
                      return (
                        <li key={`${chapter.start_s}-${index}`}>
                          <div className="ed-chapter-row">
                            {onSeek ? (
                              <button type="button" className="ed-chapter-link" onClick={() => onSeek(chapter.start_s)}>
                                {chapter.label}
                              </button>
                            ) : (
                              <span className="ed-chapter-link">{chapter.label}</span>
                            )}
                            <span className="ed-mono">{Math.max(1, Math.round(length / 60))} min</span>
                          </div>
                          {share > 0 && (
                            <span className="ed-chapter-bar" aria-hidden="true">
                              <span style={{ width: `${share}%` }} />
                            </span>
                          )}
                        </li>
                      );
                    })}
                  </ol>
                </section>
              )}
            </aside>
          )}
        </div>
      </div>
      </div>
    </article>
  );
}
