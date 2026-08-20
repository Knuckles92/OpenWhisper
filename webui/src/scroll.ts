/** Nearest ancestor that actually scrolls, ignoring the document itself. */
export function nearestOverflowParent(el: HTMLElement | null): HTMLElement | null {
  let node = el?.parentElement ?? null;
  while (node && node !== document.body && node !== document.documentElement) {
    const { overflowY } = getComputedStyle(node);
    if (overflowY === 'auto' || overflowY === 'scroll' || overflowY === 'overlay') {
      return node;
    }
    node = node.parentElement;
  }
  return null;
}

/**
 * Keep `el` visible inside its own scroller.
 *
 * `Element.scrollIntoView` also walks up to the window, which hides the
 * meeting header (Pause / End) whenever a citation or highlight fires.
 */
export function scrollChildIntoView(
  el: HTMLElement,
  { block = 'center' }: { block?: 'start' | 'center' | 'nearest' } = {},
): void {
  const parent = nearestOverflowParent(el);
  if (!parent) return;
  const parentRect = parent.getBoundingClientRect();
  const elRect = el.getBoundingClientRect();
  const offsetTop = elRect.top - parentRect.top + parent.scrollTop;
  let next = parent.scrollTop;
  if (block === 'center') {
    next = offsetTop - parent.clientHeight / 2 + elRect.height / 2;
  } else if (block === 'start') {
    next = offsetTop;
  } else if (elRect.top < parentRect.top) {
    next = offsetTop;
  } else if (elRect.bottom > parentRect.bottom) {
    next = offsetTop - parent.clientHeight + elRect.height;
  } else {
    return;
  }
  const max = Math.max(0, parent.scrollHeight - parent.clientHeight);
  parent.scrollTo({ top: Math.max(0, Math.min(max, next)) });
}

/** True when the scroller is close enough to the end to keep following new content. */
export function isStuckToEnd(scroller: HTMLElement, thresholdPx = 80): boolean {
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < thresholdPx;
}

/** The dashboard page scroller, or the nearest overflow parent as a fallback. */
export function workspaceScroller(from: HTMLElement | null): HTMLElement | null {
  const marked = from?.closest('[data-workspace-scroll]');
  if (marked instanceof HTMLElement) return marked;
  return nearestOverflowParent(from);
}
