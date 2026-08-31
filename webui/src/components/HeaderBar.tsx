import { useEffect, useState } from 'react';
import { api } from '../api';
import type { MeetingInfo, MeetingStateDoc, Op } from '../types';
import { ops } from '../types';
import type { SocketStatus } from '../ws';
import { enabledReportViews, type ReportViewId } from '../report';
import ConfirmDialog from './ConfirmDialog';
import ReportDownload from './report/ReportDownload';
import ReportViewSelect from './report/ReportViewSelect';

type PendingConfirm = 'end' | 'regenerate' | null;

interface HeaderBarProps {
  token: string;
  isHost: boolean;
  state: MeetingStateDoc;
  meeting: MeetingInfo | null;
  guestUrl: string | null;
  socketStatus: SocketStatus;
  meetingEnded: boolean;
  lastError: string | null;
  onSendOp: (op: Op) => Promise<boolean>;
  onClientError: (message: string) => void;
  onClearError: () => void;
  onToggleHistory: () => void;
  showHistory: boolean;
  onToggleActivity: () => void;
  showActivity: boolean;
  transcriptComplete?: boolean;
  reportView?: ReportViewId;
  onReportViewChange?: (view: ReportViewId) => void;
}

export default function HeaderBar({
  token,
  isHost,
  state,
  meeting,
  guestUrl,
  socketStatus,
  meetingEnded,
  lastError,
  onSendOp,
  onClientError,
  onClearError,
  onToggleHistory,
  showHistory,
  onToggleActivity,
  showActivity,
  transcriptComplete = false,
  reportView,
  onReportViewChange,
}: HeaderBarProps) {
  const [titleDraft, setTitleDraft] = useState(state.title);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm>(null);

  useEffect(() => {
    setTitleDraft(state.title);
  }, [state.title]);

  const meetingLive = state.status === 'active';
  const meetingEnding = state.status === 'ending';
  const statusClass = meetingLive
    ? 'live'
    : state.status === 'paused'
      ? 'paused'
      : meetingEnding
        ? 'paused'
        : 'ended';

  const fullGuestUrl = guestUrl
    ? guestUrl.startsWith('http')
      ? guestUrl
      : `${location.origin}${guestUrl.startsWith('/') ? '' : '/'}${guestUrl}`
    : null;

  const copyGuestLink = async () => {
    if (!fullGuestUrl) return;
    try {
      await navigator.clipboard.writeText(fullGuestUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      onClientError(
        err instanceof Error ? err.message : 'The guest link could not be copied.',
      );
    }
  };

  const commitTitle = () => {
    const trimmed = titleDraft.trim();
    if (trimmed && trimmed !== state.title) void onSendOp(ops.setTitle(trimmed));
  };

  const hostAction = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch (err) {
      onClientError(err instanceof Error ? err.message : 'The request could not be completed.');
    } finally {
      setBusy(false);
    }
  };

  const finalization = state.finalization ?? null;
  const finalizationStatus = finalization?.status ?? null;
  const finalizationMessage = (finalization?.message || '').trim();
  const showOfflineBanner =
    state.cloud_enabled &&
    !state.intelligence_online &&
    (meetingLive || meetingEnding) &&
    finalizationStatus !== 'running';
  const showFinalizationWarning =
    (finalizationStatus === 'unavailable' || finalizationStatus === 'failed') &&
    !showOfflineBanner;
  const showDiarizationBanner =
    !state.diarization_available && (meetingLive || meetingEnding);
  const meetingTitle = state.title || meeting?.title || 'Meeting';
  const reportsReady =
    state.status === 'ended' || state.finalization?.status === 'completed';
  const showReportActions = reportsReady && !showHistory;
  const reportViews = enabledReportViews(state);
  const activeReportView =
    reportView && reportViews.includes(reportView) ? reportView : reportViews[0];

  return (
    <header className="header-bar">
      <div className="header-brand">
        OpenWhisper <em>Meeting</em>
        {showHistory && <span className="history-badge">Past Meetings</span>}
      </div>

      <div className="header-actions">
        {socketStatus !== 'open' && (
          <span
            className={`socket-indicator ${socketStatus}`}
            role="status"
            aria-live="polite"
          >
            {socketStatus === 'connecting' ? 'Reconnecting…' : 'Offline'}
          </span>
        )}
        {!showHistory && !state.cloud_enabled && (
          <span className="status-chip">Transcript only</span>
        )}
        {showReportActions && (
          <>
            <ReportViewSelect
              views={reportViews}
              active={activeReportView}
              onSelect={(view) => onReportViewChange?.(view)}
            />
            <ReportDownload
              state={state}
              meeting={meeting}
              transcriptComplete={transcriptComplete}
              activeView={activeReportView}
            />
          </>
        )}
        {isHost && !showHistory && (
          <>
            {fullGuestUrl && (
              <>
                <button type="button" className="primary" onClick={copyGuestLink}>
                  {copied ? 'Copied' : 'Copy guest link'}
                </button>
                <span className="sr-only" role="status" aria-live="polite">
                  {copied ? 'Guest link copied.' : ''}
                </span>
              </>
            )}
            <button type="button" className="ghost" onClick={onToggleHistory}>
              History
            </button>
            {!showActivity && (
              <button type="button" className="ghost" onClick={onToggleActivity}>
                Show Pi activity
              </button>
            )}
            {meetingLive && (
              <button type="button" disabled={busy} onClick={() => hostAction(() => api.pauseMeeting(token))}>
                Pause
              </button>
            )}
            {state.status === 'paused' && (
              <button type="button" disabled={busy} onClick={() => hostAction(() => api.resumeMeeting(token))}>
                Resume
              </button>
            )}
            {meetingEnding && <span className="status-chip">Ending…</span>}
            {!meetingEnded && !meetingEnding && (
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={() => setPendingConfirm('end')}
              >
                End
              </button>
            )}
            <label className="cloud-toggle">
              <input
                type="checkbox"
                checked={state.cloud_enabled}
                onChange={(e) => {
                  // REST only: the engine applies the state op itself and
                  // starts/stops the intelligence layer. Sending the op over
                  // the socket too would double-apply and double-audit it.
                  hostAction(() => api.setCloud(token, e.target.checked));
                }}
              />
              Cloud insights
            </label>
            <button
              type="button"
              className="ghost"
              onClick={() => setPendingConfirm('regenerate')}
            >
              Regenerate links
            </button>
          </>
        )}
        {isHost && showHistory && (
          <button type="button" className="primary" onClick={onToggleHistory}>
            {meetingLive || state.status === 'paused' || meetingEnding
              ? 'Back to live'
              : 'Back to meeting'}
          </button>
        )}
      </div>

      {isHost && !showHistory && (
        <div className="header-title-edit">
          <input
            value={titleDraft}
            onChange={(e) => setTitleDraft(e.target.value)}
            onBlur={commitTitle}
            onKeyDown={(e) => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
            }}
            aria-label="Meeting title"
            placeholder="Meeting title"
          />
          <span className={`status-chip ${statusClass}`}>
            <span className="status-dot" />
            {meetingTitle}
          </span>
        </div>
      )}

      {!showHistory && showOfflineBanner && (
        <div className="banner warning" role="status">
          Intelligence offline — transcript continues; insights paused.
          {(finalizationStatus === 'unavailable' || finalizationStatus === 'failed') &&
            finalizationMessage && (
              <small style={{ display: 'block', opacity: 0.85, marginTop: 2 }}>
                {finalizationMessage}
              </small>
            )}
        </div>
      )}

      {!showHistory && showDiarizationBanner && (
        <div className="banner info" role="status">
          Speaker diarization unavailable — using Me / Others channels.
        </div>
      )}

      {!showHistory && meetingEnding && (
        <div className="banner info" role="status">
          Ending meeting — finishing transcription…
        </div>
      )}

      {!showHistory && finalizationStatus === 'running' && (
        <div className="banner info" role="status">
          {finalization?.total_steps && finalization?.current_step ? (
            <span>
              <strong>Step {finalization.current_step}/{finalization.total_steps}:</strong>{' '}
              {finalizationMessage || 'Finalizing meeting…'}
              {finalization.step_details && finalization.step_details !== finalizationMessage && (
                <small style={{ display: 'block', opacity: 0.85, marginTop: 2 }}>
                  {finalization.step_details}
                </small>
              )}
            </span>
          ) : (
            finalizationMessage || 'Preparing final cloud insights…'
          )}
        </div>
      )}

      {!showHistory && finalizationStatus === 'completed' && (
        <div className="banner info" role="status">
          {finalizationMessage || 'Final cloud insights are ready.'}
        </div>
      )}

      {!showHistory && finalizationStatus === 'disabled' && meetingEnded && (
        <div className="banner info" role="status">
          {finalizationMessage || 'Cloud intelligence is off for this meeting.'}
        </div>
      )}

      {!showHistory && showFinalizationWarning && (
        <div className="banner warning" role="status">
          {finalizationMessage ||
            (finalizationStatus === 'failed'
              ? 'Final cloud insights failed.'
              : 'Final cloud insights could not run.')}
        </div>
      )}

      {!showHistory && meetingLive && state.capture?.message && (
        <div className="banner warning" role="status">
          {state.capture.message}
        </div>
      )}

      {lastError && (
        <div className="banner warning" role="alert">
          {lastError}
          <button type="button" className="ghost" style={{ marginLeft: 8 }} onClick={onClearError}>
            Dismiss
          </button>
        </div>
      )}

      <ConfirmDialog
        open={pendingConfirm === 'end'}
        title="End this meeting?"
        message="Live capture will stop. Transcription will finish in the background, and notes and reports stay available."
        confirmLabel="End meeting"
        cancelLabel="Keep meeting"
        danger
        busy={busy}
        onCancel={() => setPendingConfirm(null)}
        onConfirm={() => {
          setPendingConfirm(null);
          hostAction(() => api.endMeeting(token));
        }}
      />
      <ConfirmDialog
        open={pendingConfirm === 'regenerate'}
        title="Regenerate links?"
        message="Everyone currently connected will be disconnected and will need the new links to rejoin."
        confirmLabel="Regenerate links"
        cancelLabel="Keep current links"
        danger
        busy={busy}
        onCancel={() => setPendingConfirm(null)}
        onConfirm={() => {
          setPendingConfirm(null);
          hostAction(async () => {
            const result = await api.regenerateTokens(token);
            window.location.replace(result.host_url);
          });
        }}
      />
    </header>
  );
}
