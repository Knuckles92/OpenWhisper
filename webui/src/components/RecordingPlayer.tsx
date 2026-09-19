import { useEffect, useState, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import type { HighlightPulse } from '../types';
import { cancelPendingMoment, playMoment, stopPlayback } from '../playback';
import { PULSE_LABELS, pulseTime } from './HighlightPulseStrip';

export type PlaybackMoment = Pick<HighlightPulse, 'start_s' | 'text'> & { kind?: HighlightPulse['kind'] };

interface Props {
  audioRef: RefObject<HTMLAudioElement>;
  src: string;
  label: string;
  preload?: 'none' | 'metadata';
  moment: PlaybackMoment | null;
  onClose: () => void;
}

export default function RecordingPlayer({ audioRef, src, label, preload = 'metadata', moment, onClose }: Props) {
  const [status, setStatus] = useState('');
  const [duration, setDuration] = useState(0);
  const [position, setPosition] = useState(0);
  const [paused, setPaused] = useState(true);
  const [failed, setFailed] = useState(false);
  const ready = duration > 0;

  useEffect(() => {
    const audio = audioRef.current;
    return () => stopPlayback(audio);
  }, [audioRef, src]);

  const close = () => {
    stopPlayback(audioRef.current);
    onClose();
  };
  const seek = (seconds: number) => {
    const audio = audioRef.current;
    if (!audio || !Number.isFinite(audio.duration)) return;
    audio.currentTime = Math.max(0, Math.min(audio.duration, seconds));
    setPosition(audio.currentTime);
  };
  const toggle = () => {
    const audio = audioRef.current;
    if (!audio) return;
    if (!audio.paused) audio.pause();
    else void audio.play().catch(() => setStatus('Playback did not start. Press play to try again.'));
  };

  return <div className="recording-player no-print">
    <audio ref={audioRef} controls hidden={Boolean(moment)} aria-label={label} preload={preload} src={src}
      onLoadStart={() => { setStatus('Loading recording…'); setDuration(0); setPaused(true); setFailed(false); }}
      onLoadedMetadata={event => {
        const audio = event.currentTarget;
        setDuration(Number.isFinite(audio.duration) ? audio.duration : 0);
        setPosition(audio.currentTime); setStatus('Ready to play');
      }}
      onDurationChange={event => setDuration(Number.isFinite(event.currentTarget.duration) ? event.currentTarget.duration : 0)}
      onTimeUpdate={event => setPosition(event.currentTarget.currentTime)}
      onPlay={() => setPaused(false)}
      onPlaying={() => { setStatus('Playing'); setPaused(false); setFailed(false); }}
      onPause={() => { setPaused(true); setStatus('Paused'); }}
      onEnded={() => { setPaused(true); setStatus('Replay finished'); }}
      onWaiting={() => setStatus('Buffering…')}
      onError={() => {
        if (audioRef.current) cancelPendingMoment(audioRef.current);
        setFailed(true); setDuration(0); setPaused(true); setStatus('Recording unavailable. Please try again.');
      }} />
    {moment && createPortal(<section className="recording-player is-floating no-print" aria-label="Meeting replay"
      onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); close(); } }}>
      <div className="replay-heading">
        <div className="replay-icon" aria-hidden="true">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
            <path d="M4 10v4M8 6v12M12 3v18M16 7v10M20 10v4" strokeLinecap="round" />
          </svg>
        </div>
        <div className="replay-heading-copy"><strong>Meeting replay</strong>
          <span>{moment.kind ? PULSE_LABELS[moment.kind] : 'Selected moment'} · {pulseTime(moment.start_s)}</span>
        </div>
        <button type="button" className="replay-close" aria-label="Close replay and stop audio" onClick={close}>×</button>
      </div>
      <p className="replay-quote" title={moment.text}>{moment.text}</p>
      <div className="replay-transport">
        <button type="button" className="replay-toggle" aria-label={paused ? 'Play replay' : 'Pause replay'} disabled={!ready} onClick={toggle}>
          <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            {paused ? <path d="M7 4l14 8-14 8z" /> : <path d="M6 4h4v16H6zM14 4h4v16h-4z" />}
          </svg>
        </button>
        <div className="replay-timeline">
          <input type="range" aria-label="Replay position" aria-valuetext={`${pulseTime(position)} of ${pulseTime(duration)}`}
            min={0} max={duration || 1} step={0.1} value={Math.min(position, duration)} disabled={!ready}
            onChange={event => seek(Number(event.target.value))} />
          <div className="replay-times"><span>{pulseTime(position)}</span><span>{ready ? pulseTime(duration) : '—:—'}</span></div>
        </div>
      </div>
      <div className="replay-footer">
        <div className="replay-skips">
          <button type="button" aria-label="Skip back 10 seconds" disabled={!ready} onClick={() => seek((audioRef.current?.currentTime || 0) - 10)}>−10s</button>
          <button type="button" aria-label="Skip forward 10 seconds" disabled={!ready} onClick={() => seek((audioRef.current?.currentTime || 0) + 10)}>+10s</button>
        </div>
        <span role="status">{status || 'Loading recording…'}</span>
        {failed && <button type="button" onClick={() => playMoment(audioRef.current, moment.start_s, audioRef.current?.src || src)}>Retry</button>}
      </div>
    </section>, document.body)}
  </div>;
}
