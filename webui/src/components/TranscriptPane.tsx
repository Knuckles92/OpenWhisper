import { useEffect, useMemo } from 'react';
import type { Participant, Segment } from '../types';

interface TranscriptPaneProps {
  segments: Segment[];
  participants: Participant[];
  highlightSegmentId: string | null;
  onHighlightClear: () => void;
  onReassignSpeaker: (segmentId: string, participantId: string | null) => void;
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
}: TranscriptPaneProps) {
  const sorted = useMemo(
    () => [...segments].sort((a, b) => a.start_s - b.start_s),
    [segments],
  );

  useEffect(() => {
    if (!highlightSegmentId) return;
    const t = window.setTimeout(onHighlightClear, 3000);
    return () => clearTimeout(t);
  }, [highlightSegmentId, onHighlightClear]);

  return (
    <section className="panel">
      <div className="panel-header">
        <span>Transcript</span>
        <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{sorted.length} segments</span>
      </div>
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
                  <div className="segment-meta">
                    <span>{formatTime(seg.start_s)}</span>
                    <span className="segment-speaker">
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
                    </span>
                    {seg.speaker_pinned && (
                      <span title="Speaker pinned" style={{ fontSize: 11 }}>
                        📌
                      </span>
                    )}
                    <span style={{ fontSize: 11, opacity: 0.7 }}>{seg.channel}</span>
                  </div>
                  <p className="segment-text">{seg.text}</p>
                </article>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
