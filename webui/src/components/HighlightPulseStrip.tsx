import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import type { Chapter } from '../chapters';
import { speakerColor } from '../people';
import type { HighlightPulse, LiveHighlightsStatus, Participant, Segment } from '../types';
import './momentRuler.css';

export const PULSE_LABELS: Record<HighlightPulse['kind'], string> = {
  decision: 'Decision', disagreement: 'Disagreement', commitment: 'Dated commitment', number: 'Number',
  takeaway: 'Takeaways',
};

export function pulseTime(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}

function isProbability(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1;
}

function percent(value: number): string {
  return `${Number((value * 100).toFixed(1))}%`;
}

/** Shape-coded so kinds stay distinguishable without relying on colour. */
export function PulseGlyph({ kind, size = 12 }: { kind: HighlightPulse['kind']; size?: number }) {
  return <svg className="mr-glyph" width={size} height={size} viewBox="0 0 12 12" aria-hidden="true">
    {kind === 'decision' && <path d="M6 .5 11.5 6 6 11.5.5 6Z" fill="currentColor" />}
    {kind === 'disagreement' && <path d="m2 2 8 8m0-8-8 8" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />}
    {kind === 'commitment' && <rect x="1.5" y="1.5" width="9" height="9" rx="2.5" fill="none" stroke="currentColor" strokeWidth="2" />}
    {kind === 'number' && <rect x="1" y="1" width="10" height="10" rx="1.5" fill="currentColor" />}
    {kind === 'takeaway' && <circle cx="6" cy="6" r="5" fill="currentColor" />}
  </svg>;
}

function PulseEvidence({ pulse }: { pulse: HighlightPulse }) {
  const assessment = pulse.assessment;
  const probability = isProbability(pulse.probability) ? pulse.probability : undefined;
  const threshold = isProbability(assessment?.threshold) ? assessment.threshold : undefined;
  const margin = probability !== undefined && threshold !== undefined
    ? Number(((probability - threshold) * 100).toFixed(1)) : undefined;

  return <>
    <blockquote className="pulse-preview-quote">{pulse.text}</blockquote>
    <div className="pulse-preview-score">
      <div><span>Detection probability</span><strong>{probability === undefined ? 'Unavailable' : percent(probability)}</strong></div>
      {threshold !== undefined && <div className="pulse-preview-cutoff"><span>Cutoff {percent(threshold)}</span>
        {margin !== undefined && <strong>{margin > 0 ? `+${margin} ${margin === 1 ? 'pt' : 'pts'} above` : margin === 0 ? 'At cutoff' : `${Math.abs(margin)} ${Math.abs(margin) === 1 ? 'pt' : 'pts'} below`}</strong>}
      </div>}
    </div>
    {!assessment && <p className="pulse-preview-unavailable">Additional scoring details were not saved for this pulse.</p>}
  </>;
}

