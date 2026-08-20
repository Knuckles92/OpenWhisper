import { useEffect, useMemo, useState } from 'react';
import {
  enabledReportViews,
  resolveReportView,
  segmentMap,
  writeStoredReportView,
  type ReportViewId,
} from '../../report';
import { EvidenceProvider } from '../../evidence';
import type { MeetingInfo, MeetingStateDoc, Segment } from '../../types';
import BriefReport from './BriefReport';
import FullMeetingDocument from './FullMeetingDocument';
import ReportDownload from './ReportDownload';
import ReportViewSelect from './ReportViewSelect';
import RibbonReport from './RibbonReport';
import SignalReport from './SignalReport';

interface ReportTabsProps {
  state: MeetingStateDoc;
  segments: Segment[];
  meeting?: MeetingInfo | null;
  onEvidenceClick?: (segmentId: string) => void;
  onSeek?: (seconds: number) => void;
  /** When false, Full download stays disabled so a partial transcript is never printed. */
  transcriptComplete?: boolean;
  /** Hide the in-toolbar download when the header already owns it. */
  showDownload?: boolean;
  /** Hide the in-page switcher when the header already owns it. */
  showSwitcher?: boolean;
  activeView?: ReportViewId;
  onViewChange?: (view: ReportViewId) => void;
}

export default function ReportTabs({
  state,
  segments,
  meeting,
  onEvidenceClick,
  onSeek,
  transcriptComplete = false,
  showDownload = true,
  showSwitcher = true,
  activeView,
  onViewChange,
}: ReportTabsProps) {
  const views = enabledReportViews(state);
  const segs = useMemo(() => segmentMap(segments), [segments]);
  const [internalView, setInternalView] = useState<ReportViewId>(() =>
    activeView && views.includes(activeView) ? activeView : resolveReportView(views),
  );
  const active = activeView && views.includes(activeView) ? activeView : internalView;

  useEffect(() => {
    if (!views.includes(internalView)) {
      setInternalView(resolveReportView(views));
    }
  }, [internalView, views]);

  const select = (view: ReportViewId) => {
    if (onViewChange) onViewChange(view);
    else {
      setInternalView(view);
      writeStoredReportView(view);
    }
  };

  const shared = { state, segments, segs, meeting, onEvidenceClick, onSeek };
  const showToolbar = showSwitcher || showDownload;
  const participants = useMemo(() => Object.values(state.participants), [state.participants]);

  return (
    <EvidenceProvider segments={segments} participants={participants}>
      <section className="report-stage">
        {showToolbar && (
          <div className="report-toolbar">
            {showSwitcher && (
              <ReportViewSelect views={views} active={active} onSelect={select} />
            )}
            {showDownload && (
              <ReportDownload
                state={state}
                meeting={meeting}
                transcriptComplete={transcriptComplete}
                activeView={active}
              />
            )}
          </div>
        )}
        <div className="report-sheet">
          {active === 'ribbon' && <RibbonReport {...shared} />}
          {active === 'brief' && <BriefReport {...shared} />}
          {active === 'signal' && <SignalReport {...shared} />}
        </div>
        <FullMeetingDocument state={state} segments={segments} />
      </section>
    </EvidenceProvider>
  );
}
