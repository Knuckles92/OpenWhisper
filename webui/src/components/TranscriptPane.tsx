import { useEffect, useMemo, type ReactNode } from 'react';
import type { Participant, Segment } from '../types';

interface TranscriptPaneProps {
  segments: Segment[];
  participants: Participant[];
  highlightSegmentId: string | null;
  onHighlightClear: () => void;
  onReassignSpeaker: (segmentId: string, participantId: string | null) => void;
  readOnly?: boolean;
  /** Optional control rendered under the Conversation header (e.g. audio). */
  headerExtra?: ReactNode;
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
  participants,
  highlightSegmentId,
  onHighlightClear,
  onReassignSpeaker,
  readOnly = false,
  headerExtra,
}: TranscriptPaneProps) {
  const sorted = useMemo(
    () => [...segments].sort((a, b) => a.start_s - b.start_s),
    [segments],
  );
  const highlightedAvailable = Boolean(
    highlightSegmentId && sorted.some((segment) => segment.id === highlightSegmentId),
  );

  useEffect(() => {
    if (!highlightSegmentId || !highlightedAvailable) return;
    requestAnimationFrame(() => {
      document.getElementById(`seg-${highlightSegmentId}`)?.scrollIntoView({
        behavior: 'smooth',
        block: 'center',
      });
    });
    const t = window.setTimeout(onHighlightClear, 3000);
    return () => clearTimeout(t);
  }, [highlightSegmentId, highlightedAvailable, onHighlightClear]);

  return (
    <section className="panel">
      <div className="panel-header">
        <span>Conversation</span>
        <span className="meta">{sorted.length} segments</span>
      </div>
      {headerExtra}
      <div className="panel-body">
        {sorted.length === 0 ? (
          <p className="empty-state">Waiting for speech…</p>
        ) : (
          <div className="segment-list">
            {sorted.map((seg) => {
              const highlighted = seg.id === highlightSegmentId;
              return (
                <article
                  key={seg.id}
                  id={`seg-${seg.id}`}
                  className={`segment${highlighted ? ' highlight' : ''}`}
                >
                  <time className="segment-time">{formatTime(seg.start_s)}</time>
                  <div>
                    <div className="segment-meta">
                      <span className="segment-speaker">
                        {readOnly ? (
                          <span>{speakerLabel(participants, seg.speaker_participant_id, seg.channel)}</span>
                        ) : (
                          <select
                            value={seg.speaker_participant_id ?? ''}
                            onChange={(e) => {
                              const val = e.target.value;
                              onReassignSpeaker(seg.id, val || null);
                            }}
                            aria-label="Speaker"
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
