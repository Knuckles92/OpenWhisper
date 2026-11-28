import { memo, useCallback, useMemo, type KeyboardEvent, type MouseEvent, type RefObject } from 'react';
import { speakerColorMap, UNKNOWN_SPEAKER } from '../../people';
import { clock, speakerName } from '../../report';
import { useAudioClock } from '../../playback';
import type { Participant, Segment } from '../../types';

export type MarkerCard = 'decisions' | 'risks' | 'action_items';

export interface MinimapMarker {
  id: string;
  card: MarkerCard;
  time: number;
  text: string;
}

interface TimelineMinimapProps {
  segments: Segment[];
  markers: MinimapMarker[];
  participants: Record<string, Participant>;
  /** Meeting-clock length the track is drawn against; always > 0. */
  duration: number;
  /** The dashboard's recording element, when one is mounted. */
  audioRef?: RefObject<HTMLAudioElement | null>;
  /** Changes whenever that element is replaced, so the clock re-binds. */
  audioKey?: string;
  onSeek?: (seconds: number) => void;
}

const MARKER_LABELS: Record<MarkerCard, string> = {
  decisions: 'Decision',
  risks: 'Risk',
  action_items: 'Commitment',
};

const TICKS = [0, 0.25, 0.5, 0.75, 1];
const ARROW_STEP = 5;
const PAGE_STEP = 30;

/** A turn shorter than this still needs a hit target the playhead can land in. */
const MIN_SPAN_S = 0.4;

function clip(text: string, max = 110): string {
  const trimmed = text.trim();
  return trimmed.length > max ? `${trimmed.slice(0, max - 1)}…` : trimmed;
}

const SegmentBars = memo(function SegmentBars({
  segments,
  participants,
  colorMap,
  duration,
  playingId,
}: {
  segments: Segment[];
  participants: Record<string, Participant>;
  colorMap: Map<string, string>;
  duration: number;
  playingId: string | null;
}) {
  return (
    <>
      {segments.map((segment) => {
        const span = Math.max(0, segment.end_s - segment.start_s);
        const who = speakerName(participants, segment.speaker_participant_id, segment.channel);
        return (
          <i
            key={segment.id}
            className={`mm-seg${segment.id === playingId ? ' is-playing' : ''}`}
            data-start={segment.start_s}
            title={`${clock(segment.start_s)} · ${who}: ${clip(segment.text)}`}
            style={{
              left: `${(segment.start_s / duration) * 100}%`,
              width: `${Math.max(0.35, (span / duration) * 100)}%`,
              height: `${18 + Math.min(40, span * 1.9)}px`,
              background: (segment.speaker_participant_id && colorMap.get(segment.speaker_participant_id)) || UNKNOWN_SPEAKER,
            }}
          />
        );
      })}
    </>
  );
});

const PlayheadOverlay = memo(function PlayheadOverlay({
  at,
  duration,
  playing,
  started,
}: {
  at: number;
  duration: number;
  playing: boolean;
  started: boolean;
}) {
  if (!started) return null;
  const pct = `${(at / duration) * 100}%`;
  return (
    <>
      <span className="mm-played" style={{ width: pct }} aria-hidden="true" />
      <span
        className={`mm-playhead${playing ? ' is-playing' : ''}`}
        style={{ left: pct }}
        aria-hidden="true"
      />
    </>
  );
});

const MarkersLayer = memo(function MarkersLayer({
  markers,
  duration,
  interactive,
  onSeek,
}: {
  markers: MinimapMarker[];
  duration: number;
  interactive: boolean;
  onSeek: (seconds: number) => void;
}) {
  return (
    <div className="mm-markers">
      {markers.map((marker) => (
        <button
          key={`${marker.card}-${marker.id}`}
          type="button"
          className={`mm-marker ${marker.card}`}
          style={{ left: `${(marker.time / duration) * 100}%` }}
          title={marker.text}
          aria-label={`${MARKER_LABELS[marker.card]} at ${clock(marker.time)}: ${marker.text}`}
          disabled={!interactive}
          onClick={(event) => {
            event.stopPropagation();
            onSeek(marker.time);
          }}
        />
      ))}
    </div>
  );
});