function PulsePreview({ pulse, anchor, id, playbackAvailable, context, onPlay, onDismiss, onEnter, onLeave }: {
  pulse: HighlightPulse;
  anchor: HTMLButtonElement;
  id: string;
  playbackAvailable: boolean;
  context: string;
  onPlay: () => void;
  onDismiss: () => void;
  onEnter: () => void;
  onLeave: () => void;
}) {
  const cardRef = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: 0, top: 0, arrow: 0, above: false, maxHeight: 0 });

  useLayoutEffect(() => {
    const place = () => {
      const card = cardRef.current;
      if (!card || !anchor.isConnected) return;
      const target = anchor.getBoundingClientRect();
      const { width } = card.getBoundingClientRect();
      const body = card.querySelector<HTMLElement>('.pulse-preview-body');
      const height = card.scrollHeight + 2 + (body ? body.scrollHeight - body.clientHeight : 0);
      const margin = 12, gap = 10;
      const center = target.left + target.width / 2;
      const left = Math.max(margin, Math.min(center - width / 2, window.innerWidth - width - margin));
      const roomAbove = target.top - gap - margin;
      const roomBelow = window.innerHeight - target.bottom - gap - margin;
      const above = height <= roomAbove || (height > roomBelow && roomAbove > roomBelow);
      // Scroll the excerpt when neither side has enough room; never cover the marker.
      const maxHeight = Math.max(0, above ? roomAbove : roomBelow);
      const top = above ? target.top - gap - Math.min(height, maxHeight) : target.bottom + gap;
      setPosition({ left, top, above, maxHeight, arrow: Math.max(20, Math.min(center - left, width - 20)) });
    };
    place();
    window.addEventListener('resize', place);
    return () => window.removeEventListener('resize', place);
  }, [anchor, pulse]);

  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (cardRef.current?.contains(document.activeElement)) anchor.focus();
      onDismiss();
    };
    // The preview is portaled, so move from its trigger into its controls explicitly.
    const enter = (event: KeyboardEvent) => {
      if (document.activeElement !== anchor || event.shiftKey || !['Tab', 'ArrowDown'].includes(event.key)) return;
      event.preventDefault();
      cardRef.current?.querySelector<HTMLElement>('button')?.focus();
    };
    // Scrolling the excerpt is fine; scrolling the meeting dismisses a detached preview.
    const scroll = (event: Event) => {
      if (!cardRef.current?.contains(event.target as Node)) onDismiss();
    };
    const outside = (event: PointerEvent) => {
      if (!cardRef.current?.contains(event.target as Node) && !anchor.contains(event.target as Node)) onDismiss();
    };
    anchor.addEventListener('keydown', enter);
    document.addEventListener('keydown', escape);
    document.addEventListener('scroll', scroll, true);
    document.addEventListener('pointerdown', outside);
    return () => {
      anchor.removeEventListener('keydown', enter);
      document.removeEventListener('keydown', escape);
      document.removeEventListener('scroll', scroll, true);
      document.removeEventListener('pointerdown', outside);
    };
  }, [anchor, onDismiss]);

  return createPortal(<div ref={cardRef} id={id} role="dialog" aria-modal="false" aria-labelledby={`${id}-heading`}
    className={`pulse-preview mr-pop pulse-${pulse.kind} no-print`} data-side={position.above ? 'above' : 'below'}
    style={{ left: position.left, top: position.top, maxHeight: position.maxHeight || undefined, '--pulse-arrow-left': `${position.arrow}px` } as CSSProperties}
    onPointerEnter={onEnter} onPointerLeave={onLeave} onFocus={onEnter} onBlur={onLeave}>
    <div className="mr-pop-heading">
      <PulseGlyph kind={pulse.kind} size={11} />
      <strong id={`${id}-heading`} className="mr-pop-kind">{PULSE_LABELS[pulse.kind]}</strong>
      <span className="mr-pop-time">{pulseTime(pulse.start_s)}</span>
      <button type="button" className="pulse-preview-close" onClick={() => { anchor.focus(); onDismiss(); }}>Close</button>
    </div>
    <div className="pulse-preview-body">
      <PulseEvidence key={pulse.id} pulse={pulse} />
    </div>
    {context && <p className="mr-pop-context">{context}</p>}
    <div className="mr-pop-footer">
      {playbackAvailable ? <>
        <button type="button" className="primary mr-pop-play" onClick={onPlay}>
          <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 1.5 10.5 6 3 10.5Z" fill="currentColor" /></svg>
          Play from here
        </button>
        <span className="mr-pop-hint">Click pulse to replay this moment</span>
      </> : <span className="mr-pop-hint">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><circle cx="12" cy="12" r="9" /><path d="M12 7v6m0 3v1" /></svg>
        No recording available for this moment
      </span>}
    </div>
  </div>, document.body);
}

interface PlacedPulse { pulse: HighlightPulse; left: number; row: number }

