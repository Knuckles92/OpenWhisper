import { useEffect, useState } from 'react';
import { api } from '../api';
import type { MeetingInfo, MeetingStateDoc, Op } from '../types';
import { ops } from '../types';
import type { SocketStatus } from '../ws';

interface HeaderBarProps {
  token: string;
  isHost: boolean;
  state: MeetingStateDoc;
  meeting: MeetingInfo | null;
  guestUrl: string | null;
  socketStatus: SocketStatus;
  meetingEnded: boolean;
  lastError: string | null;
  onSendOp: (op: Op) => void;
  onClearError: () => void;
  onToggleHistory: () => void;
  showHistory: boolean;
  onToggleActivity: () => void;
  showActivity: boolean;
}

function formatStatus(status: string): string {
  return status.replace(/_/g, ' ');
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
  onClearError,
  onToggleHistory,
  showHistory,
  onToggleActivity,
  showActivity,
}: HeaderBarProps) {
  const [titleDraft, setTitleDraft] = useState(state.title);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setTitleDraft(state.title);
  }, [state.title]);

  const statusClass =
    state.status === 'active' ? 'live' : state.status === 'paused' ? 'paused' : 'ended';

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
    } catch {
      /* clipboard unavailable */
    }
  };

  const commitTitle = () => {
    const trimmed = titleDraft.trim();
    if (trimmed && trimmed !== state.title) onSendOp(ops.setTitle(trimmed));
  };

  const hostAction = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
    } catch {
      /* surfaced via WS error or banner */
    } finally {
      setBusy(false);
    }
  };

  const showOfflineBanner = !state.intelligence_online && state.status === 'active';
  const showDiarizationBanner = !state.diarization_available && state.status === 'active';

  return (
    <header className="header-bar">
      <div className="header-title">
        {isHost ? (
          <input
            value={titleDraft}
            onChange={(e) => setTitleDraft(e.target.value)}
            onBlur={commitTitle}
            onKeyDown={(e) => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur();
            }}
            aria-label="Meeting title"
          />
        ) : (
          <h1 style={{ margin: 0 }}>{state.title || meeting?.title || 'Meeting'}</h1>
        )}
      </div>

      <div className="header-status">
        <span className={`status-chip ${statusClass}`}>
          <span className="status-dot" />
          {formatStatus(state.status)}
        </span>
        <span className={`socket-indicator ${socketStatus}`}>
          {socketStatus === 'open' ? 'Connected' : socketStatus === 'connecting' ? 'Reconnecting…' : 'Offline'}
        </span>
        {!state.cloud_enabled && (
          <span className="status-chip">Transcript only</span>
        )}
      </div>

      <div className="header-actions">
        {isHost && (
          <>
            <button type="button" className={showHistory ? 'primary' : 'ghost'} onClick={onToggleHistory}>
              {showHistory ? 'Live view' : 'History'}
            </button>
            <button type="button" className={showActivity ? 'primary' : 'ghost'} onClick={onToggleActivity}>
              Activity
            </button>
            {state.status === 'active' && (
              <button
                type="button"
                disabled={busy}
                onClick={() => hostAction(() => api.pauseMeeting(token))}
              >
                Pause
              </button>
            )}
            {state.status === 'paused' && (
              <button
                type="button"
                disabled={busy}
                onClick={() => hostAction(() => api.resumeMeeting(token))}
              >
                Resume
              </button>
            )}
            {!meetingEnded && (
              <button
                type="button"
                className="danger"
                disabled={busy}
                onClick={() => {
                  if (window.confirm('End this meeting?')) {
                    hostAction(() => api.endMeeting(token));
                  }
                }}
              >
                End
              </button>
            )}
            <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 13 }}>
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
              onClick={() => {
                if (!window.confirm('Regenerate links? Everyone currently connected will be disconnected.')) return;
                hostAction(async () => {
                  const result = await api.regenerateTokens(token);
                  window.location.replace(result.host_url);
                });
              }}
            >
              Regenerate links
            </button>
          </>
        )}
      </div>

      {showOfflineBanner && (
        <div className="banner warning" role="status">
          Intelligence offline — transcript continues; insights paused.
        </div>
      )}

      {showDiarizationBanner && (
        <div className="banner info" role="status">
          Speaker diarization unavailable — using Me / Others channels.
        </div>
      )}

      {state.status === 'active' && state.capture?.message && (
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

      {isHost && fullGuestUrl && (
        <div className="guest-link-row">
          <input readOnly value={fullGuestUrl} aria-label="Guest link" />
          <button type="button" onClick={copyGuestLink}>
            {copied ? 'Copied' : 'Copy guest link'}
          </button>
        </div>
      )}
    </header>
  );
}
