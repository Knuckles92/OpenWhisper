import { useEffect, useMemo, useState } from 'react';
import { enabledReportViews, segmentMap, type ReportViewId } from '../../report';
import type { MeetingInfo, MeetingStateDoc, Segment } from '../../types';
import BriefReport from './BriefReport';
import RibbonReport from './RibbonReport';
import SignalReport from './SignalReport';

const STORAGE_KEY = 'ow_report_view';

const TAB_META: Record<ReportViewId, { label: string; note: string }> = {
  ribbon: {
    label: 'Ribbon',
    note: 'The meeting against its own clock. Walk it in order; the minimap shows where the conversation actually happened.',
  },
  brief: {
    label: 'Brief',
    note: 'One editorial page, one column, no boxes. Prints and pastes cleanly.',
  },
  signal: {
    label: 'Signal',
    note: 'One screen, plus the clips worth hearing. Best for forty seconds before the next call.',
  },
};

function readStoredView(): ReportViewId | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === 'ribbon' || raw === 'brief' || raw === 'signal') return raw;
  } catch {
    /* private mode / blocked storage */
  }
  return null;
}

function writeStoredView(view: ReportViewId): void {
  try {
    localStorage.setItem(STORAGE_KEY, view);
  } catch {
    /* ignore */
  }
}

interface ReportTabsProps {
  state: MeetingStateDoc;
  segments: Segment[];
  meeting?: MeetingInfo | null;
  onEvidenceClick?: (segmentId: string) => void;
  onSeek?: (seconds: number) => void;
}

export default function ReportTabs({
  state,
  segments,
  meeting,
  onEvidenceClick,
  onSeek,
}: ReportTabsProps) {
  const views = enabledReportViews(state);
  const segs = useMemo(() => segmentMap(segments), [segments]);
  const [active, setActive] = useState<ReportViewId>(() => {
    const stored = readStoredView();
    if (stored && views.includes(stored)) return stored;
    return views.includes('ribbon') ? 'ribbon' : views[0];
  });

  useEffect(() => {
    if (!views.includes(active)) {
      const next = views.includes('ribbon') ? 'ribbon' : views[0];
      setActive(next);
    }
  }, [active, views]);

  const select = (view: ReportViewId) => {
    setActive(view);
    writeStoredView(view);
  };

  const shared = { state, segments, segs, meeting, onEvidenceClick, onSeek };

  return (
    <section className="report-stage">
      <div className="report-tabs tab-row">
        {views.map((view) => (
          <button
            key={view}
            type="button"
            className={`tab${active === view ? ' active' : ''}`}
            onClick={() => select(view)}
          >
            {TAB_META[view].label}
          </button>
        ))}
      </div>
      <p className="report-tab-note">{TAB_META[active]?.note}</p>
      <div className="report-sheet">
        {active === 'ribbon' && <RibbonReport {...shared} />}
        {active === 'brief' && <BriefReport {...shared} />}
        {active === 'signal' && <SignalReport {...shared} />}
      </div>
    </section>
  );
}
