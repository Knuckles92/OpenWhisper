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
