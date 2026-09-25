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

export const DESIGN_THEMES = [
  { id: 'studio', label: 'Studio', description: 'Forest ink & soft peach' },
  { id: 'sunlit', label: 'Sunlit', description: 'Warm paper & garden green' },
  { id: 'sage', label: 'Sage', description: 'Quiet greens & a dark bar' },
  { id: 'focus', label: 'Focus Stage', description: 'Airy sage & centered type' },
  { id: 'original', label: 'Original', description: 'Classic charcoal & blue' },
] as const;

export type DesignTheme = (typeof DESIGN_THEMES)[number]['id'];
const DESIGN_STORAGE_KEY = 'openwhisper.meeting.design';

export function readDesignTheme(): DesignTheme {
  try {
    const stored = localStorage.getItem(DESIGN_STORAGE_KEY);
    const theme = DESIGN_THEMES.find((option) => option.id === stored);
    if (theme) return theme.id;
  } catch {
    // The default remains usable when browser storage is blocked.
  }
  return 'studio';
}

export function applyDesignTheme(choice: DesignTheme): void {
  document.documentElement.setAttribute('data-design', choice);
}

export function saveDesignTheme(choice: DesignTheme): void {
  applyDesignTheme(choice);
  try {
    localStorage.setItem(DESIGN_STORAGE_KEY, choice);
  } catch {
    // Apply for this visit even if the browser cannot remember it.
  }
}
