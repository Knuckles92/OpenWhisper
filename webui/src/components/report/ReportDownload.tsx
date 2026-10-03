import { useEffect, useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { printDocumentTitle, printMeeting } from '../../print';
import { enabledReportViews, REPORT_VIEW_META, resolveReportView, type ReportViewId } from '../../report';
import type { MeetingInfo, MeetingStateDoc } from '../../types';

interface ReportDownloadProps {
  state: MeetingStateDoc;
  meeting?: MeetingInfo | null;
  /** Full printing waits for complete data; onPrepareFull can load remaining pages. */
  transcriptComplete?: boolean;
  onPrepareFull?: () => Promise<void>;
  activeView?: ReportViewId;
}

export default function ReportDownload({
  state,
  meeting,
  transcriptComplete = false,
  onPrepareFull,
  activeView,
}: ReportDownloadProps) {
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const [preparing, setPreparing] = useState(false);
  const preparingRef = useRef(false);
  const preparation = useRef(0);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  const meetingId = useRef(state.meeting_id);
  meetingId.current = state.meeting_id;
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    preparation.current++; preparingRef.current = false; setPreparing(false); setError(null);
  }, [state.meeting_id]);
  const views = enabledReportViews(state);
  const active = activeView && views.includes(activeView) ? activeView : resolveReportView(views);
  const meetingTitle = state.title || meeting?.display_title || meeting?.title || 'Meeting';

  const download = async (scope: 'summary' | 'full') => {
    if (preparingRef.current) return;
    preparingRef.current = true;
    const requestedMeeting = state.meeting_id;
    const operation = ++preparation.current;
    const current = () => mounted.current && meetingId.current === requestedMeeting && preparation.current === operation;
    setError(null);
    try {
      if (scope === 'full' && !transcriptComplete && onPrepareFull) {
        setPreparing(true);
        await onPrepareFull();
        if (!current()) return;
        flushSync(() => setPreparing(false));
      }
      if (!current()) return;
      detailsRef.current?.removeAttribute('open');
      printMeeting(scope, printDocumentTitle(String(meetingTitle), scope, REPORT_VIEW_META[active]?.label));
    } catch (err) {
      if (current() && !(err instanceof Error && err.name === 'AbortError')) {
        setError(err instanceof Error ? err.message : 'Could not prepare the full meeting. Retry.');
      }
    } finally {
      if (preparation.current === operation) preparingRef.current = false;
      if (current()) setPreparing(false);
    }
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
          disabled={preparing || (!transcriptComplete && !onPrepareFull)}
          title={
            transcriptComplete
              ? 'Report plus notes, captured items, questions, people, and the full transcript'
              : onPrepareFull ? 'Load the complete transcript and print the full meeting' : 'Waiting for the full transcript to load'
          }
          onClick={() => download('full')}
        >
          {preparing ? 'Preparing full meeting…' : 'Full meeting'}
        </button>
        {error && <p role="alert">{error}</p>}
      </div>
    </details>
  );
}
