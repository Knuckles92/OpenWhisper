import FinalizationDiagnostics from './FinalizationDiagnostics';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { MeetingInfo, MeetingStateDoc, Op, Participant } from '../types';
import { ops } from '../types';
import type { SocketStatus } from '../ws';
import { enabledReportViews, type ReportViewId } from '../report';
import { initials, speakerColor } from '../people';
import ThemePicker from './ThemePicker';
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
  historySelectionId?: string | null;
  historyFocused?: boolean;
  onOpenHistoryMeeting?: () => void;
  onExitHistoryMeeting?: () => void;
  onToggleActivity: () => void;
  showActivity: boolean;
  transcriptLoadError: string | null;
  onRetryTranscript: () => void;
  transcriptComplete?: boolean;
  reportView?: ReportViewId;
  onReportViewChange?: (view: ReportViewId) => void;
  /** People in the room, for the avatar stack. */
  participants?: Participant[];
  onlineIds?: Set<string>;
  /** True while speech is being heard, so the level meter moves honestly. */
  listening?: boolean;
  /** Opens the full-screen room display (presenter view). */
  onOpenRoom?: () => void;
}

const MAX_AVATARS = 4;

function statusLabel(status: string): string {
  if (status === 'active') return 'Live';
  if (status === 'paused') return 'Paused';
  if (status === 'ending') return 'Ending';
  if (status === 'ended') return 'Ended';
  return status.replace(/_/g, ' ');
}

function captureLabel(capture: MeetingStateDoc['capture'] | undefined): string {
  if (!capture) return '';
  if (capture.mic_available && capture.loopback_available) return 'Mic + system audio';
  if (capture.mic_available) return 'Mic only';
  if (capture.loopback_available) return 'System audio only';
  return 'No audio input';
}

