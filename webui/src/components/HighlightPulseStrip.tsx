import type { HighlightPulse, LiveHighlightsStatus } from '../types';

export const PULSE_LABELS = {
  decision: 'Decision', disagreement: 'Disagreement', commitment: 'Dated commitment', number: 'Number',
};

export function pulseTime(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
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
            disabled={!playbackAvailable} className={`pulse-mark pulse-${kind}`} style={{ left: `${p.start_s / duration * 96}%` }}
            aria-label={`${label} at ${pulseTime(p.start_s)}: ${p.text}`}
            title={`${pulseTime(p.start_s)} · ${p.text}`} onClick={() => onSelect(p)} />)}
        </div>
      </div>)}
      <div className="pulse-scale"><span>0:00</span><span>{pulseTime(duration)}</span></div>
    </div> : !live && <p className="pulse-empty">{message}</p>}
  </section>;
}
