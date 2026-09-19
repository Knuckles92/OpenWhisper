import { useEffect, useRef, useState, type RefObject } from 'react';

const pending = new WeakMap<HTMLAudioElement, () => void>();

export function cancelPendingMoment(audio: HTMLAudioElement) {
  const previous = pending.get(audio);
  if (previous) audio.removeEventListener('loadedmetadata', previous);
  pending.delete(audio);
}

export function stopPlayback(audio: HTMLAudioElement | null) {
  if (!audio) return;
  cancelPendingMoment(audio);
  audio.pause();
}

/** Latest click wins, including while a fresh recording snapshot loads. */
export function playMoment(audio: HTMLAudioElement | null, seconds: number, source?: string) {
  if (!audio || !Number.isFinite(seconds) || seconds < 0) return;
  cancelPendingMoment(audio);
  const apply = () => {
    pending.delete(audio);
    audio.currentTime = Number.isFinite(audio.duration) ? Math.min(seconds, audio.duration) : seconds;
    void audio.play()?.catch(() => { /* The visible controls allow a manual retry. */ });
  };
  if (!source && audio.readyState >= 1) apply();
  else {
    pending.set(audio, apply);
    audio.addEventListener('loadedmetadata', apply, {once: true});
    if (source) {
      audio.src = source;
      audio.load();
    }
  }
}

export interface AudioClock {
  /** Meeting-clock seconds the recording is sitting at. */
  time: number;
  playing: boolean;
  /** 0 until the browser reports a finite duration. */
  duration: number;
}

const IDLE: AudioClock = { time: 0, playing: false, duration: 0 };

// `timeupdate` fires roughly 4x a second, which a CSS transition smooths out;
// an animation frame loop would cost far more for a playhead nobody measures.
const CLOCK_EVENTS = [
  'timeupdate', 'play', 'playing', 'pause', 'ended', 'seeking', 'seeked',
  'loadedmetadata', 'durationchange', 'emptied',
];

/**
 * Follow an `<audio>` element owned elsewhere in the tree. Subscribing here
 * rather than lifting position into the dashboard keeps the 4Hz re-render
 * inside whichever small component draws a playhead.
 *
 * `audioKey` must change whenever the element itself is replaced (React `key`,
 * meeting id, …) so the listeners re-bind instead of holding a detached node.
 */
export function useAudioClock(
  audioRef?: RefObject<HTMLAudioElement | null>,
  audioKey = '',
): AudioClock {
  const [clock, setClock] = useState<AudioClock>(IDLE);
  useEffect(() => {
    const audio = audioRef?.current;
    if (!audio) {
      setClock(IDLE);
      return undefined;
    }
    const read = () => setClock((previous) => {
      const next: AudioClock = {
        time: audio.currentTime,
        playing: !audio.paused && !audio.ended,
        duration: Number.isFinite(audio.duration) ? audio.duration : 0,
      };
      return previous.time === next.time
        && previous.playing === next.playing
        && previous.duration === next.duration ? previous : next;
    });
    for (const name of CLOCK_EVENTS) audio.addEventListener(name, read);
    read();
    return () => {
      for (const name of CLOCK_EVENTS) audio.removeEventListener(name, read);
    };
  }, [audioRef, audioKey]);
  return clock;
}

/** Anything with a meeting-clock span the playhead can sit inside. */
export interface PlaybackSpan {
  id: string;
  start_s: number;
  end_s: number;
}

/** A turn shorter than this still needs a window the playhead can land in. */
const MIN_SPAN_S = 0.4;

/**
 * The span covering `seconds`, or undefined in the gaps between them.
 *
 * Mic and loopback turns overlap, so more than one span can contain a moment;
 * the latest to have started is the one being spoken, which keeps the answer
 * the same whether the caller holds turns oldest- or newest-first.
 */
export function spanAt<T extends PlaybackSpan>(spans: T[], seconds: number): T | undefined {
  let best: T | undefined;
  for (const span of spans) {
    if (seconds < span.start_s) continue;
    if (seconds >= Math.max(span.end_s, span.start_s + MIN_SPAN_S)) continue;
    if (!best || span.start_s > best.start_s) best = span;
  }
  return best;
}

/**
 * The id of the span the recording is sitting inside, or null.
 *
 * Deliberately not layered on `useAudioClock`: the transcript rail renders
 * every turn, and re-rendering that whole list 4x a second just to move one
 * CSS class is waste. Keeping only the derived id in state means a render when
 * the turn actually changes — a few times a minute.
 *
 * `audioKey` carries the same contract as `useAudioClock`: change it whenever
 * the element is replaced so the listeners re-bind.
 */
export function usePlayingSpanId(
  spans: PlaybackSpan[],
  audioRef?: RefObject<HTMLAudioElement | null>,
  audioKey = '',
): string | null {
  const [id, setId] = useState<string | null>(null);
  // Read through a ref so arriving turns never re-bind the listeners; the next
  // `timeupdate` picks them up, and a paused playhead has nothing to revise.
  const spansRef = useRef(spans);
  spansRef.current = spans;

  useEffect(() => {
    const audio = audioRef?.current;
    if (!audio) {
      setId(null);
      return undefined;
    }
    const read = () => {
      const at = audio.currentTime;
      // Before the first play there is no position worth marking.
      const started = at > 0 || (!audio.paused && !audio.ended);
      const next = started ? spanAt(spansRef.current, at)?.id ?? null : null;
      setId((previous) => (previous === next ? previous : next));
    };
    for (const name of CLOCK_EVENTS) audio.addEventListener(name, read);
    read();
    return () => {
      for (const name of CLOCK_EVENTS) audio.removeEventListener(name, read);
    };
  }, [audioRef, audioKey]);

  return id;
}
