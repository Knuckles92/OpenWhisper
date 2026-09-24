import { useCallback, useEffect, useState, type RefObject } from 'react';
import { workspaceScroller } from '../scroll';
import './pocket.css';

export type PocketTab = 'live' | 'notes' | 'transcript' | 'captured' | 'people';

const POCKET_QUERY = '(max-width: 639.98px)';
const TAB_KEY = 'openwhisper.pocketTab';

/** True on phone-width viewports; false wherever matchMedia is unavailable. */
export function usePocketLayout(): boolean {
  const query = () =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(POCKET_QUERY)
      : null;
  const [pocket, setPocket] = useState(() => query()?.matches ?? false);
  useEffect(() => {
    const media = query();
    if (!media) return undefined;
    const sync = () => setPocket(media.matches);
    sync();
    media.addEventListener?.('change', sync);
    return () => media.removeEventListener?.('change', sync);
  }, []);
  return pocket;
}

function readTab(): PocketTab | null {
  try {
    const value = sessionStorage.getItem(TAB_KEY);
    return value === 'live' || value === 'notes' || value === 'transcript'
      || value === 'captured' || value === 'people' ? value : null;
  } catch {
    return null;
  }
}

/** Tabs available for the meeting's phase: an ended meeting has no live notes. */
export function pocketTabs(ended: boolean): PocketTab[] {
  return ended
    ? ['live', 'transcript', 'captured', 'people']
    : ['live', 'notes', 'captured', 'people'];
}

/** The selected phone tab, remembered for this browser session. */
export function usePocketTab(ended: boolean): [PocketTab, (tab: PocketTab) => void] {
  const [tab, setTab] = useState<PocketTab>(() => readTab() ?? 'live');
  const choose = useCallback((next: PocketTab) => {
    setTab(next);
    try {
      sessionStorage.setItem(TAB_KEY, next);
    } catch {
      // Private windows may refuse storage; the choice just won't persist.
    }
  }, []);
  const tabs = pocketTabs(ended);
  return [tabs.includes(tab) ? tab : 'live', choose];
}

const LABELS: Record<PocketTab, string> = {
  live: 'Live',
  notes: 'Notes',
  transcript: 'Transcript',
  captured: 'Captured',
  people: 'People',
};

function TabIcon({ tab }: { tab: PocketTab }) {
  const common = {
    width: 20, height: 20, viewBox: '0 0 20 20', fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const, 'aria-hidden': true,
  };
  switch (tab) {
    case 'live':
      return <svg {...common}><circle cx="10" cy="10" r="2.5" fill="currentColor" /><path d="M5.5 5.5a6.4 6.4 0 000 9M14.5 5.5a6.4 6.4 0 010 9" /></svg>;
    case 'notes':
      return <svg {...common}><path d="M5 3.5h7l3 3v10H5z" /><path d="M7.5 9h5M7.5 12h5" /></svg>;
    case 'transcript':
      return <svg {...common}><path d="M3.5 5h13M3.5 9h13M3.5 13h8" /></svg>;
    case 'captured':
      return <svg {...common}><path d="M10 3l2.2 4.5 4.8.7-3.5 3.4.8 4.9L10 14.2l-4.3 2.3.8-4.9L3 8.2l4.8-.7z" /></svg>;
    default:
      return <svg {...common}><circle cx="8" cy="7" r="2.6" /><path d="M3.5 16c.6-2.6 2.3-4 4.5-4s3.9 1.4 4.5 4" /><path d="M13 5.2a2.4 2.4 0 010 4.6M14.5 12.3c1 .6 1.7 1.9 2 3.7" /></svg>;
  }
}

/** Bottom tab bar for the phone layout. */
export function PocketTabs({ tabs, active, onSelect, capturedCount }: {
  tabs: PocketTab[];
  active: PocketTab;
  onSelect: (tab: PocketTab) => void;
  capturedCount: number;
}) {
  return (
    <nav className="pocket-tabs" aria-label="Meeting sections">
      {tabs.map((tab) => (
        <button
          key={tab}
          type="button"
          className={`pocket-tab${tab === active ? ' active' : ''}`}
          aria-current={tab === active ? 'page' : undefined}
          onClick={() => onSelect(tab)}
        >
          <span className="pocket-tab-icon">
            <TabIcon tab={tab} />
            {tab === 'captured' && capturedCount > 0 && (
              <span className="pocket-badge" aria-label={`${capturedCount} captured`}>
                {capturedCount > 99 ? '99+' : capturedCount}
              </span>
            )}
          </span>
          <span className="pocket-tab-label">{LABELS[tab]}</span>
        </button>
      ))}
    </nav>
  );
}

/**
 * "Jump to live" pill: newest content sits at the top of the Live tab, so the
 * pill shows once the reader has scrolled away from it.
 */
export function JumpToLive({ anchorRef, enabled }: {
  anchorRef: RefObject<HTMLElement>;
  enabled: boolean;
}) {
  const [away, setAway] = useState(false);
  useEffect(() => {
    if (!enabled) {
      setAway(false);
      return undefined;
    }
    const scroller = workspaceScroller(anchorRef.current);
    if (!scroller) return undefined;
    const sync = () => setAway(scroller.scrollTop > 480);
    sync();
    scroller.addEventListener('scroll', sync, { passive: true });
    return () => scroller.removeEventListener('scroll', sync);
  }, [anchorRef, enabled]);

  if (!enabled || !away) return null;
  return (
    <button
      type="button"
      className="pocket-jump"
      onClick={() => {
        const scroller = workspaceScroller(anchorRef.current);
        scroller?.scrollTo({ top: 0, behavior: 'smooth' });
      }}
    >
      <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
        <path d="M6 10V2M2.5 5.5L6 2l3.5 3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      Jump to live
    </button>
  );
}
