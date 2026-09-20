import { useState } from 'react';
import { api, ApiError } from '../../api';
import { stripLeadingTitle } from '../../markdown';
import type { CustomReport, MeetingStateDoc } from '../../types';
import MarkdownView from './MarkdownView';

/** Longest request the server accepts, mirroring MAX_REQUEST_CHARS. */
const MAX_REQUEST_CHARS = 2000;

/** Starting points, not templates: each is a different *shape* of document. */
const EXAMPLES = [
  'A one-page brief for someone who missed the call',
  'Every commitment we made, who owns it, and by when',
  'A table of the objections raised and how we answered each',
  'What changed since the last time we discussed this',
];

interface CustomReportsProps {
  state: MeetingStateDoc;
  token: string;
  meetingId: string;
  /** Called with the fresh state after a request or delete (non-live views). */
  onState?: (state: MeetingStateDoc) => void;
  /** Print and archive views show the reports without the composer. */
  readOnly?: boolean;
}

function reportHeading(report: CustomReport): string {
  const title = report.title.trim();
  if (title) return title;
  const request = report.request.trim();
  return request.length > 80 ? `${request.slice(0, 79)}…` : request || 'Report';
}

function sourceLine(report: CustomReport): string {
  const { sources } = report;
  const parts: string[] = [];
  if (typeof sources?.transcript_lines === 'number') {
    parts.push(`${sources.transcript_lines} transcript lines`);
  }
  if (sources?.past_meetings) parts.push('past meetings');
  if (sources?.knowledge_folder) parts.push('knowledge folder');
  if (sources?.model) parts.push(sources.model);
  return parts.join(' · ');
}

function ReportCard({
  report,
  token,
  meetingId,
  onDelete,
  readOnly,
}: {
  report: CustomReport;
  token: string;
  meetingId: string;
  onDelete: (report: CustomReport) => Promise<void>;
  readOnly: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const running = report.status === 'running';
  const heading = reportHeading(report);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(report.markdown);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access can be denied; the download stays available.
    }
  };

  return (
    <article className={`custom-report custom-report-${report.status}`}>
      <header className="custom-report-header">
        <div>
          <h4>{heading}</h4>
          <p className="custom-report-ask">“{report.request}”</p>
        </div>
        {!readOnly && (
          <div className="custom-report-actions no-print">
            {report.status === 'ready' && (
              <>
                <button type="button" onClick={() => void copy()}>
                  {copied ? 'Copied' : 'Copy'}
                </button>
                <a
                  className="button-link"
                  href={api.reportDownloadUrl(token, meetingId, report.id)}
                  download
                >
                  Download
                </a>
              </>
            )}
            <button
              type="button"
              className="danger"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await onDelete(report);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {running ? 'Stop' : 'Delete'}
            </button>
          </div>
        )}
      </header>

      {running && (
        <p className="custom-report-status" role="status" aria-live="polite">
          {report.message || 'Reading the meeting and writing your report…'}
        </p>
      )}
      {report.status === 'failed' && (
        <p className="custom-report-status warning" role="status">
          {report.message || 'The report could not be written.'}
        </p>
      )}
      {report.status === 'ready' && (
        <>
          <MarkdownView source={stripLeadingTitle(report.markdown, heading)} />
          {sourceLine(report) && (
            <p className="custom-report-sources">Written from {sourceLine(report)}.</p>
          )}
        </>
      )}
    </article>
  );
}

/**
 * Ask for a report in your own words, and read what came back.
 *
 * The composer is host-only and disabled while a report is in flight: one
 * agent reads the corpus at a time, which is also what the server enforces.
 */
export default function CustomReports({
  state,
  token,
  meetingId,
  onState,
  readOnly = false,
}: CustomReportsProps) {
  const all = state.custom_reports ?? [];
  // The printed document carries finished reports only: a failure banner or
  // a spinner is dashboard state, not part of what someone circulates.
  const reports = readOnly ? all.filter((report) => report.status === 'ready') : all;
  const running = all.some((report) => report.status === 'running');
  const [draft, setDraft] = useState('');
  const [pending, setPending] = useState(false);
  const [error, setError] = useState('');

  if (readOnly && !reports.length) return null;

  const submit = async () => {
    const text = draft.trim();
    if (!text || pending || running) return;
    setPending(true);
    setError('');
    try {
      const result = await api.requestReport(token, meetingId, text);
      onState?.(result.state);
      setDraft('');
    } catch (err) {
      setError(
        err instanceof ApiError || err instanceof Error
          ? err.message
          : 'The report could not be started.',
      );
    } finally {
      setPending(false);
    }
  };

  const remove = async (report: CustomReport) => {
    setError('');
    try {
      const result = await api.deleteReport(token, meetingId, report.id);
      onState?.(result.state);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The report could not be deleted.');
    }
  };

  return (
    // The interactive panel never prints: the print-only full document
    // carries its own read-only copy, so a Full download has exactly one.
    <section
      className={`panel custom-reports${readOnly ? '' : ' no-print'}`}
      aria-labelledby="custom-reports-heading"
    >
      <div className="panel-header">
        <span id="custom-reports-heading">
          {readOnly ? 'Requested Reports' : 'Ask for a report'}
        </span>
        {reports.length > 0 && (
          <span className="status-chip">{reports.length}</span>
        )}
      </div>
      <div className="panel-body">
        {!readOnly && (
          <div className="custom-report-composer no-print">
            <p>
              Describe the report you want. The agent reads this meeting’s whole
              record — transcript, notes, decisions, action items, questions —
              and writes that document.
            </p>
            <textarea
              aria-label="Describe the report you want"
              placeholder="What should this report cover, and who is it for?"
              value={draft}
              maxLength={MAX_REQUEST_CHARS}
              disabled={pending || running}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
                  event.preventDefault();
                  void submit();
                }
              }}
            />
            <div className="custom-report-examples">
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  className="ghost"
                  disabled={pending || running}
                  onClick={() => setDraft(example)}
                >
                  {example}
                </button>
              ))}
            </div>
            <button
              type="button"
              className="primary"
              disabled={pending || running || !draft.trim()}
              onClick={() => void submit()}
            >
              {pending ? 'Starting…' : running ? 'A report is being written…' : 'Write this report'}
            </button>
            <p role="status" aria-live="polite" className="custom-report-hint">
              {error
                || (running
                  ? 'One report is written at a time. This one will appear below.'
                  : 'Reports are written by the meeting’s cloud text model and saved with the meeting.')}
            </p>
          </div>
        )}

        {reports.length === 0 ? (
          !readOnly && (
            <p className="empty-state">No reports yet for this meeting.</p>
          )
        ) : (
          <div className="custom-report-list">
            {[...reports].reverse().map((report) => (
              <ReportCard
                key={report.id}
                report={report}
                token={token}
                meetingId={meetingId}
                onDelete={remove}
                readOnly={readOnly}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
