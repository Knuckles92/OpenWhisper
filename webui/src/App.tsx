import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react';
import { api } from './api';
import CardsPane from './components/CardsPane';
import HeaderBar from './components/HeaderBar';
import HistoryPane from './components/HistoryPane';
import ActivityPane from './components/ActivityPane';
import JoinGate from './components/JoinGate';
import MeetingOverview from './components/MeetingOverview';
import NotesPane from './components/NotesPane';
import ParticipantsPane from './components/ParticipantsPane';
import SpotlightRow from './components/SpotlightRow';
import ReportTabs from './components/report/ReportTabs';
import TranscriptPane from './components/TranscriptPane';
import { EvidenceProvider } from './evidence';
import {
  enabledReportViews,
  resolveReportView,
  writeStoredReportView,
  type ReportViewId,
} from './report';
import { initialUiState, meetingReducer } from './state';
import type { Op, Role, SessionResponse } from './types';
import { MeetingSocket, socketStatusMessage } from './ws';

/** Read meeting token from `/m/{token}` or `?token=`. */
export function extractToken(): string | null {
  const pathMatch = location.pathname.match(/\/m\/([^/?#]+)/);
  if (pathMatch?.[1]) return decodeURIComponent(pathMatch[1]);
  return new URLSearchParams(location.search).get('token');
}

export interface DashboardProps {
  token: string;
  role: Role;
  guestName: string | null;
  initialSession: SessionResponse;
}

function MeetingDashboard({ token, role, guestName, initialSession }: DashboardProps) {
  const [ui, dispatch] = useReducer(meetingReducer, initialUiState);
  const socketRef = useRef<MeetingSocket | null>(null);
  const isHost = role === 'host';
  const initialHistoryMeetingId = useMemo(
    () => new URLSearchParams(location.search).get('history'),
    [],
  );
  const [showHistory, setShowHistory] = useState(
    isHost && Boolean(initialHistoryMeetingId),
  );
  const [showActivity, setShowActivity] = useState(true);
  const [highlightSegmentId, setHighlightSegmentId] = useState<string | null>(null);
  const [transcriptComplete, setTranscriptComplete] = useState(false);
  const [reportView, setReportView] = useState<ReportViewId>(() =>
    resolveReportView(['ribbon', 'brief', 'signal']),
  );
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const workspaceRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    dispatch({
      type: 'server_message',
      msg: {
        type: 'hello',
        role,
        participant_id: null,
        seq: initialSession.state.seq,
        state: initialSession.state,
        segments: [],
        urls: {},
        meeting: initialSession.meeting,
      },
    });
  }, [role, initialSession]);

  useEffect(() => {
    const socket = new MeetingSocket({
      token,
      name: guestName,
      onMessage: (msg) => dispatch({ type: 'server_message', msg }),
      onStatus: (status) => dispatch({ type: 'socket_status', status }),
    });
    socketRef.current = socket;
    socket.connect();
    return () => {
      socket.close();
      socketRef.current = null;
    };
  }, [token, guestName]);

  useEffect(() => {
    let cancelled = false;
    const hydrate = async () => {
      setTranscriptComplete(false);
      try {
        let cursor: string | undefined;
        do {
          const page = await api.transcriptPage(token, cursor);
          if (cancelled) return;
          dispatch({ type: 'hydrate_segments', segments: page.items });
          cursor = page.next_cursor ?? undefined;
        } while (cursor);
      } catch {
        /* live WebSocket remains usable when background history hydration fails */
      }
      if (!cancelled) setTranscriptComplete(true);
    };
    hydrate();
    return () => {
      cancelled = true;
    };
  }, [token]);

  const reportViews = useMemo(
    () => (ui.state ? enabledReportViews(ui.state) : ['ribbon' as const]),
    [ui.state],
  );

  useEffect(() => {
    if (!reportViews.includes(reportView)) {
      setReportView(resolveReportView(reportViews));
    }
  }, [reportView, reportViews]);

  const selectReportView = useCallback((view: ReportViewId) => {
    setReportView(view);
    writeStoredReportView(view);
  }, []);

  const sendOp = useCallback(async (op: Op): Promise<boolean> => {
    const socket = socketRef.current;
    if (!socket) {
      dispatch({ type: 'client_error', message: 'You are offline. Your change was not sent.' });
      return false;
    }
    try {
      const results = await socket.sendAction(op);
      const acknowledged = results.length > 0;
      if (!acknowledged) {
        dispatch({ type: 'client_error', message: 'The server did not acknowledge the change.' });
      }
      const rejected = results.find((result) => !result.ok);
      if (rejected) {
        dispatch({
          type: 'client_error',
          message: (rejected.reason || 'Action was rejected').replace(/_/g, ' '),
        });
      }
      return acknowledged && !rejected;
    } catch (err) {
      dispatch({
        type: 'client_error',
        message: err instanceof Error ? err.message : 'Your change could not be sent.',
      });
      return false;
    }
  }, []);

  const sendUndo = useCallback(async (seq: number): Promise<boolean> => {
    const socket = socketRef.current;
    if (!socket) {
      dispatch({ type: 'client_error', message: 'You are offline. Undo was not sent.' });
      return false;
    }
    try {
      const results = await socket.sendUndo(seq);
      const acknowledged = results.length > 0;
      if (!acknowledged) {
        dispatch({ type: 'client_error', message: 'The server did not acknowledge undo.' });
      }
      const rejected = results.find((result) => !result.ok);
      if (rejected) {
        dispatch({
          type: 'client_error',
          message: (rejected.reason || 'Undo was rejected').replace(/_/g, ' '),
        });
      }
      return acknowledged && !rejected;
    } catch (err) {
      dispatch({
        type: 'client_error',
        message: err instanceof Error ? err.message : 'Undo could not be sent.',
      });
      return false;
    }
  }, []);

  const participants = useMemo(
    () => (ui.state ? Object.values(ui.state.participants) : []),
    [ui.state],
  );

  const handleEvidenceClick = useCallback(async (segmentId: string) => {
    if (!ui.segments.some((segment) => segment.id === segmentId) && ui.state) {
      try {
        const segment = await api.segment(token, ui.state.meeting_id, segmentId);
        dispatch({ type: 'hydrate_segments', segments: [segment] });
      } catch {
        return;
      }
    }
    setHighlightSegmentId(segmentId);
    setShowHistory(false);
  }, [token, ui.segments, ui.state]);

  useLayoutEffect(() => {
    const el = workspaceRef.current;
    if (!el) return undefined;
    const sync = () => {
      el.style.setProperty('--workspace-scrollport', `${el.clientHeight}px`);
    };
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(el);
    return () => observer.disconnect();
  }, [showHistory]);

  const seekTo = useCallback((seconds: number) => {
    const el = audioRef.current;
    if (!el) return;
    const apply = () => {
      try {
        el.currentTime = seconds;
        void el.play();
      } catch {
        /* seeking may fail until metadata is ready */
      }
    };
    if (el.readyState >= 1) apply();
    else el.addEventListener('loadedmetadata', apply, { once: true });
  }, []);

  if (!ui.state) {
    return <div className="loading-screen" role="status" aria-live="polite">Connecting…</div>;
  }

  if (ui.socketStatus === 'unauthorized' || ui.socketStatus === 'name_required') {
    return (
      <div className="error-screen" role="alert">
        <p>{ui.socketStatus === 'unauthorized' ? 'Meeting link expired' : 'Unable to join meeting'}</p>
        <p>{socketStatusMessage(ui.socketStatus)}</p>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <HeaderBar
        token={token}
        isHost={isHost}
        state={ui.state}
        meeting={ui.meeting}
        guestUrl={ui.guestUrl}
        socketStatus={ui.socketStatus}
        meetingEnded={ui.meetingEnded}
        lastError={ui.lastError}
        onSendOp={sendOp}
        onClientError={(message) => dispatch({ type: 'client_error', message })}
        onClearError={() => dispatch({ type: 'clear_error' })}
        onToggleHistory={() => {
          setShowHistory((v) => !v);
        }}
        showHistory={showHistory}
        onToggleActivity={() => {
          setShowActivity((v) => !v);
          setShowHistory(false);
        }}
        showActivity={showActivity}
        transcriptComplete={transcriptComplete}
        reportView={reportView}
        onReportViewChange={selectReportView}
      />

      {showHistory && isHost ? (
        <div className="app-main full-bleed">
          <HistoryPane
            token={token}
            initialMeetingId={initialHistoryMeetingId}
            onClose={() => setShowHistory(false)}
          />
        </div>
      ) : (
        <EvidenceProvider segments={ui.segments} participants={participants}>
          <div className="app-main workspace" ref={workspaceRef} data-workspace-scroll>
            <aside className="workspace-conversation">
              <TranscriptPane
                segments={ui.segments}
                participants={participants}
                highlightSegmentId={highlightSegmentId}
                onHighlightClear={() => setHighlightSegmentId(null)}
                newestFirst
                onReassignSpeaker={(segmentId, participantId) =>
                  sendOp({ op: 'reassign_segment_speaker', segment_id: segmentId, participant_id: participantId })
                }
                headerExtra={
                  <div className="recording-inline">
                    <audio
                      ref={audioRef}
                      key={`${ui.state.meeting_id}:${ui.state.status}`}
                      controls
                      aria-label="Meeting audio recording"
                      preload={ui.state.status === 'active' ? 'none' : 'metadata'}
                      src={api.audioUrl(
                        token,
                        ui.state.meeting_id,
                        ui.state.status,
                      )}
                    />
                  </div>
                }
              />
            </aside>

            <div className="workspace-center">
              {isHost && showActivity && (
                <ActivityPane
                  token={token}
                  onUndo={sendUndo}
                  onHide={() => setShowActivity(false)}
                  refreshKey={ui.state.seq}
                  cloudEnabled={ui.state.cloud_enabled}
                  intelligenceOnline={ui.state.intelligence_online}
                  meetingStatus={ui.state.status}
                  finalizationStatus={ui.state.finalization?.status ?? null}
                  finalizationMessage={ui.state.finalization?.message ?? null}
                  agentActivity={ui.agentActivity}
                />
              )}

              {ui.state.status === 'ended' || ui.state.finalization?.status === 'completed' ? (
                <ReportTabs
                  state={ui.state}
                  segments={ui.segments}
                  meeting={ui.meeting}
                  onEvidenceClick={handleEvidenceClick}
                  onSeek={seekTo}
                  transcriptComplete={transcriptComplete}
                  showDownload={false}
                  showSwitcher={false}
                  activeView={reportView}
                  onViewChange={selectReportView}
                />
              ) : (
                <>
                  <MeetingOverview
                    meetingTitle={ui.state.title || ui.meeting?.title || 'Meeting'}
                    status={ui.state.status}
                    topic={ui.state.topic.current}
                    topicEvidence={
                      ui.state.topic.history[ui.state.topic.history.length - 1]?.evidence ?? []
                    }
                    summary={ui.state.rolling_summary}
                    summaryEvidence={ui.state.rolling_summary_evidence}
                    cloudEnabled={ui.state.cloud_enabled}
                    intelligenceOnline={ui.state.intelligence_online}
                    onEvidenceClick={handleEvidenceClick}
                  />

                  <SpotlightRow
                    cards={ui.state.cards}
                    status={ui.state.status}
                    cloudEnabled={ui.state.cloud_enabled}
                    intelligenceOnline={ui.state.intelligence_online}
                    onSendOp={sendOp}
                    onEvidenceClick={handleEvidenceClick}
                    onUndo={isHost ? sendUndo : undefined}
                    lastSeqByTarget={ui.lastSeqByTarget}
                  />

                  <NotesPane
                    notes={ui.state.cards.live_notes ?? []}
                    status={ui.state.status}
                    cloudEnabled={ui.state.cloud_enabled}
                    intelligenceOnline={ui.state.intelligence_online}
                    onSendOp={sendOp}
                    onEvidenceClick={handleEvidenceClick}
                    onUndo={isHost ? sendUndo : undefined}
                    lastSeqByTarget={ui.lastSeqByTarget}
                  />
                </>
              )}
            </div>

            <aside className="workspace-rail">
              <section className="panel capture">
                <h3 className="capture-heading">Captured</h3>
                <div className="capture-body">
                  <CardsPane
                    cards={ui.state.cards}
                    questions={ui.state.questions}
                    onSendOp={sendOp}
                    onEvidenceClick={handleEvidenceClick}
                    onUndo={isHost ? sendUndo : undefined}
                    lastSeqByTarget={ui.lastSeqByTarget}
                    newestFirst
                    embedded
                  />
                </div>
              </section>
              <ParticipantsPane
                participants={participants}
                onlineIds={ui.onlineIds}
                onRename={(participantId, displayName) =>
                  sendOp({ op: 'rename_participant', participant_id: participantId, display_name: displayName })
                }
              />
            </aside>
          </div>
        </EvidenceProvider>
      )}
    </div>
  );
}

export default function App() {
  const token = useMemo(() => extractToken(), []);
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [guestName, setGuestName] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    api
      .session(token)
      .then((s) => {
        if (!cancelled) setSession(s);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : 'Failed to load session');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token]);

  if (!token) {
    return (
      <div className="error-screen" role="alert">
        <p>Missing meeting token.</p>
        <p>Open the link shared by the host.</p>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="error-screen" role="alert">
        <p>Unable to join meeting</p>
        <p>{loadError}</p>
      </div>
    );
  }

  if (!session) {
    return <div className="loading-screen" role="status" aria-live="polite">Loading meeting…</div>;
  }

  if (session.role === 'guest' && !guestName) {
    return (
      <JoinGate
        meetingTitle={session.state.title || session.meeting.title || 'Meeting'}
        onJoin={(name) => setGuestName(name)}
      />
    );
  }

  return (
    <MeetingDashboard
      token={token}
      role={session.role}
      guestName={guestName}
      initialSession={session}
    />
  );
}
