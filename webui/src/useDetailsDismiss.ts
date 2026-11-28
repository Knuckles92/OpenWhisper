import { useEffect, type RefObject } from 'react';

/**
 * Dismiss a `<details>` popover on outside pointer clicks and `Escape`,
 * returning keyboard focus to its `<summary>` when dismissed via `Escape`.
 */
export function useDetailsDismiss(ref: RefObject<HTMLDetailsElement | null>): void {
  useEffect(() => {
    const onPointer = (event: PointerEvent) => {
      const details = ref.current;
      if (details?.open && event.target instanceof Node && !details.contains(event.target)) {
        details.removeAttribute('open');
      }
    };
    const onKey = (event: KeyboardEvent) => {
      const details = ref.current;
      if (event.key === 'Escape' && details?.open) {
        event.preventDefault();
        event.stopPropagation();
        details.removeAttribute('open');
        details.querySelector('summary')?.focus();
      }
    };
    document.addEventListener('pointerdown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [ref]);
}
