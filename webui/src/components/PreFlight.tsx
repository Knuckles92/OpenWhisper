import { useState } from 'react';
import type { MeetingInfo, MeetingStateDoc, Op, Participant } from '../types';
import { initials, speakerColor } from '../people';
import MeetingBrief from './MeetingBrief';
import './preflight.css';

export interface PreFlightProps {
  state: MeetingStateDoc;
  meeting?: MeetingInfo | null;
  isHost: boolean;
  guestUrl?: string | null;
  participants?: Participant[];
  onlineIds?: Set<string>;
  /** True while speech is being heard, so the level only moves honestly. */
  listening?: boolean;
  onSendOp: (op: Op) => Promise<boolean>;
  onClientError?: (message: string) => void;
}

type Readiness = 'ready' | 'off' | 'warn';

function Glyph({ kind }: { kind: Readiness }) {
  return (
    <svg className={`pf-glyph pf-${kind}`} width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
      {kind === 'ready' && <path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />}
      {kind === 'off' && <path d="M4 8h8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />}
      {kind === 'warn' && (
        <>
          <path d="M8 2.2l6 11H2z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
          <path d="M8 6.5v3M8 11.6v.2" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
        </>
      )}
    </svg>
  );
}

function absoluteUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  if (url.startsWith('http')) return url;
  return `${location.origin}${url.startsWith('/') ? '' : '/'}${url}`;
}

/**
 * The opening state of a live meeting, before anyone has been transcribed:
 * what the record is for, whether capture is healthy, and who is here.
 */
export default function PreFlight({
  state,
  isHost,
  guestUrl = null,
  participants = [],
  onlineIds,
  listening = false,
  onSendOp,
  onClientError,
}: PreFlightProps) {
  const [copied, setCopied] = useState(false);
  const brief = (state.intent?.text ?? '').trim();
  const link = absoluteUrl(guestUrl);
  const capture = state.capture;
  const paused = state.status === 'paused';

  const copyLink = async () => {
    if (!link) return;
    try {
      await navigator.clipboard.writeText(link);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      onClientError?.(err instanceof Error ? err.message : 'The guest link could not be copied.');
    }
  };

  const cloud: [Readiness, string] = !state.cloud_enabled
    ? ['off', 'Off — transcript only']
    : state.intelligence_online ? ['ready', 'Online'] : ['warn', 'Offline'];
  const rows: Array<{ label: string; state: Readiness; detail: string }> = [
    { label: 'Microphone', state: capture?.mic_available ? 'ready' : 'warn', detail: capture?.mic_available ? 'Detected' : 'Not detected' },
    { label: 'System audio', state: capture?.loopback_available ? 'ready' : 'off', detail: capture?.loopback_available ? 'Capturing' : 'Not captured' },
    { label: 'AI insights', state: cloud[0], detail: cloud[1] },
    { label: 'Speaker separation', state: state.diarization_available ? 'ready' : 'off', detail: state.diarization_available ? 'Available' : 'Me / Others only' },
  ];

  return (
    <section className="preflight" aria-labelledby="preflight-heading">
      <div className="pf-intro">
        <div className="pf-eyebrow">{paused ? 'Paused · Getting started' : 'Getting started'}</div>
        <h2 id="preflight-heading" className="pf-headline">
          {brief || 'What should this meeting capture?'}
        </h2>
        {(isHost || brief) && (
          <MeetingBrief intent={state.intent} isHost={isHost} status={state.status} onSendOp={onSendOp} />
        )}
      </div>

      <div className="pf-grid">
        <div className="pf-card pf-listen" role="status" aria-live="polite">
          <span className={`pf-level${listening ? ' hearing' : ''}`} aria-hidden="true">
            {Array.from({ length: 9 }, (_, i) => <span key={i} />)}
          </span>
          <span className="pf-listen-text">
            {paused
              ? 'Capture is paused — resume to start the transcript.'
              : listening
                ? 'Hearing speech — the transcript will appear in a moment'
                : 'Waiting for the first words…'}
          </span>
        </div>

        <div className="pf-card">
          <h3 className="pf-card-title">Readiness</h3>
          <ul className="pf-checks">
            {rows.map((row) => (
              <li key={row.label} className="pf-check">
                <Glyph kind={row.state} />
                <span className="pf-check-label">{row.label}</span>
                <span className={`pf-check-state pf-${row.state}`}>{row.detail}</span>
              </li>
            ))}
            {isHost && (
              <li className="pf-check">
                <Glyph kind={link ? 'ready' : 'off'} />
                <span className="pf-check-label">Guest link</span>
                {link ? (
                  <button type="button" className="pf-copy" onClick={() => void copyLink()}>
                    {copied ? 'Copied' : 'Copy link'}
                  </button>
                ) : (
                  <span className="pf-check-state pf-off">Unavailable</span>
                )}
              </li>
            )}
          </ul>
          <span className="sr-only" role="status" aria-live="polite">{copied ? 'Guest link copied.' : ''}</span>
        </div>

        <div className="pf-card">
          <h3 className="pf-card-title">Who’s here</h3>
          {participants.length > 0 && (
            <ul className="pf-people">
              {participants.map((person) => (
                <li key={person.id} className="pf-person">
                  <span
                    className={`pf-avatar${onlineIds?.has(person.id) ? ' online' : ''}`}
                    style={{ background: speakerColor(person.id, participants) }}
                    aria-hidden="true"
                  >
                    {initials(person.display_name)}
                  </span>
                  <span>{person.display_name}</span>
                </li>
              ))}
            </ul>
          )}
          {participants.length <= 1 && (
            <p className="pf-hint">Share the guest link to invite others.</p>
          )}
        </div>
      </div>
    </section>
  );
}
