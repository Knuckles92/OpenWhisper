import { useEffect, useId, useRef, useState } from 'react';
import { DESIGN_THEMES, readDesignTheme, readTheme, saveDesignTheme, saveTheme, type DesignTheme, type ThemeChoice } from '../theme';
import './themePicker.css';

const MODES: Array<[ThemeChoice, string]> = [['system', 'Auto'], ['light', 'Light'], ['dark', 'Dark']];

export default function ThemePicker() {
  const [design, setDesign] = useState<DesignTheme>(readDesignTheme);
  const [mode, setMode] = useState<ThemeChoice>(readTheme);
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const groupId = useId();

  useEffect(() => {
    const dismiss = (event: PointerEvent) => {
      const details = detailsRef.current;
      if (details?.open && event.target instanceof Node && !details.contains(event.target)) details.open = false;
    };
    document.addEventListener('pointerdown', dismiss);
    return () => document.removeEventListener('pointerdown', dismiss);
  }, []);

  return (
    <details className="appearance-picker" ref={detailsRef} onKeyDown={(event) => {
      if (event.key === 'Escape' && detailsRef.current?.open) {
        event.preventDefault();
        event.stopPropagation();
        detailsRef.current.open = false;
        detailsRef.current.querySelector('summary')?.focus();
      }
    }}>
      <summary className="appearance-trigger" aria-label="Appearance">
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.3" aria-hidden="true">
          <circle cx="8" cy="8" r="5.7" /><path d="M8 2.3a5.7 5.7 0 010 11.4z" fill="currentColor" stroke="none" />
        </svg>
        <span>Appearance</span>
      </summary>
      <div className="appearance-panel">
        <fieldset className="appearance-designs">
          <legend>Make yourself at home</legend>
          <p className="appearance-hint">Choose a look for your meeting space.</p>
          <div className="appearance-grid">
            {DESIGN_THEMES.map((option) => (
              <label key={option.id} className="appearance-option" data-preview={option.id}>
                <input className="sr-only" type="radio" name={`${groupId}-design`} value={option.id}
                  checked={design === option.id} onChange={() => { saveDesignTheme(option.id); setDesign(option.id); }} />
                <span className="appearance-swatch" aria-hidden="true">
                  <span className="appearance-swatch-bar" />
                  <span className="appearance-swatch-content"><i /><i /><i /></span>
                </span>
                <span className="appearance-option-name">{option.label}<span className="appearance-check" aria-hidden="true">✓</span></span>
                <span className="appearance-option-description">{option.description}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <fieldset className="appearance-mode">
          <legend>Color mode</legend>
          <div className="appearance-modes">
            {MODES.map(([value, label]) => (
              <label key={value}>
                <input className="sr-only" type="radio" name={`${groupId}-mode`} value={value} checked={mode === value}
                  onChange={() => { saveTheme(value); setMode(value); }} />
                <span>{label}</span>
              </label>
            ))}
          </div>
        </fieldset>
        <p className="appearance-footnote">Saved in this browser. Auto follows your device.</p>
      </div>
    </details>
  );
}
