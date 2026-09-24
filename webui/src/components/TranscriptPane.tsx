import { useEffect, useMemo, useRef, type CSSProperties, type MouseEvent, type ReactNode, type RefObject } from 'react';
import { usePlayingSpanId } from '../playback';
import { scrollChildIntoView } from '../scroll';
import type { Participant, Segment, SpeechPreviewMsg } from '../types';
import { speakerColor } from '../people';

interface TranscriptPaneProps {
  segments: Segment[];
  previews?: SpeechPreviewMsg[];
  participants: Participant[];
  highlightSegmentId: string | null;
  onHighlightClear: () => void;
  onReassignSpeaker: (segmentId: string, participantId: string | null) => void;
  readOnly?: boolean;
  /** Optional control rendered under the Conversation header (e.g. audio). */
  headerExtra?: ReactNode;
  /** Prefix element ids so a print copy does not collide with the live list. */
  segmentIdPrefix?: string;
  /** Live rail: newest speech at the top. Print / history stay chronological. */
  newestFirst?: boolean;
  /** Play the recording from this turn. Omit to leave the list inert (print). */
  onPlaySegment?: (startSeconds: number, segmentId: string) => void;
  /** The recording element, so the turn being played can mark itself. */
  audioRef?: RefObject<HTMLAudioElement | null>;
  /** Changes whenever that element is replaced, so the listeners re-bind. */
  audioKey?: string;
}

function formatTime(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, '0')}`;
}

function speakerLabel(participants: Participant[], participantId: string | null, channel: string): string {
  if (participantId) {
    const p = participants.find((x) => x.id === participantId);
    if (p) return p.display_name;
  }
  return channel === 'mic' ? 'Me' : 'Others';
}

export default function TranscriptPane({
  segments,
  previews = [],
  participants,
  highlightSegmentId,
  onHighlightClear,
  onReassignSpeaker,
  readOnly = false,
  headerExtra,
  segmentIdPrefix = '',
  newestFirst = false,
  onPlaySegment,
  audioRef,
  audioKey,
}: TranscriptPaneProps) {
  const sorted = useMemo(
    () =>
      [...segments].sort((a, b) =>
        newestFirst ? b.start_s - a.start_s : a.start_s - b.start_s,
      ),
    [segments, newestFirst],
  );
  const highlightedAvailable = Boolean(
    highlightSegmentId && sorted.some((segment) => segment.id === highlightSegmentId),
  );
  const clearHighlightRef = useRef(onHighlightClear);
  clearHighlightRef.current = onHighlightClear;

  useEffect(() => {
    if (!highlightSegmentId || !highlightedAvailable) return undefined;
    const node = document.getElementById(`${segmentIdPrefix}seg-${highlightSegmentId}`);
    if (node) {
      scrollChildIntoView(node, { block: 'center' });
      node.focus({ preventScroll: true });
    }
    const timer = window.setTimeout(() => clearHighlightRef.current(), 3000);
    return () => window.clearTimeout(timer);
  }, [highlightSegmentId, highlightedAvailable, segmentIdPrefix]);

  const playingSegmentId = usePlayingSpanId(sorted, audioRef, audioKey);
  const seekable = Boolean(onPlaySegment);

  /**
   * Anywhere in the turn is a play target, which is why this is a click
   * handler and not a wrapping button: the row already owns a speaker picker,
   * and the text has to stay selectable for people quoting it.
   */
  const playFromRow = (event: MouseEvent<HTMLElement>, segment: Segment) => {
    if (!onPlaySegment) return;
    if ((event.target as HTMLElement).closest('select, button, a, input, textarea')) return;
    if (window.getSelection()?.isCollapsed === false) return;
    onPlaySegment(segment.start_s, segment.id);
  };

  return (
    <section className="panel">
      {/* One block so the title and the player stick to the rail together. */}
      <div className="panel-head">
        <div className="panel-header">
          <span>Conversation</span>
          <span className="meta">{sorted.length} segments</span>
        </div>
        {headerExtra && <div className="no-print">{headerExtra}</div>}
      </div>
      <div className="panel-body">
        {!readOnly && previews.filter(p => p.text.trim()).map(p => (
          <div className="segment no-print" key={p.channel} aria-live="polite">
            <time className="segment-time">{formatTime(p.start_s)}</time>
            <div>
              <div className="segment-meta">{p.channel === 'mic' ? 'Me' : 'Others'} · Live preview</div>
              <p className="segment-text">{p.text}</p>
            </div>
          </div>
        ))}
        {sorted.length === 0 ? (
          <p className="empty-state">
            {readOnly ? 'No transcript was captured.' : 'Waiting for speech…'}
          </p>
        ) : (
          <div className="segment-list">
            {sorted.map((seg) => {
              const highlighted = seg.id === highlightSegmentId;
              const nowPlaying = seg.id === playingSegmentId;
              return (
                <article
                  key={seg.id}
                  id={`${segmentIdPrefix}seg-${seg.id}`}
                  className={`segment${highlighted ? ' highlight' : ''}${nowPlaying ? ' is-playing' : ''}${seekable ? ' is-seekable' : ''}`}
                  tabIndex={highlighted ? -1 : undefined}
                  aria-current={nowPlaying ? 'true' : undefined}
                  onClick={seekable ? (event) => playFromRow(event, seg) : undefined}
                  aria-label={`${speakerLabel(participants, seg.speaker_participant_id, seg.channel)} at ${formatTime(seg.start_s)}`}
                >
                  {seekable ? (
                    <button
                      type="button"
                      className="segment-time segment-seek"
                      aria-label={`Play the recording from ${formatTime(seg.start_s)}`}
                      onClick={() => onPlaySegment?.(seg.start_s, seg.id)}
                    >
                      {formatTime(seg.start_s)}
                    </button>
                  ) : (
                    <time className="segment-time">{formatTime(seg.start_s)}</time>
                  )}
                  <div>
                    <div className="segment-meta">
                      <span
                        className="segment-speaker"
                        style={{ '--speaker': speakerColor(seg.speaker_participant_id, participants) } as CSSProperties}
                      >
                        {readOnly ? (
                          <span>{speakerLabel(participants, seg.speaker_participant_id, seg.channel)}</span>
                        ) : (
                          <select
                            value={seg.speaker_participant_id ?? ''}
                            onChange={(e) => {
                              const val = e.target.value;
                              onReassignSpeaker(seg.id, val || null);
                            }}
                            aria-label={`Speaker for transcript at ${formatTime(seg.start_s)}`}
                          >
                            <option value="">
                              {speakerLabel(participants, null, seg.channel)}
                            </option>
                            {participants.map((p) => (
                              <option key={p.id} value={p.id}>
                                {p.display_name}
                              </option>
                            ))}
                          </select>
                        )}
                      </span>
                      {seg.speaker_pinned && (
                        <span title="Speaker pinned" className="segment-channel">
                          pinned
                        </span>
                      )}
                      <span className="segment-channel">{seg.channel}</span>
                    </div>
                    <p className="segment-text">{seg.text}</p>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
