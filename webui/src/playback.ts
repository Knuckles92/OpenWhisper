const pending = new WeakMap<HTMLAudioElement, () => void>();

/** Latest click wins, including while a fresh recording snapshot loads. */
export function playMoment(audio: HTMLAudioElement | null, seconds: number, source?: string) {
  if (!audio || !Number.isFinite(seconds) || seconds < 0) return;
  const previous = pending.get(audio);
  if (previous) audio.removeEventListener('loadedmetadata', previous);
  const apply = () => {
    pending.delete(audio);
    audio.currentTime = seconds;
    void audio.play()?.catch(() => { /* Controls remain available if autoplay is denied. */ });
  };
  if (source) {
    audio.src = source;
    audio.load();
  }
  if (!source && audio.readyState >= 1) apply();
  else {
    pending.set(audio, apply);
    audio.addEventListener('loadedmetadata', apply, {once: true});
  }
}