/** Wall-clock meeting time, minus pauses; frozen while paused or ended. */
function useElapsed(meeting: MeetingInfo | null, status: string): number | null {
  const [now, setNow] = useState(() => Date.now());
  const running = status === 'active';
  useEffect(() => {
    if (!running) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  // A finished meeting reports its own length; trust it over wall-clock math.
  const recorded = Number(meeting?.duration_s);
  if (!running && status === 'ended' && Number.isFinite(recorded) && recorded > 0) return recorded;
  const started = meeting?.started_at ? Date.parse(String(meeting.started_at)) : NaN;
  if (!Number.isFinite(started)) return null;
  const ended = meeting?.ended_at ? Date.parse(String(meeting.ended_at)) : NaN;
  const paused = Number(meeting?.paused_total_s) || 0;
  // `now` stops ticking once the meeting leaves "active", so a pause reads steady.
  const end = Number.isFinite(ended) ? ended : now;
  return Math.max(0, (end - started) / 1000 - paused);
}

/** m:ss under an hour, h:mm:ss beyond it. */
function clockTime(seconds: number): string {
  const whole = Math.floor(seconds);
  const h = Math.floor(whole / 3600);
  const m = Math.floor((whole % 3600) / 60);
  const s = String(whole % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

function ShareIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
      <path d="M5.5 8.5l3-3M6 3.5l1-1a2.5 2.5 0 013.5 3.5l-1 1M8 10.5l-1 1A2.5 2.5 0 013.5 8l1-1" strokeLinecap="round" />
    </svg>
  );
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
  historySelectionId = null,
  historyFocused = false,
  onOpenHistoryMeeting,
  onExitHistoryMeeting,
  onToggleActivity,
  showActivity,
  transcriptLoadError,
  onRetryTranscript,
  transcriptComplete = false,
  reportView,
  onReportViewChange,
  participants = [],
  onlineIds,
  listening = false,
  onOpenRoom,
}: HeaderBarProps) {
  const [titleDraft, setTitleDraft] = useState(state.title);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pendingConfirm, setPendingConfirm] = useState<PendingConfirm>(null);
  const moreRef = useRef<HTMLDetailsElement>(null);


  useEffect(() => {
    setTitleDraft(state.title);
  }, [state.title]);

  const meetingLive = state.status === 'active';
  const meetingEnding = state.status === 'ending';
  const sessionRunning = meetingLive || state.status === 'paused' || meetingEnding;
  const statusClass = meetingLive
    ? 'live'
    : state.status === 'paused' || meetingEnding
      ? 'paused'
      : 'ended';
  const elapsed = useElapsed(meeting, state.status);

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
    else if (!trimmed) setTitleDraft(state.title);
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

  const closeMore = () => moreRef.current?.removeAttribute('open');

  // The overflow menu is a <details>; close it on outside clicks and Escape.
  useEffect(() => {
    const onPointer = (event: PointerEvent) => {
      const menu = moreRef.current;
      if (menu?.open && !menu.contains(event.target as Node)) menu.removeAttribute('open');
    };
    const onKey = (event: KeyboardEvent) => {
      const menu = moreRef.current;
      if (event.key === 'Escape' && menu?.open) {
        menu.removeAttribute('open');
        menu.querySelector('summary')?.focus();
      }
    };
    document.addEventListener('pointerdown', onPointer);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, []);

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
  const brief = (state.intent?.text || '').trim();
  const reportsReady =
    state.status === 'ended' || state.finalization?.status === 'completed';
  const showReportActions = reportsReady && !showHistory;
  const reportViews = enabledReportViews(state);
  const activeReportView =
    reportView && reportViews.includes(reportView) ? reportView : reportViews[0];
  const shownPeople = participants.slice(0, MAX_AVATARS);
  const extraPeople = participants.length - shownPeople.length;
  const capture = captureLabel(state.capture);
  // Calm, one-line outcomes sit in the context strip instead of full-width banners.
  const contextNote = showHistory
    ? null
    : finalizationStatus === 'completed'
      ? finalizationMessage || 'Final insights are ready.'
      : finalizationStatus === 'disabled' && meetingEnded
        ? finalizationMessage || 'AI insights are off for this meeting.'
        : null;
  const showContext =
    socketStatus !== 'open' || (!showHistory && !state.cloud_enabled) || showReportActions
    || Boolean(contextNote);

  return (
    <header className="header-bar">
      <div className="meeting-masthead">
        <span className="cb-brand">OpenWhisper<span className="brand-period">.</span></span>
        <div className="masthead-tools">
          <span className="masthead-caption">A space for the conversation</span>
          <ThemePicker />
        </div>
      </div>
      <div className={`command-bar${showHistory ? ' history' : ''}`}>
        <div className="cb-left">
          {showHistory ? (
            <span className="cb-section">Meeting history</span>
          ) : (
            <div className={`cb-status ${statusClass}`} role="status" aria-live="polite">
              <span className="cb-dot" aria-hidden="true" />
              <span className="cb-status-label">{statusLabel(state.status)}</span>
              {elapsed !== null && (
                <span className="cb-timer" aria-label={`Elapsed ${clockTime(elapsed)}`}>
                  {clockTime(elapsed)}
                </span>
              )}
              {meetingLive && (
                <span
                  className={`cb-meter${listening ? ' hearing' : ''}`}
                  title={capture}
                  aria-label={listening ? 'Speech detected' : 'Listening'}
                >
                  {Array.from({ length: 7 }, (_, i) => <span key={i} />)}
                </span>
              )}
              {meetingLive && capture && <span className="cb-capture">{capture}</span>}
            </div>
          )}
        </div>

        {!showHistory && (
          <div className="cb-title">
            {isHost ? (
              <input
                className="cb-title-input"
                value={titleDraft}
                onChange={(e) => setTitleDraft(e.target.value)}
                onBlur={commitTitle}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
                  if (e.key === 'Escape') {
                    setTitleDraft(state.title);
                    (e.target as HTMLInputElement).blur();
                  }
                }}
                aria-label="Meeting title"
                placeholder="Meeting title"
                title="Click to rename"
              />
            ) : (
              <h1 className="cb-title-text">{meetingTitle}</h1>
            )}
            {brief && <span className="cb-brief" title={brief}>{brief}</span>}
          </div>
        )}

        <div className="cb-actions">
          {!showHistory && participants.length > 0 && (
            <div className="cb-people" aria-label={`${participants.length} in the room`}>
              {shownPeople.map((person) => (
                <span
                  key={person.id}
                  className={`cb-avatar${onlineIds && !onlineIds.has(person.id) ? ' away' : ''}`}
                  style={{ background: speakerColor(person.id, participants) }}
                  title={person.display_name}
                  aria-hidden="true"
                >
                  {initials(person.display_name)}
                </span>
              ))}
              {extraPeople > 0 && <span className="cb-avatar more" aria-hidden="true">+{extraPeople}</span>}
            </div>
          )}

          {isHost && !showHistory && (
            <>
              {fullGuestUrl && (
                <>
                  <button type="button" className="cb-button cb-share" onClick={copyGuestLink}>
                    <ShareIcon />
                    <span className="cb-label">{copied ? 'Copied' : 'Copy guest link'}</span>
                  </button>
                  <span className="sr-only" role="status" aria-live="polite">
                    {copied ? 'Guest link copied.' : ''}
                  </span>
                </>
              )}
              <button type="button" className="cb-button" onClick={onToggleHistory}>
                History
              </button>
              {meetingLive && (
                <button
                  type="button"
                  className="cb-button icon"
                  disabled={busy}
                  aria-label="Pause"
                  title="Pause capture"
                  onClick={() => hostAction(() => api.pauseMeeting(token))}
                >
                  <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                    <rect x="2" y="1.5" width="3" height="9" rx="1" fill="currentColor" />
                    <rect x="7" y="1.5" width="3" height="9" rx="1" fill="currentColor" />
                  </svg>
                </button>
              )}
              {state.status === 'paused' && (
                <button
                  type="button"
                  className="cb-button"
                  disabled={busy}
                  onClick={() => hostAction(() => api.resumeMeeting(token))}
                >
                  <svg width="11" height="11" viewBox="0 0 12 12" aria-hidden="true">
                    <path d="M3 1.5l7.5 4.5L3 10.5z" fill="currentColor" />
                  </svg>
                  Resume
                </button>
              )}
              {!meetingEnded && !meetingEnding && (
                <button
                  type="button"
                  className="cb-button end"
                  disabled={busy}
                  onClick={() => setPendingConfirm('end')}
                >
                  End
                </button>
              )}
              <details ref={moreRef} className="cb-more">
                <summary className="cb-button icon" aria-label="More meeting options">
                  <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
                    <circle cx="3" cy="7" r="1.3" fill="currentColor" />
                    <circle cx="7" cy="7" r="1.3" fill="currentColor" />
                    <circle cx="11" cy="7" r="1.3" fill="currentColor" />
                  </svg>
                </summary>
                <div className="cb-menu">
                  <label className="cb-menu-item cb-toggle">
                    <span>
                      <strong>AI insights</strong>
                      <small>Live notes, topics and highlights</small>
                    </span>
                    <input
                      type="checkbox"
                      role="switch"
                      checked={state.cloud_enabled}
                      onChange={(e) => {
                        // REST only: the engine applies the state op itself and
                        // starts/stops the intelligence layer. Sending the op over
                        // the socket too would double-apply and double-audit it.
                        hostAction(() => api.setCloud(token, e.target.checked));
                      }}
                    />
                  </label>
                  {onOpenRoom && (
                    <button
                      type="button"
                      className="cb-menu-item"
                      onClick={() => {
                        closeMore();
                        onOpenRoom();
                      }}
                    >
                      <span className="cb-menu-copy">
                        <strong>Room display</strong>
                        <small>Big-type view for a meeting-room screen</small>
                      </span>
                    </button>
                  )}
                  {!showActivity && (
                    <button
                      type="button"
                      className="cb-menu-item"
                      onClick={() => {
                        closeMore();
                        onToggleActivity();
                      }}
                    >
                      Show Pi activity
                    </button>
                  )}
                  <button
                    type="button"
                    className="cb-menu-item danger"
                    onClick={() => {
                      closeMore();
                      setPendingConfirm('regenerate');
                    }}
                  >
                    Regenerate links
                  </button>
                </div>
              </details>
            </>
          )}
          {!isHost && !showHistory && onOpenRoom && (
            <button
              type="button"
              className="cb-button icon"
              aria-label="Room display"
              title="Room display"
              onClick={onOpenRoom}
            >
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
                <rect x="1.75" y="2.5" width="12.5" height="8.5" rx="1.5" />
                <path d="M5.5 14h5M8 11v3" strokeLinecap="round" />
              </svg>
            </button>
          )}
          {isHost && showHistory && (
            <>
              {historyFocused ? (
                <button type="button" className="cb-button" onClick={onExitHistoryMeeting}>
                  All meetings
                </button>
              ) : historySelectionId ? (
                <button type="button" className="cb-button primary" onClick={onOpenHistoryMeeting}>
                  Open meeting
                </button>
              ) : null}
              {/* Leaving history returns to this dashboard's own meeting, which is
                  only worth a header slot while that meeting is still running or
                  while no past meeting is open. The pane's Close always exits. */}
              {(sessionRunning || (!historyFocused && !historySelectionId)) && (
                <button type="button" className="cb-button primary" onClick={onToggleHistory}>
                  {sessionRunning ? 'Back to live' : 'Back to meeting'}
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {showContext && (
        <div className="cb-context">
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
          {contextNote && (
            <span className={`cb-note${finalizationStatus === 'completed' ? ' done' : ''}`} role="status">
              {contextNote}
            </span>
          )}
          {showReportActions && (
            <div className="cb-report-actions">
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
            </div>
          )}
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
            finalizationMessage || 'Preparing final insights…'
          )}
        </div>
      )}

      {!showHistory && showFinalizationWarning && (
        <div className="banner warning" role="status">
          {finalizationMessage ||
            (finalizationStatus === 'failed'
              ? 'Final insights failed.'
              : 'Final insights could not run.')}
          {isHost && <FinalizationDiagnostics finalization={finalization} meetingId={state.meeting_id} />}
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

      {transcriptLoadError && (
        <div className="banner warning" role="alert">
          {transcriptLoadError}
          <button type="button" className="ghost" style={{ marginLeft: 8 }} onClick={onRetryTranscript}>
            Retry transcript
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
