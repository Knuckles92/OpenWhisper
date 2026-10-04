import { useEffect, useRef, useState } from 'react';
import type { MeetingStateDoc } from '../types';
import './audio-check.css';

interface AudioCheckProps {
  capture: MeetingStateDoc['capture'];
  channel: 'mic' | 'loopback';
  paused?: boolean;
}

type Probe = { baseline: number; deadline: number; generation: number | undefined };

/** An explicit signal check; silence on its own is never treated as a fault. */
export default function AudioCheck({ capture, channel, paused = false }: AudioCheckProps) {
  const [probe, setProbe] = useState<Probe | null>(null);
  const [outcome, setOutcome] = useState<'heard' | 'not-heard' | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const isMic = channel === 'mic';
  const label = isMic ? 'Microphone' : 'System audio';
  const available = isMic ? capture.mic_available : capture.loopback_available;
  const receiving = isMic ? capture.mic_receiving : capture.loopback_receiving;
  const signalWindows = (isMic ? capture.mic_signal_windows : capture.loopback_signal_windows) ?? 0;
  const sourceGeneration = isMic ? capture.mic_source_generation : capture.loopback_source_generation;
  const previousGeneration = useRef(sourceGeneration);

  useEffect(() => {
    if (previousGeneration.current !== sourceGeneration) {
      previousGeneration.current = sourceGeneration;
      setProbe(null);
      setOutcome(null);
    }
  }, [sourceGeneration]);

  useEffect(() => {
    if (!probe) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 200);
    return () => clearInterval(timer);
  }, [probe]);

  useEffect(() => {
    if (!probe) return;
    if (!available || paused || probe.generation !== sourceGeneration) {
      setProbe(null);
      setOutcome(null);
      return;
    }
    if (signalWindows > probe.baseline) {
      setOutcome('heard');
      setProbe(null);
    } else if (now >= probe.deadline) {
      setOutcome('not-heard');
      setProbe(null);
    }
  }, [probe, signalWindows, now, available, paused, sourceGeneration]);

  const start = () => {
    setOutcome(null);
    setNow(Date.now());
    // Ask for five seconds of input; allow two more for the one-second
    // watchdog/status update and transport to reach the dashboard.
    setProbe({ baseline: signalWindows, deadline: Date.now() + 7000,
      generation: sourceGeneration });
  };
  const detail = !available
    ? `${label} is unavailable.`
    : paused
      ? 'Resume capture to test.'
      : probe
        ? isMic ? 'Speak into the microphone now…' : 'Play the meeting audio now…'
        : outcome === 'heard'
          ? 'Audio signal detected. Confirm it came from the intended device; this check cannot identify the source.'
          : outcome === 'not-heard'
            ? receiving === false
              ? `No audio blocks arrived from ${label.toLowerCase()}. Check the device or its permissions, then try again.`
              : isMic
                ? 'No signal detected. Check the selected microphone and its level, then try again.'
                : 'No signal detected. Check the output device while audio is playing, then try again.'
            : receiving === false
              ? 'Waiting for audio blocks from this device.'
              : isMic
                ? 'Speak for five seconds to verify this input.'
                : 'Play audio from the meeting app for five seconds to verify this input.';

  return (
    <div className="audio-check">
      <button type="button" onClick={start} disabled={!available || paused || Boolean(probe)}>
        {probe ? 'Checking…' : `Check ${label.toLowerCase()}`}
      </button>
      <span className={`audio-check-detail${outcome === 'not-heard' ? ' needs-attention' : ''}`} role="status">
        {detail}
      </span>
    </div>
  );
}
