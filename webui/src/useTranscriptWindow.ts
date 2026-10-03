import { useEffect, useMemo, useRef, useState } from 'react';
import { nearestOverflowParent } from './scroll';
import type { Segment } from './types';

const ESTIMATED_HEIGHT = 96;
const OVERSCAN = 600;

function rowAt(offsets: number[], position: number): number {
  let lo = 0, hi = offsets.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (offsets[mid] <= position) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

/** Window variable-height transcript rows inside the existing page/rail scroller. */
export function useTranscriptWindow(segments: Segment[], enabled: boolean, targetId: string | null) {
  const listRef = useRef<HTMLDivElement>(null);
  const heights = useRef(new Map<string, number>());
  const width = useRef(0);
  const [measurement, setMeasurement] = useState(0);
  const [range, setRange] = useState({ start: 0, end: 30 });
  const active = enabled && segments.length > 100;
  const offsets = useMemo(() => {
    const result = [0];
    for (const segment of segments) result.push(result[result.length - 1] + (heights.current.get(segment.id) ?? ESTIMATED_HEIGHT));
    return result;
  }, [segments, measurement]);
  const start = active ? Math.min(range.start, Math.max(0, segments.length - 1)) : 0;
  const end = active ? Math.min(Math.max(range.end, start + 1), segments.length) : segments.length;

  useEffect(() => {
    const list = listRef.current;
    if (!active || !list) return;
    const parent = nearestOverflowParent(list);
    const scrollTarget = parent ?? window;
    const update = () => {
      const rect = list.getBoundingClientRect();
      const top = Math.max(0, (parent?.getBoundingClientRect().top ?? 0) - rect.top);
      const height = parent?.clientHeight || window.innerHeight || 800;
      const next = { start: rowAt(offsets, Math.max(0, top - OVERSCAN)),
        end: Math.min(segments.length, rowAt(offsets, top + height + OVERSCAN) + 1) };
      setRange(previous => previous.start === next.start && previous.end === next.end ? previous : next);
    };
    update();
    scrollTarget.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    return () => { scrollTarget.removeEventListener('scroll', update); window.removeEventListener('resize', update); };
  }, [active, offsets, segments.length]);

  useEffect(() => {
    if (!active || !targetId || !listRef.current) return;
    const index = segments.findIndex(segment => segment.id === targetId);
    if (index < 0) return;
    setRange({ start: Math.max(0, index - 10), end: Math.min(segments.length, index + 20) });
    const list = listRef.current;
    const parent = nearestOverflowParent(list);
    if (parent) {
      const base = list.getBoundingClientRect().top - parent.getBoundingClientRect().top + parent.scrollTop;
      const top = Math.max(0, base + offsets[index] - parent.clientHeight / 2);
      parent.scrollTo({ top });
    }
  }, [active, targetId, segments, offsets]);

  useEffect(() => {
    const list = listRef.current;
    if (!active || !list) return;
    const nodes = [...list.querySelectorAll<HTMLElement>('[data-transcript-id]')];
    const measure = () => {
      const parent = nearestOverflowParent(list);
      const visibleTop = Math.max(0, (parent?.getBoundingClientRect().top ?? 0) - list.getBoundingClientRect().top);
      const anchor = rowAt(offsets, visibleTop);
      const previousAnchor = offsets[anchor];
      const previousHeight = offsets[anchor + 1] - previousAnchor;
      const withinAnchor = visibleTop - previousAnchor;
      let changed = false;
      const nextWidth = list.getBoundingClientRect().width;
      if (nextWidth > 0 && width.current !== nextWidth) {
        if (width.current) heights.current.clear();
        width.current = nextWidth;
        changed = true;
      }
      for (const node of nodes) {
        const id = node.dataset.transcriptId!;
        const height = node.getBoundingClientRect().height;
        if (height > 0 && heights.current.get(id) !== height) { heights.current.set(id, height); changed = true; }
      }
      if (changed) {
        // Keep the same turn at the viewport edge as estimates become real heights.
        if (parent) {
          let nextAnchor = 0;
          for (let index = 0; index < anchor; index++) nextAnchor += heights.current.get(segments[index].id) ?? ESTIMATED_HEIGHT;
          const nextHeight = heights.current.get(segments[anchor]?.id) ?? ESTIMATED_HEIGHT;
          const withinAdjustment = previousHeight > 0 ? withinAnchor * (nextHeight / previousHeight - 1) : 0;
          parent.scrollTop += nextAnchor - previousAnchor + withinAdjustment;
        }
        setMeasurement(value => value + 1);
      }
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(list);
    nodes.forEach(node => observer.observe(node));
    return () => observer.disconnect();
  }, [active, start, end, segments, offsets]);

  return { listRef, start, end, top: offsets[start], bottom: offsets[segments.length] - offsets[end] };
}
