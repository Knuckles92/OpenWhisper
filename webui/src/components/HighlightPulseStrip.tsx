import type { HighlightPulse } from '../types';

export const PULSE_LABELS = {
  decision: 'Decision', disagreement: 'Disagreement', commitment: 'Dated commitment', number: 'Number',
};

export function pulseTime(seconds: number): string {
  return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`;
}

export default function HighlightPulseStrip({ pulses, onSelect }: {
  pulses: HighlightPulse[];
  onSelect: (pulse: HighlightPulse) => void;
}) {
  const duration = Math.max(60, ...pulses.map(p => p.start_s + 30));
  return <section className="panel highlight-pulses" aria-label="Meeting highlights">
    <div className="pulse-heading"><h3>Meeting pulse</h3><span>Click a moment to play</span></div>
    {pulses.length ? <div className="pulse-tracks">
      {Object.entries(PULSE_LABELS).map(([kind, label]) => <div className="pulse-lane" key={kind}>
        <span className={`pulse-label pulse-${kind}`}>{label}</span>
        <div className="pulse-track">
          {pulses.filter(p => p.kind === kind).map(p => <button type="button" key={p.id}
            className={`pulse-mark pulse-${kind}`} style={{ left: `${p.start_s / duration * 96}%` }}
            aria-label={`${label} at ${pulseTime(p.start_s)}: ${p.text}`}
            title={`${pulseTime(p.start_s)} · ${p.text}`} onClick={() => onSelect(p)} />)}
        </div>
      </div>)}
      <div className="pulse-scale"><span>0:00</span><span>{pulseTime(duration)}</span></div>
    </div> : <p className="pulse-empty">Highlights appear after a minute of speech when live highlights are enabled in Meeting settings.</p>}
  </section>;
}
