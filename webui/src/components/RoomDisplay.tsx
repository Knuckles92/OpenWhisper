import { useEffect, useRef } from 'react';
import type { Chapter } from '../chapters';
import { speakerColor } from '../people';
import type { CardItem, MeetingStateDoc, Participant, Segment } from '../types';
import './roomDisplay.css';

interface RoomDisplayProps {
  state: MeetingStateDoc;
  segments: Segment[];
  participants: Participant[];
  chapters: Chapter[];
  /** Recorded seconds so far — the room clock. */
  elapsedS: number;
  onExit: () => void;
}

function clockTime(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = String(whole % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

function live(items: CardItem[] | undefined): CardItem[] {
  return (items ?? [])
    .filter((item) => item.status !== 'removed')
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));
}

function speakerName(participants: Participant[], id: string | null, channel: string): string {
  const person = participants.find((p) => p.id === id);
  if (person) return person.display_name;
  return channel === 'mic' ? 'Me' : 'Others';
}

function statusLabel(status: string): string {
  if (status === 'active') return 'Live';
  if (status === 'paused') return 'Paused';
  if (status === 'ending') return 'Ending';
  return 'Ended';
}

/**
 * Presenter view for a conference-room screen: the current topic, the last
 * two things said, and what the room has decided and owes — sized to read
 * from across the table.
 */
export default function RoomDisplay({
  state,
  segments,
  participants,
  chapters,
  elapsedS,
  onExit,
}: RoomDisplayProps) {
  const exitRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const root = document.documentElement;
    // Fullscreen is a nicety: browsers may refuse it without a gesture.
    try {
      if (!document.fullscreenElement && root.requestFullscreen) {
        void root.requestFullscreen().catch(() => undefined);
      }
    } catch {
      // Ignore: the overlay already fills the window.
    }
    exitRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onExit();
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
      try {
        if (document.fullscreenElement && document.exitFullscreen) {
          void document.exitFullscreen().catch(() => undefined);
        }
      } catch {
        // Ignore.
      }
    };
  }, [onExit]);

  const running = state.status === 'active' || state.status === 'paused';
  const topic = state.topic.current || state.title || 'Waiting for the discussion to begin';
  const recent = [...segments]
    .filter((segment) => segment.text.trim())
    .sort((a, b) => a.start_s - b.start_s)
    .slice(-2);
  const decisions = live(state.cards.decisions).slice(0, 3);
  const actions = live(state.cards.action_items).slice(0, 4);
  const openQuestion = (state.questions ?? []).find((q) => q.status === 'open');
  const span = Math.max(1, elapsedS, ...chapters.map((c) => c.end_s ?? c.start_s));
  const current = chapters.length - 1;

  return (
    <div className="room" role="dialog" aria-modal="true" aria-label="Room display">
      <button ref={exitRef} type="button" className="room-exit" onClick={onExit}>
        Exit room display <kbd>Esc</kbd>
      </button>

      <div className="room-main">
        <section className="room-now" aria-live="polite">
          <div className={`room-status ${state.status === 'active' ? 'live' : ''}`}>
            <span className="room-dot" aria-hidden="true" />
            <span>{statusLabel(state.status)}</span>
            <span className="room-clock">{clockTime(elapsedS)}</span>
          </div>
          <p className="room-eyebrow">{running ? 'Now discussing' : 'Last topic'}</p>
          <h1 className="room-topic">{topic}</h1>
          <div className="room-lines">
            {recent.map((segment) => (
              <p key={segment.id} className="room-line">
                <span
                  className="room-speaker"
                  style={{ color: speakerColor(segment.speaker_participant_id, participants) }}
                >
                  {speakerName(participants, segment.speaker_participant_id, segment.channel)}
                </span>
                {segment.text}
              </p>
            ))}
          </div>
        </section>

        <aside className="room-side">
          <section>
            <h2 className="room-heading decision">Decided</h2>
            {decisions.length ? (
              <ul className="room-list">
                {decisions.map((item) => <li key={item.id}>{item.text}</li>)}
              </ul>
            ) : (
              <p className="room-empty">Nothing decided yet.</p>
            )}
          </section>
          <section>
            <h2 className="room-heading action">To do</h2>
            {actions.length ? (
              <ul className="room-list">
                {actions.map((item) => {
                  const owner = participants.find((p) => p.id === item.data.owner_participant_id);
                  return (
                    <li key={item.id}>
                      {owner && <strong>{owner.display_name} </strong>}
                      {item.text}
                    </li>
                  );
                })}
              </ul>
            ) : (
              <p className="room-empty">No actions yet.</p>
            )}
          </section>
          {openQuestion && (
            <section className="room-open">
              <h2 className="room-heading question">Still open</h2>
              <p>{openQuestion.text}</p>
            </section>
          )}
        </aside>
      </div>

      {chapters.length > 0 && (
        <ol className="room-chapters" aria-label="Topics so far">
          {chapters.map((chapter, index) => {
            const end = chapters[index + 1]?.start_s ?? chapter.end_s ?? span;
            const width = Math.max(4, ((end - chapter.start_s) / span) * 100);
            return (
              <li
                key={`${chapter.start_s}-${index}`}
                className={index === current ? 'current' : undefined}
                style={{ flexGrow: width }}
                aria-current={index === current ? 'step' : undefined}
              >
                <span>{chapter.label}</span>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