/** Lay markers out left to right, dropping a colliding one onto the next row. */
function placePulses(pulses: HighlightPulse[], duration: number, widthPx: number): PlacedPulse[] {
  const gapPct = (22 / Math.max(widthPx, 1)) * 100;
  const rowEnds: number[] = [];
  return [...pulses].sort((a, b) => a.start_s - b.start_s || a.id.localeCompare(b.id)).map(pulse => {
    const left = Math.min(100, Math.max(0, (pulse.start_s / duration) * 100));
    let row = rowEnds.findIndex(end => left - end >= gapPct);
    if (row < 0) row = rowEnds.length < 3 ? rowEnds.length : rowEnds.indexOf(Math.min(...rowEnds));
    rowEnds[row] = left;
    return { pulse, left, row };
  });
}

interface ActivityBucket { color: string; density: number }

/** Roughly one bucket per minute, coloured by whoever talked most in it. */
function activityBuckets(segments: Segment[], participants: Participant[], duration: number): ActivityBucket[] {
  const spoken = segments.filter(s => Number.isFinite(s.start_s) && s.text?.trim());
  if (!spoken.length) return [];
  const count = Math.min(90, Math.max(1, Math.ceil(duration / 60)));
  const size = duration / count;
  const talk: Array<Map<string, number>> = Array.from({ length: count }, () => new Map());
  for (const segment of spoken) {
    const end = Number.isFinite(segment.end_s) && segment.end_s > segment.start_s ? segment.end_s : segment.start_s + 2;
    const index = Math.min(count - 1, Math.max(0, Math.floor(((segment.start_s + end) / 2) / size)));
    const who = segment.speaker_participant_id ?? '';
    talk[index].set(who, (talk[index].get(who) ?? 0) + (end - segment.start_s));
  }
  return talk.map(bucket => {
    let top = '', most = 0, total = 0;
    for (const [who, seconds] of bucket) {
      total += seconds;
      if (seconds > most) { most = seconds; top = who; }
    }
    return { color: speakerColor(top || null, participants), density: total ? Math.min(1, total / size) : 0 };
  });
}

