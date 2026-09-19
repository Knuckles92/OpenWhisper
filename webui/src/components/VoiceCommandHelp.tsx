import { useEffect, useId, useState } from 'react';
import type { VoiceCommandGuide } from '../types';
import './VoiceCommandHelp.css';

const DISMISS_KEY = 'ow.voiceHintDismissed';

/** Dismissal is per meeting, so a new meeting gets one quiet reminder. */
function readDismissed(meetingId: string): boolean {
  try {
    return sessionStorage.getItem(DISMISS_KEY) === meetingId;
  } catch {
    return false;
  }
}

function blockedReason(paused: boolean, cloudEnabled: boolean, isHost: boolean): string | null {
  if (paused) return 'paused';
  if (cloudEnabled) return null;
  return isHost ? 'needs Cloud intelligence' : 'host has Cloud intelligence off';
}

export default function VoiceCommandHelp({ guide, meetingId, cloudEnabled, paused, isHost }: {
  guide?: VoiceCommandGuide; meetingId: string; cloudEnabled: boolean; paused: boolean; isHost: boolean;
}) {
  const [dismissed, setDismissed] = useState(() => readDismissed(meetingId));
  const [open, setOpen] = useState(false);
  const bodyId = useId();

  useEffect(() => {
    setDismissed(readDismissed(meetingId));
    setOpen(false);
  }, [meetingId]);

  if (!guide || dismissed) return null;

  const blocked = blockedReason(paused, cloudEnabled, isHost);
  const alternates = guide.names.filter(name => name !== guide.primary);

  function dismiss() {
    setDismissed(true);
    try {
      sessionStorage.setItem(DISMISS_KEY, meetingId);
    } catch {
      // Private browsing: the hint simply returns on reload.
    }
  }

  return <div className={`voice-hint${open ? ' is-open' : ''}${blocked ? ' is-blocked' : ''}`}>
    <div className="voice-hint-row">
      <span className="voice-hint-dot" aria-hidden="true" />
      <p className="voice-hint-lead">
        Say <b>“{guide.primary}, …”</b> to steer the note taker
        {blocked && <span className="voice-hint-blocked"> · {blocked}</span>}
      </p>
      <button type="button" className="voice-hint-more" aria-expanded={open} aria-controls={bodyId}
        onClick={() => setOpen(value => !value)}>{open ? 'Hide' : 'Commands'}</button>
      <button type="button" className="voice-hint-close" aria-label="Hide voice command hint"
        onClick={dismiss}>×</button>
    </div>
    {open && <div className="voice-hint-body" id={bodyId}>
      <ul className="voice-hint-list">
        {guide.examples.map(example => <li key={example.label}>
          <span className="voice-hint-phrase">“{example.phrase}”</span>
          <span className="voice-hint-label">{example.label}</span>
        </li>)}
      </ul>
      <p className="voice-hint-note">
        “That” means the point just spoken
        {alternates.length > 0 && <> · also answers to {alternates.map((name, i) =>
          <span key={name}>{i > 0 && ' or '}“{name}”</span>)}</>}
        {isHost
          ? ' · set up under Settings → Meeting Mode → Fast judgments'
          : ' · speak into the meeting audio; this page does not open your microphone'}
      </p>
    </div>}
  </div>;
}
