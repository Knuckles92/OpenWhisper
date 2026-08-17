import type { MeetingStateDoc, Segment } from '../../types';
import CardsPane from '../CardsPane';
import NotesPane from '../NotesPane';
import ParticipantsPane from '../ParticipantsPane';
import QuestionInbox from '../QuestionInbox';
import TranscriptPane from '../TranscriptPane';

interface FullMeetingDocumentProps {
  state: MeetingStateDoc;
  segments: Segment[];
}

function topicStamp(ts: string): string {
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
}

/**
 * Print-only complete meeting record. Hidden on screen; shown when
 * `body[data-print-scope="full"]` so History and the ended dashboard
 * download the same document, not whatever happens to be mounted.
 */
export default function FullMeetingDocument({
  state,
  segments,
}: FullMeetingDocumentProps) {
  const participants = Object.values(state.participants);
  const currentTopic = (state.topic.current || '').trim();
  const previousTopics = (state.topic.history || []).slice(0, -1).filter((entry) =>
    (entry.text || '').trim(),
  );
  const noop = () => undefined;

  return (
    <div className="full-meeting-document" aria-hidden="true">
      {(currentTopic || previousTopics.length > 0) && (
        <section className="panel">
          <div className="panel-header">
            <span>Topic</span>
          </div>
          <div className="panel-body">
            {currentTopic && <p className="print-topic-current">{currentTopic}</p>}
            {previousTopics.length > 0 && (
              <>
                <h4 className="card-section-title">Previously</h4>
                <ul className="print-topic-history">
                  {previousTopics.map((entry, index) => {
                    const stamp = topicStamp(entry.ts);
                    return (
                      <li key={`${entry.ts}-${index}`}>
                        {entry.text.trim()}
                        {stamp ? ` (${stamp})` : ''}
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
          </div>
        </section>
      )}

      <NotesPane
        notes={state.cards.live_notes ?? []}
        status={state.status}
        cloudEnabled={state.cloud_enabled}
        intelligenceOnline={state.intelligence_online}
        onEvidenceClick={noop}
        lastSeqByTarget={{}}
        readOnly
      />

      <section className="panel capture">
        <h3 className="capture-heading">Captured</h3>
        <div className="capture-body">
          <CardsPane
            cards={state.cards}
            onEvidenceClick={noop}
            lastSeqByTarget={{}}
            embedded
            readOnly
          />
          <QuestionInbox
            questions={state.questions}
            onEvidenceClick={noop}
            embedded
            readOnly
          />
        </div>
      </section>

      <ParticipantsPane participants={participants} onlineIds={new Set()} readOnly />

      <TranscriptPane
        segments={segments}
        participants={participants}
        highlightSegmentId={null}
        onHighlightClear={noop}
        onReassignSpeaker={noop}
        readOnly
        segmentIdPrefix="print-"
      />
    </div>
  );
}
