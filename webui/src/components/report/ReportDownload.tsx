import { useRef } from 'react';
import { printDocumentTitle, printMeeting } from '../../print';
import { enabledReportViews, REPORT_VIEW_META, resolveReportView, type ReportViewId } from '../../report';
import type { MeetingInfo, MeetingStateDoc } from '../../types';

interface ReportDownloadProps {
  state: MeetingStateDoc;
  meeting?: MeetingInfo | null;
  /** When false, Full download stays disabled so a partial transcript is never printed. */
  transcriptComplete?: boolean;
  activeView?: ReportViewId;
}

export default function ReportDownload({
  state,
  meeting,
  transcriptComplete = false,
  activeView,
}: ReportDownloadProps) {
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const views = enabledReportViews(state);
  const active = activeView && views.includes(activeView) ? activeView : resolveReportView(views);
  const meetingTitle = state.title || meeting?.display_title || meeting?.title || 'Meeting';

  const download = (scope: 'summary' | 'full') => {
    detailsRef.current?.removeAttribute('open');
    printMeeting(
      scope,
      printDocumentTitle(String(meetingTitle), scope, REPORT_VIEW_META[active]?.label),
    );
  };

  return (
    <details ref={detailsRef} className="report-download">
      <summary aria-label="Download meeting report">Download</summary>
      <div className="report-download-menu">
        <button type="button" onClick={() => download('summary')}>
          Summary — {REPORT_VIEW_META[active]?.label}
        </button>
        <button
          type="button"
          disabled={!transcriptComplete}
          title={
            transcriptComplete
              ? 'Report plus notes, captured items, questions, people, and the full transcript'
              : 'Waiting for the full transcript to load'
          }
          onClick={() => download('full')}
        >
          {transcriptComplete ? 'Full meeting' : 'Full meeting (loading transcript…)'}
        </button>
      </div>
    </details>
  );
}