export default function HighlightPulseStrip({ pulses, onSelect, playbackAvailable = true,
  highlightStatus = 'unknown', meetingStatus = 'ended', cloudEnabled = true, isHost = false,
  chapters = [], segments = [], participants = [], durationS, nowEdge = false }: {
  pulses: HighlightPulse[];
  playbackAvailable?: boolean;
  highlightStatus?: LiveHighlightsStatus;
  meetingStatus?: string;
  cloudEnabled?: boolean;
  isHost?: boolean;
  onSelect: (pulse: HighlightPulse) => void;
  /** Topic chapters drawn as labelled bands along the ruler. */
  chapters?: Chapter[];
  /** Transcript used for the speaker-activity strip and popover attribution. */
  segments?: Segment[];
  participants?: Participant[];
  /** Meeting length (or elapsed time while live) the ruler spans. */
  durationS?: number;
  /** Mark the end of the ruler as "now" while the meeting is running. */
  nowEdge?: boolean;
}) {
  const [preview, setPreview] = useState<{ id: string; anchor: HTMLButtonElement } | null>(null);
  const previewId = useId();
  const previewHovered = useRef(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout>>();
  const trackRef = useRef<HTMLDivElement>(null);
  const [trackWidth, setTrackWidth] = useState(800);
  const cancelClose = useCallback(() => { clearTimeout(closeTimer.current); }, []);
  const dismiss = useCallback(() => { cancelClose(); previewHovered.current = false; setPreview(null); }, [cancelClose]);
  const activePulse = pulses.find(p => p.id === preview?.id);
  useEffect(() => cancelClose, [cancelClose]);

  useLayoutEffect(() => {
    const el = trackRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return undefined;
    const sync = () => { if (el.clientWidth) setTrackWidth(el.clientWidth); };
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const showPreview = (pulse: HighlightPulse, anchor: HTMLButtonElement) => {
    cancelClose();
    previewHovered.current = false;
    setPreview({ id: pulse.id, anchor });
  };
  const scheduleClose = () => {
    cancelClose();
    closeTimer.current = setTimeout(() => {
      const focusedInPreview = document.getElementById(previewId)?.contains(document.activeElement);
      if (!previewHovered.current && !focusedInPreview && document.activeElement !== preview?.anchor) setPreview(null);
    }, 160);
  };

  const lastSpoken = segments.reduce((end, s) => Math.max(end, Number.isFinite(s.end_s) ? s.end_s : 0), 0);
  const duration = durationS !== undefined && Number.isFinite(durationS) && durationS > 0
    ? Math.max(durationS, ...pulses.map(p => p.start_s + 5))
    : Math.max(60, lastSpoken, ...pulses.map(p => p.start_s + 30));
  const placed = useMemo(() => placePulses(pulses, duration, trackWidth), [pulses, duration, trackWidth]);
  const rows = placed.reduce((most, p) => Math.max(most, p.row + 1), 1);
  const buckets = useMemo(() => activityBuckets(segments, participants, duration), [segments, participants, duration]);
  const bands = chapters
    .map((chapter, i) => {
      const start = Math.min(duration, Math.max(0, chapter.start_s));
      const end = Math.min(duration, Math.max(start, chapter.end_s ?? chapters[i + 1]?.start_s ?? duration));
      return { ...chapter, start, end, last: i === chapters.length - 1 };
    })
    .filter(band => band.end > band.start || band.last);
  const counts = (Object.keys(PULSE_LABELS) as HighlightPulse['kind'][])
    .map(kind => ({ kind, count: pulses.filter(p => p.kind === kind).length }))
    .filter(entry => entry.count > 0);
  const ticks = [0, 0.25, 0.5, 0.75, 1];
  const showRuler = pulses.length > 0 || bands.length > 0 || buckets.length > 0;

  const previewContext = (pulse: HighlightPulse) => {
    const segment = segments.find(s => s.id === pulse.segment_id);
    const speaker = participants.find(p => p.id === segment?.speaker_participant_id)?.display_name;
    const chapter = bands.find(band => pulse.start_s >= band.start && (pulse.start_s < band.end || band.last))?.label;
    return [speaker, chapter].filter(Boolean).join(' · ');
  };

  const live = ['active', 'paused'].includes(meetingStatus);
  let message = 'No highlights were captured for this meeting.';
  if (live) {
    if (highlightStatus === 'off') {
      message = isHost
        ? 'Live highlights are off. Enable Live highlight pulses under Meeting settings → Fast judgments.'
        : 'Live highlights are off. The host can enable them in Meeting settings.';
    } else if (highlightStatus === 'unknown') {
      message = 'Live highlight status is unavailable. Reconnect to check the current setting.';
    } else if (!cloudEnabled) {
      message = isHost
        ? 'Live highlights are on, but Cloud insights are off. Turn on Cloud insights to resume highlights.'
        : 'Live highlights are on, but the host has Cloud insights turned off.';
    } else if (highlightStatus === 'unavailable') {
      message = isHost
        ? 'Live highlights are on, but unavailable. Check Fast judgments setup in Meeting settings.'
        : 'Live highlights are on, but unavailable. The host needs to check Fast judgments setup.';
    } else if (meetingStatus === 'paused') {
      message = 'Live highlights are on. Resume the meeting to capture new highlights.';
    } else {
      message = 'Live highlights are on. Checking for highlights about once a minute as you speak.';
    }
  }

  return <section className="panel highlight-pulses mr" aria-label="Meeting highlights">
    <div className="mr-head">
      <h3>Meeting pulse</h3>
      {pulses.length > 0 && <span className="mr-hint">{playbackAvailable ? 'Click a moment to play' : 'No recording available'}</span>}
    </div>
    {live && <p className="pulse-empty mr-status" role="status">{message}</p>}
    {showRuler && <div className="mr-ruler">
      <div className="mr-area" ref={trackRef}>
        {pulses.length > 0 && <div className="mr-markers" style={{ height: 6 + rows * 20 }}>
          {placed.map(({ pulse: p, left, row }) => {
            const label = PULSE_LABELS[p.kind];
            const open = preview?.id === p.id && Boolean(activePulse);
            return <button type="button" key={p.id}
              aria-disabled={!playbackAvailable} className={`pulse-mark pulse-${p.kind}`}
              style={{ left: `${left}%`, bottom: row * 20 }}
              aria-label={`${label} at ${pulseTime(p.start_s)}: ${p.text}`}
              aria-haspopup="dialog" aria-expanded={open}
              aria-controls={open ? previewId : undefined}
              data-preview={open ? 'open' : undefined}
              onPointerEnter={event => { if (event.pointerType !== 'touch') showPreview(p, event.currentTarget); }}
              onPointerLeave={scheduleClose} onFocus={event => showPreview(p, event.currentTarget)} onBlur={scheduleClose}
              onKeyDown={event => {
                if (event.key === 'ArrowDown' && preview?.id !== p.id) {
                  event.preventDefault();
                  showPreview(p, event.currentTarget);
                }
              }}
              onClick={event => {
                if (playbackAvailable) { dismiss(); onSelect(p); }
                else showPreview(p, event.currentTarget);
              }}><PulseGlyph kind={p.kind} /></button>;
          })}
        </div>}
        <div className="mr-axis">
        {bands.length > 0 ? <div className="mr-bands">
          {bands.map((band, i) => <div key={`${i}-${band.start}`} className={`mr-band${band.last ? ' current' : ''}`}
            style={{ left: `${(band.start / duration) * 100}%`, width: `${((band.end - band.start) / duration) * 100}%` }}
            title={`${band.label} · ${pulseTime(band.start)}–${band.last && nowEdge ? 'now' : pulseTime(band.end)}`}>
            <span className="mr-band-label">{band.label}</span>
            <span className="mr-band-time">{pulseTime(band.start)}–{band.last && nowEdge ? 'now' : pulseTime(band.end)}</span>
          </div>)}
        </div> : <div className="mr-track" />}
        {buckets.length > 0 && <div className="mr-activity" role="img" aria-label="Speaker activity over the meeting">
          {buckets.map((bucket, i) => bucket.density
            ? <span key={i} style={{ background: bucket.color, opacity: 0.3 + 0.65 * bucket.density }} />
            : <span key={i} className="empty" />)}
        </div>}
        {nowEdge && <div className="mr-now" aria-hidden="true" />}
        </div>
      </div>
      <div className="mr-scale" aria-hidden="true">
        {ticks.map(t => <span key={t} className={`mr-tick${t > 0 && t < 1 ? ' mid' : ''}${t === 1 && nowEdge ? ' now' : ''}`}
          style={{ left: `${t * 100}%` }}>
          {t === 1 && nowEdge ? `now ${pulseTime(duration)}` : pulseTime(duration * t)}
        </span>)}
      </div>
    </div>}
    {counts.length > 0 && <div className="mr-legend">
      {counts.map(({ kind, count }) => <span key={kind} className={`mr-legend-item pulse-${kind}`}>
        <PulseGlyph kind={kind} size={10} />
        <span className={`pulse-label pulse-${kind}`}>{PULSE_LABELS[kind]}</span>
        <span className="mr-count">{count}</span>
      </span>)}
    </div>}
    {!pulses.length && !live && <p className="pulse-empty">{message}</p>}
    {preview && activePulse && <PulsePreview pulse={activePulse} anchor={preview.anchor} id={previewId}
      playbackAvailable={playbackAvailable} context={previewContext(activePulse)}
      onPlay={() => { const pulse = activePulse; preview.anchor.focus(); dismiss(); onSelect(pulse); }}
      onDismiss={dismiss}
      onEnter={() => { previewHovered.current = true; cancelClose(); }}
      onLeave={() => { previewHovered.current = false; scheduleClose(); }} />}
  </section>;
}
