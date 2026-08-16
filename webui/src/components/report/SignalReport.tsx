import type { ReactNode } from 'react';
import {
  clock,
  deriveListenPicks,
  deriveSignalHeadline,
  deriveSignalStandfirst,
  formatMeetingWhen,
  liveItems,
  meetingDuration,
  ownerId,
  severity,
  speakerName,
} from '../../report';
import type { CardItem, MeetingInfo, MeetingStateDoc, Question, Segment } from '../../types';
import ReportTimestamp from './ReportTimestamp';

interface SignalReportProps {
  state: MeetingStateDoc;
  segments: Segment[];
  segs: Map<string, Segment>;
  meeting?: MeetingInfo | null;
  onEvidenceClick?: (segmentId: string) => void;
  onSeek?: (seconds: number) => void;
}

interface WatchItem {
  id: string;
  text: string;
  evidence: string[];
  kind: 'risk' | 'question';
  severity?: string | null;
}

function Column({
  title,
  count,
  watch,
  children,
}: {
  title: string;
  count: number;
  watch?: boolean;
  children: ReactNode;
}) {
  return (
    <div className={`sg-col${watch ? ' watch' : ''}`}>
      <div className="sg-col-head">
        <h3>{title}</h3>
        <span className="sg-n">{count}</span>
      </div>
      <ul>{children}</ul>
    </div>
  );
}

export default function SignalReport({
  state,
  segments,
  segs,
  meeting,
  onEvidenceClick,
  onSeek,
}: SignalReportProps) {
  const people = Object.values(state.participants);
  const duration = meetingDuration(segments);
  const startedAt = typeof meeting?.started_at === 'string' ? meeting.started_at : null;
  const decisions = liveItems(state.cards.decisions);
  const actions = liveItems(state.cards.action_items);
  const risks = liveItems(state.cards.risks);
  const open = (state.questions || []).filter((question) => question.status === 'open');
  const watch: WatchItem[] = [
    ...risks.map((item) => ({
      id: item.id,
      text: item.text,
      evidence: item.evidence,
      kind: 'risk' as const,
      severity: severity(item),
    })),
    ...open.map((question: Question) => ({
      id: question.id,
      text: question.text,
      evidence: question.evidence,
      kind: 'question' as const,
    })),
  ];
  const picks = deriveListenPicks(state, segs);

  return (
    <div className="signal">
      <div className="sg-top">
        <div className="eyebrow">If you missed it</div>
        <div className="sg-when">
          {formatMeetingWhen(startedAt, duration)}
          {people.length > 0 && ` · ${people.map((person) => person.display_name).join(', ')}`}
        </div>
      </div>

      <h1 className="sg-headline">{deriveSignalHeadline(state)}</h1>
      {deriveSignalStandfirst(state) && (
        <p className="sg-standfirst">{deriveSignalStandfirst(state)}</p>
      )}

      <div className="sg-cols">
        <Column title="Decided" count={decisions.length}>
          {decisions.map((item) => (
            <li key={item.id}>
              {item.text}
              <span className="sg-meta">
                <ReportTimestamp
                  evidence={item.evidence}
                  segs={segs}
                  limit={1}
                  onEvidenceClick={onEvidenceClick}
                  onSeek={onSeek}
                />
              </span>
            </li>
          ))}
        </Column>
        <Column title="Owed" count={actions.length}>
          {actions.map((item: CardItem) => {
            const owner = ownerId(item);
            return (
              <li key={item.id}>
                {item.text}
                <span className="sg-meta">
                  <b>{owner ? speakerName(state.participants, owner) : 'unassigned'}</b>
                  {' · '}
                  <ReportTimestamp
                    evidence={item.evidence}
                    segs={segs}
                    limit={1}
                    onEvidenceClick={onEvidenceClick}
                    onSeek={onSeek}
                  />
                </span>
              </li>
            );
          })}
        </Column>
        <Column title="Watch" count={watch.length} watch>
          {watch.map((item) => (
            <li key={item.id}>
              {item.text}
              <span className="sg-meta">
                {item.kind === 'question'
                  ? 'open question · '
                  : `${item.severity || 'unrated'} risk · `}
                <ReportTimestamp
                  evidence={item.evidence}
                  segs={segs}
                  limit={1}
                  onEvidenceClick={onEvidenceClick}
                  onSeek={onSeek}
                />
              </span>
            </li>
          ))}
        </Column>
      </div>

      {picks.length > 0 && (
        <div className="sg-listen">
          <h3>Three minutes worth hearing</h3>
          <div className="sg-picks">
            {picks.map((segment) => (
              <button
                key={segment.id}
                type="button"
                className="sg-pick"
                onClick={() => {
                  onSeek?.(segment.start_s);
                  onEvidenceClick?.(segment.id);
                }}
              >
                <span className="sg-pick-t">
                  {clock(segment.start_s)} · {speakerName(state.participants, segment.speaker_participant_id, segment.channel)}
                </span>
                <span className="sg-pick-q">“{segment.text}”</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
