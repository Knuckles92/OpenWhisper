import {
  formatMeetingWhen,
  liveItems,
  meetingDuration,
  ownerId,
  severity,
  speakerName,
  splitSummary,
} from '../../report';
import type { CardItem, MeetingInfo, MeetingStateDoc, Segment } from '../../types';
import ReportTimestamp from './ReportTimestamp';

interface BriefReportProps {
  state: MeetingStateDoc;
  segments: Segment[];
  segs: Map<string, Segment>;
  meeting?: MeetingInfo | null;
  onEvidenceClick?: (segmentId: string) => void;
  onSeek?: (seconds: number) => void;
}

export default function BriefReport({
  state,
  segments,
  segs,
  meeting,
  onEvidenceClick,
  onSeek,
}: BriefReportProps) {
  const people = Object.values(state.participants);
  const duration = meetingDuration(segments);
  const startedAt = typeof meeting?.started_at === 'string' ? meeting.started_at : null;
  const { lede, rest } = splitSummary(state.rolling_summary || '');
  const decisions = liveItems(state.cards.decisions);
  const actions = liveItems(state.cards.action_items);
  const risks = [...liveItems(state.cards.risks)].sort((left, right) => {
    const rank = { high: 0, medium: 1, low: 2 } as Record<string, number>;
    return (rank[severity(left) || ''] ?? 3) - (rank[severity(right) || ''] ?? 3);
  });
  const open = (state.questions || []).filter((question) => question.status === 'open');
  const resolved = (state.questions || []).filter((question) => question.status === 'resolved');
  const userNotes = liveItems(state.cards.user_notes);
  const asrModel = typeof meeting?.asr_model === 'string' ? meeting.asr_model : '';

  const byOwner = new Map<string, CardItem[]>();
  for (const item of actions) {
    const key = ownerId(item) || '_none';
    const group = byOwner.get(key) ?? [];
    group.push(item);
    byOwner.set(key, group);
  }

  return (
    <div className="brief">
      <div className="brief-inner">
        <div className="eyebrow br-kicker">
          Meeting brief{state.topic?.current ? ` · ${state.topic.current}` : ''}
        </div>
        <h1 className="br-title">{state.title || meeting?.display_title || meeting?.title || 'Meeting'}</h1>
        <div className="br-byline">
          <b>{formatMeetingWhen(startedAt, duration) || 'Meeting'}</b>
          {people.length > 0 && ` · ${people.map((person) => person.display_name).join(', ')}`}
        </div>

        {lede && <p className="br-lede">{lede}</p>}
        {rest.map((paragraph) => (
          <p key={paragraph.slice(0, 24)}>{paragraph}</p>
        ))}

        {decisions.length > 0 && (
          <>
            <hr className="br-rule" />
            <h2 className="br-h">What was decided</h2>
            <ol className="br-list">
              {decisions.map((item) => (
                <li key={item.id}>
                  <p className="br-statement">{item.text}</p>
                  <p className="br-sub">
                    Settled at{' '}
                    <ReportTimestamp
                      evidence={item.evidence}
                      segs={segs}
                      limit={3}
                      onEvidenceClick={onEvidenceClick}
                      onSeek={onSeek}
                    />
                    {item.status === 'confirmed' ? ' · confirmed by the host' : ''}
                  </p>
                </li>
              ))}
            </ol>
          </>
        )}

        {actions.length > 0 && (
          <>
            <hr className="br-rule" />
            <h2 className="br-h">Who owes what</h2>
            <ul className="br-owed">
              {[...byOwner.entries()].flatMap(([pid, items]) =>
                items.map((item, index) => (
                  <li key={item.id}>
                    <span className="br-who">
                      {index === 0
                        ? pid === '_none'
                          ? 'Unassigned'
                          : speakerName(state.participants, pid)
                        : ''}
                    </span>
                    <span className="br-what">
                      {item.text}{' '}
                      <ReportTimestamp
                        evidence={item.evidence}
                        segs={segs}
                        limit={2}
                        onEvidenceClick={onEvidenceClick}
                        onSeek={onSeek}
                      />
                    </span>
                  </li>
                )),
              )}
            </ul>
          </>
        )}

        {risks.length > 0 && (
          <>
            <hr className="br-rule" />
            <h2 className="br-h">What to watch</h2>
            <ul className="br-open">
              {risks.map((item) => (
                <li key={item.id}>
                  <p className="br-q">{item.text}</p>
                  <p className="br-a">
                    {severity(item) || 'unrated'} ·{' '}
                    <ReportTimestamp
                      evidence={item.evidence}
                      segs={segs}
                      limit={3}
                      onEvidenceClick={onEvidenceClick}
                      onSeek={onSeek}
                    />
                  </p>
                </li>
              ))}
            </ul>
          </>
        )}

        {open.length > 0 && (
          <>
            <hr className="br-rule" />
            <h2 className="br-h">Still open</h2>
            <ul className="br-open">
              {open.map((question) => (
                <li key={question.id}>
                  <p className="br-q">{question.text}</p>
                  <p className="br-a">
                    {question.suggested_answer
                      ? (
                        <>
                          {question.suggested_answer}{' '}
                          {question.suggested_confidence != null && (
                            <em>({Math.round(question.suggested_confidence * 100)}% confidence)</em>
                          )}
                        </>
                      )
                      : 'Nothing in the transcript answers this.'}{' '}
                    <ReportTimestamp
                      evidence={question.evidence}
                      segs={segs}
                      limit={2}
                      onEvidenceClick={onEvidenceClick}
                      onSeek={onSeek}
                    />
                  </p>
                </li>
              ))}
            </ul>
          </>
        )}

        {resolved.length > 0 && (
          <>
            <hr className="br-rule thin" />
            <h2 className="br-h">Answered in the room</h2>
            <ul className="br-open">
              {resolved.map((question) => (
                <li key={question.id} style={{ borderLeftColor: 'var(--leaf)' }}>
                  <p className="br-q">{question.text}</p>
                  <p className="br-a">
                    {question.answer}{' '}
                    <em>
                      (answered {question.answer_source === 'audio' ? 'in the room' : 'by a participant'})
                    </em>{' '}
                    <ReportTimestamp
                      evidence={question.evidence}
                      segs={segs}
                      limit={2}
                      onEvidenceClick={onEvidenceClick}
                      onSeek={onSeek}
                    />
                  </p>
                </li>
              ))}
            </ul>
          </>
        )}

        {userNotes[0] && (
          <>
            <hr className="br-rule" />
            <h2 className="br-h">Your own note</h2>
            <p className="br-q" style={{ borderLeft: '2px solid var(--line)', paddingLeft: 20 }}>
              {userNotes[0].text}
            </p>
          </>
        )}

        <p className="br-foot">
          {asrModel
            ? `Transcribed locally with ${asrModel} and synthesised in a single consolidation pass. `
            : 'Synthesised in a single consolidation pass. '}
          Every timestamp is a link back into the recording.
          <br />
          {liveItems(state.cards.key_points).length} key points, {decisions.length} decisions,{' '}
          {actions.length} commitments and {risks.length} risks were drawn from{' '}
          {segments.length} transcript segments.
        </p>
      </div>
    </div>
  );
}
