/** Viewer's colour theme: follow the OS, or pin light/dark for this browser. */
export type ThemeChoice = 'system' | 'light' | 'dark';

const STORAGE_KEY = 'openwhisper.meeting.theme';

export function readTheme(): ThemeChoice {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    if (value === 'light' || value === 'dark') return value;
  } catch {
    // Storage can be blocked (private windows); the OS setting still applies.
  }
  return 'system';
}

/** Point the CSS at a theme; "system" defers to prefers-color-scheme. */
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', choice);
}

export function saveTheme(choice: ThemeChoice): void {
  applyTheme(choice);
  try {
    if (choice === 'system') localStorage.removeItem(STORAGE_KEY);
    else localStorage.setItem(STORAGE_KEY, choice);
  } catch {
    // Not persisted, but applied for this visit.
  }
}