/**
 * The Ribbon minimap: proportional bars for every turn, markers for what was
 * captured, and a playhead tracking the recording. Clicking a bar plays that
 * turn; clicking the gaps plays from that point on the meeting clock.
 */
export default function TimelineMinimap({
  segments,
  markers,
  participants,
  duration,
  audioRef,
  audioKey,
  onSeek,
}: TimelineMinimapProps) {
  const { time, playing } = useAudioClock(audioRef, audioKey);
  const at = Math.min(Math.max(time, 0), duration);
  const interactive = Boolean(onSeek);
  const colorMap = useMemo(() => speakerColorMap(Object.values(participants)), [participants]);
  // Before the first play there is no position worth drawing a line through.
  const started = playing || at > 0;

  const seekTo = useCallback(
    (seconds: number) => onSeek?.(Math.min(duration, Math.max(0, seconds))),
    [duration, onSeek],
  );

  const seekFromPointer = (event: MouseEvent<HTMLDivElement>) => {
    if (!onSeek) return;
    const bar = (event.target as HTMLElement).closest<HTMLElement>('.mm-seg');
    if (bar?.dataset.start) {
      seekTo(Number(bar.dataset.start));
      return;
    }
    const box = event.currentTarget.getBoundingClientRect();
    if (!box.width) return;
    seekTo(((event.clientX - box.left) / box.width) * duration);
  };

  const scrub = (event: KeyboardEvent<HTMLDivElement>) => {
    if (!onSeek) return;
    const step = { ArrowLeft: -ARROW_STEP, ArrowRight: ARROW_STEP, ArrowDown: -ARROW_STEP, ArrowUp: ARROW_STEP,
      PageDown: -PAGE_STEP, PageUp: PAGE_STEP }[event.key];
    const absolute = event.key === 'Home' ? 0 : event.key === 'End' ? duration : null;
    if (step == null && absolute == null) return;
    event.preventDefault();
    seekTo(absolute ?? at + (step as number));
  };

  const playingNow = segments.find(
    (segment) => at >= segment.start_s && at < Math.max(segment.end_s, segment.start_s + MIN_SPAN_S),
  );
  const playingId = started ? (playingNow?.id ?? null) : null;

  return (
    <div className="minimap">
      <div className="mm-frame">
        <div
          className={`mm-track${interactive ? ' is-seekable' : ''}`}
          role={interactive ? 'slider' : undefined}
          tabIndex={interactive ? 0 : undefined}
          aria-label={interactive ? 'Meeting timeline. Click a turn or use the arrow keys to play from that point.' : undefined}
          aria-valuemin={interactive ? 0 : undefined}
          aria-valuemax={interactive ? Math.round(duration) : undefined}
          aria-valuenow={interactive ? Math.round(at) : undefined}
          aria-valuetext={interactive ? `${clock(at)} of ${clock(duration)}` : undefined}
          onClick={interactive ? seekFromPointer : undefined}
          onKeyDown={interactive ? scrub : undefined}
        >
          <SegmentBars
            segments={segments}
            participants={participants}
            colorMap={colorMap}
            duration={duration}
            playingId={playingId}
          />
          <PlayheadOverlay at={at} duration={duration} playing={playing} started={started} />
        </div>
        {/* Markers sit outside the slider so they stay their own tab stops. */}
        <MarkersLayer markers={markers} duration={duration} interactive={interactive} onSeek={seekTo} />
      </div>
      <div className="mm-axis">
        {TICKS.map((fraction) => (
          <span key={fraction}>{clock(duration * fraction)}</span>
        ))}
      </div>
      <div className="mm-legend">
        <span><i style={{ background: 'var(--leaf)' }} /> decision</span>
        <span><i style={{ background: 'var(--clay)' }} /> risk</span>
        <span><i style={{ background: 'var(--ink)' }} /> commitment</span>
        <span style={{ color: 'var(--faint)' }}>
          {interactive ? 'bar height = length of turn · click a turn to play it' : 'bar height = length of turn, colour = speaker'}
        </span>
      </div>
      {started && playingNow && (
        <p className="mm-now">
          <span className="mm-now-time">{clock(at)}</span>
          <b>{speakerName(participants, playingNow.speaker_participant_id, playingNow.channel)}</b>
          <span className="mm-now-text">{clip(playingNow.text, 140)}</span>
        </p>
      )}
    </div>
  );
}
