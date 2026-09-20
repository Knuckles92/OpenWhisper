import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import type { HighlightPulse, LiveHighlightsStatus } from '../types';

export const PULSE_LABELS = {
  decision: 'Decision', disagreement: 'Disagreement', commitment: 'Dated commitment', number: 'Number',
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

function PulsePreview({ pulse, anchor, id, playbackAvailable, onDismiss, onEnter, onLeave }: {
  pulse: HighlightPulse;
  anchor: HTMLButtonElement;
  id: string;
  playbackAvailable: boolean;
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
    className={`pulse-preview pulse-${pulse.kind} no-print`} data-side={position.above ? 'above' : 'below'}
    style={{ left: position.left, top: position.top, maxHeight: position.maxHeight || undefined, '--pulse-arrow-left': `${position.arrow}px` } as CSSProperties}
    onPointerEnter={onEnter} onPointerLeave={onLeave} onFocus={onEnter} onBlur={onLeave}>
    <div className="pulse-preview-heading">
      <div><span className="pulse-preview-eyebrow">Meeting pulse</span>
        <strong id={`${id}-heading`} className="pulse-preview-kind"><span aria-hidden="true" />{PULSE_LABELS[pulse.kind]}</strong>
      </div>
      <span className="pulse-preview-time">{pulseTime(pulse.start_s)}</span>
    </div>
    <div className="pulse-preview-body">
      <PulseEvidence key={pulse.id} pulse={pulse} />
    </div>
    <div className="pulse-preview-footer">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
        {playbackAvailable ? <path d="m9 5 11 7-11 7V5Z" /> : <><circle cx="12" cy="12" r="9" /><path d="M12 7v6m0 3v1" /></>}
      </svg>
      <span>{playbackAvailable ? 'Click pulse to replay this moment' : 'No recording available for this moment'}</span>
      <button type="button" className="pulse-preview-close" onClick={() => { anchor.focus(); onDismiss(); }}>Close</button>
    </div>
  </div>, document.body);
}

export default function HighlightPulseStrip({ pulses, onSelect, playbackAvailable = true,
  highlightStatus = 'unknown', meetingStatus = 'ended', cloudEnabled = true, isHost = false }: {
  pulses: HighlightPulse[];
  playbackAvailable?: boolean;
  highlightStatus?: LiveHighlightsStatus;
  meetingStatus?: string;
  cloudEnabled?: boolean;
  isHost?: boolean;
  onSelect: (pulse: HighlightPulse) => void;
}) {
  const [preview, setPreview] = useState<{ id: string; anchor: HTMLButtonElement } | null>(null);
  const previewId = useId();
  const previewHovered = useRef(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout>>();
  const cancelClose = useCallback(() => { clearTimeout(closeTimer.current); }, []);
  const dismiss = useCallback(() => { cancelClose(); previewHovered.current = false; setPreview(null); }, [cancelClose]);
  const activePulse = pulses.find(p => p.id === preview?.id);
  useEffect(() => cancelClose, [cancelClose]);
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
  const duration = Math.max(60, ...pulses.map(p => p.start_s + 30));
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
  return <section className="panel highlight-pulses" aria-label="Meeting highlights">
    <div className="pulse-heading"><h3>Meeting pulse</h3>{pulses.length > 0 && <span>{playbackAvailable ? 'Click a moment to play' : 'No recording available'}</span>}</div>
    {live && <p className="pulse-empty" role="status">{message}</p>}
    {pulses.length ? <div className="pulse-tracks">
      {Object.entries(PULSE_LABELS).map(([kind, label]) => <div className="pulse-lane" key={kind}>
        <span className={`pulse-label pulse-${kind}`}>{label}</span>
        <div className="pulse-track">
          {pulses.filter(p => p.kind === kind).map(p => <button type="button" key={p.id}
            aria-disabled={!playbackAvailable} className={`pulse-mark pulse-${kind}`} style={{ left: `${p.start_s / duration * 96}%` }}
            aria-label={`${label} at ${pulseTime(p.start_s)}: ${p.text}`}
            aria-haspopup="dialog" aria-expanded={preview?.id === p.id && Boolean(activePulse)}
            aria-controls={preview?.id === p.id && activePulse ? previewId : undefined}
            data-preview={preview?.id === p.id && Boolean(activePulse) ? 'open' : undefined}
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
            }} />)}
        </div>
      </div>)}
      <div className="pulse-scale"><span>0:00</span><span>{pulseTime(duration)}</span></div>
    </div> : !live && <p className="pulse-empty">{message}</p>}
    {preview && activePulse && <PulsePreview pulse={activePulse} anchor={preview.anchor} id={previewId}
      playbackAvailable={playbackAvailable} onDismiss={dismiss}
      onEnter={() => { previewHovered.current = true; cancelClose(); }}
      onLeave={() => { previewHovered.current = false; scheduleClose(); }} />}
  </section>;
}
