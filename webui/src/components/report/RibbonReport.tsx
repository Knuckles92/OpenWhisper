import type { RefObject } from 'react';
import {
  clock,
  formatMeetingWhen,
  itemTime,
  liveItems,
  meetingDuration,
  ownerId,
  severity,
  speakerName,
} from '../../report';
import type { CardItem, MeetingInfo, MeetingStateDoc, Segment } from '../../types';
import ReportTimestamp from './ReportTimestamp';
import TimelineMinimap, { type MarkerCard, type MinimapMarker } from './TimelineMinimap';

interface RibbonReportProps {
  state: MeetingStateDoc;
  segments: Segment[];
  segs: Map<string, Segment>;
  meeting?: MeetingInfo | null;
  onEvidenceClick?: (segmentId: string) => void;
  onSeek?: (seconds: number) => void;
  audioRef?: RefObject<HTMLAudioElement | null>;
  audioKey?: string;
}

type CutKind = 'settled' | 'watch' | 'owed';

interface Attached {
  item: CardItem;
  kind: CutKind;
  t: number | null;
}

export default function RibbonReport({
  state,
  segments,
  segs,
  meeting,
  onEvidenceClick,
  onSeek,
  audioRef,
  audioKey,
}: RibbonReportProps) {
  const duration = Math.max(meetingDuration(segments), 1);
  const people = Object.values(state.participants);
  const decisions = liveItems(state.cards.decisions);
  const risks = liveItems(state.cards.risks);
  const actions = liveItems(state.cards.action_items);
  const notes = liveItems(state.cards.live_notes);
  const beats = [...liveItems(state.cards.timeline)].sort(
    (left, right) => (itemTime(left, segs) ?? 1e9) - (itemTime(right, segs) ?? 1e9),
  );
  const extras: Attached[] = [
    ...decisions.map((item) => ({ item, kind: 'settled' as const, t: itemTime(item, segs) })),
    ...risks.map((item) => ({ item, kind: 'watch' as const, t: itemTime(item, segs) })),
    ...actions.map((item) => ({ item, kind: 'owed' as const, t: itemTime(item, segs) })),
  ];

  // Items without a timestamp (or before the first beat) still belong in
  // the exported report. Assign once, then render unmatched items separately.
  const attached = new Map<string, Attached[]>();
  const unplaced: Attached[] = [];
  for (const entry of extras) {
    const beat = entry.t == null ? undefined : [...beats].reverse().find(candidate => {
      const time = itemTime(candidate, segs);
      return time != null && time <= entry.t!;
    });
    if (!beat) unplaced.push(entry);
    else attached.set(beat.id, [...(attached.get(beat.id) ?? []), entry]);
  }

  const noteFor = (time: number): CardItem | null => {
    const candidates = notes.filter((note) => {
      const stamp = itemTime(note, segs);
      return stamp != null && stamp <= time;
    });
    const last = candidates[candidates.length - 1];
    if (!last) return null;
    const stamp = itemTime(last, segs);
    return stamp != null && Math.abs(stamp - time) < 60 ? last : null;
  };

  const markers: MinimapMarker[] = ([
    ['decisions', decisions], ['risks', risks], ['action_items', actions],
  ] as [MarkerCard, CardItem[]][]).flatMap(([card, items]) => items.flatMap((item) => {
    const time = itemTime(item, segs);
    return time == null ? [] : [{ id: item.id, card, time, text: item.text }];
  }));
  const startedAt = typeof meeting?.started_at === 'string' ? meeting.started_at : null;
  const openCount = (state.questions || []).filter((question) => question.status === 'open').length;

  const renderCut = ({ item, kind }: Attached) => {
    const label = kind === 'settled' ? 'Settled' : kind === 'watch' ? 'Risk' : 'Owed';
    const sev = severity(item);
    const owner = ownerId(item);
    return (
      <div className={`rb-cut ${kind}`} key={item.id}>
        <span className="rb-cut-kind">
          {label}{sev ? ` · ${sev}` : ''}
        </span>
        <p>
          {item.text}{' '}
          <ReportTimestamp
            evidence={item.evidence}
            segs={segs}
            limit={3}
            onEvidenceClick={onEvidenceClick}
            onSeek={onSeek}
          />
        </p>
        {owner && (
          <div className="rb-owner">
            <b>{speakerName(state.participants, owner)}</b> picked this up
          </div>
        )}
      </div>
    );
  };

  return (
    <div className="ribbon">
      <div className="rb-head">
        <div className="eyebrow">
          {formatMeetingWhen(startedAt, duration) || 'Meeting'}
          {people.length ? ` · ${people.length} people` : ''}
        </div>
        <h1 className="rb-title">{state.title || meeting?.display_title || meeting?.title || 'Meeting'}</h1>
        <div className="rb-meta">
          {people.length > 0 && (
            <span>
              <b>{people.map((person) => person.display_name).join(', ')}</b>
            </span>
          )}
          <span>
            {decisions.length} settled · {actions.length} owed · {openCount} still open
          </span>
        </div>

        <TimelineMinimap
          segments={segments}
          markers={markers}
          participants={state.participants}
          duration={duration}
          audioRef={audioRef}
          audioKey={audioKey}
          onSeek={onSeek}
        />
      </div>

      <div className="rb-flow">
        {beats.length === 0 && (
          <p className="rb-note">No timeline beats were recorded for this meeting.</p>
        )}
        {beats.map((beat) => {
          const time = itemTime(beat, segs) ?? 0;
          const mine = (attached.get(beat.id) ?? [])
            .sort((left, right) => left.t! - right.t!);
          const note = noteFor(time);
          return (
            <div className="rb-row" key={beat.id}>
              <div className="rb-time">{clock(time)}</div>
              <div className="rb-rail"><span className="rb-dot" /></div>
              <div className="rb-body">
                <h3 className="rb-beat">{beat.text}</h3>
                {note && <p className="rb-note">{note.text}</p>}
                {mine.map(renderCut)}
              </div>
            </div>
          );
        })}
        {unplaced.length > 0 && (
          <section className="rb-body">
            <h3 className="rb-beat">Additional insights</h3>
            {unplaced.map(renderCut)}
          </section>
        )}
      </div>
    </div>
  );
}
