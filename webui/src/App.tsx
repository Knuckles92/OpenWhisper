import InsightReview from './components/InsightReview';
import HighlightPulseStrip from './components/HighlightPulseStrip';
import { playMoment } from './playback';
import RecordingPlayer, { type PlaybackMoment } from './components/RecordingPlayer';
import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from 'react';
import { api } from './api';
import SelectionInsight from './components/SelectionInsight';
import { correctedSegments, correctionText } from './corrections';
import { hydrateTranscript, sendDashboardAction, type TranscriptLoadState } from './dashboardActions';
import CardsPane from './components/CardsPane';
import HeaderBar from './components/HeaderBar';
import HistoryPane from './components/HistoryPane';
import ActivityPane from './components/ActivityPane';
import VoiceAssistantBubble from './components/VoiceAssistantBubble';
import VoiceCommandHelp from './components/VoiceCommandHelp';
import JoinGate from './components/JoinGate';
import MeetingBrief from './components/MeetingBrief';
import MeetingOverview from './components/MeetingOverview';
import NotesPane from './components/NotesPane';
import ParticipantsPane from './components/ParticipantsPane';
import PreFlight from './components/PreFlight';
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
import { topicChapters } from './chapters';
import RoomDisplay from './components/RoomDisplay';
import { JumpToLive, PocketTabs, pocketTabs, usePocketLayout, usePocketTab } from './components/PocketShell';
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
  // Lifted out of HistoryPane so the header button can open the meeting the
  // host picked, instead of dropping them back on this dashboard's own meeting.
  const [historyMeetingId, setHistoryMeetingId] = useState<string | null>(
    initialHistoryMeetingId,
  );
  const [historyFocused, setHistoryFocused] = useState(false);
  const [showActivity, setShowActivity] = useState(true);
  const [roomOpen, setRoomOpen] = useState(
    () => new URLSearchParams(location.search).get('view') === 'room',
  );
  const closeRoom = useCallback(() => setRoomOpen(false), []);
  const pocket = usePocketLayout();
  const [pocketTab, setPocketTab] = usePocketTab(
    ui.state?.status === 'ended' || ui.state?.finalization?.status === 'completed',
  );
  const [highlightSegmentId, setHighlightSegmentId] = useState<string | null>(null);
  const [transcriptLoad, setTranscriptLoad] = useState<TranscriptLoadState>({ status: 'loading' });
  const [transcriptAttempt, setTranscriptAttempt] = useState(0);
  const transcriptComplete = transcriptLoad.status === 'complete';
  const [reportView, setReportView] = useState<ReportViewId>(() =>
    resolveReportView(['ribbon', 'brief', 'signal']),
  );
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playbackMoment, setPlaybackMoment] = useState<PlaybackMoment | null>(null);
  const workspaceRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    dispatch({
      type: 'server_message',
      msg: {
        type: 'hello',
        voice_commands: initialSession.voice_commands,
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
    void hydrateTranscript(
      (cursor) => api.transcriptPage(token, cursor),
      (segments) => dispatch({ type: 'hydrate_segments', segments }),
      setTranscriptLoad,
      () => cancelled,
    );
    return () => {
      cancelled = true;
    };
  }, [token, transcriptAttempt]);

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

  const onActionError = useCallback((message: string) => {
    dispatch({ type: 'client_error', message });
  }, []);

  const sendOp = useCallback((op: Op): Promise<boolean> => {
    const socket = socketRef.current;
    return sendDashboardAction(
      socket ? () => socket.sendAction(op) : null, 'change', onActionError,
    );
  }, [onActionError]);

  const sendUndo = useCallback((seq: number): Promise<boolean> => {
    const socket = socketRef.current;
    return sendDashboardAction(
      socket ? () => socket.sendUndo(seq) : null, 'undo', onActionError,
    );
  }, [onActionError]);

  // Term corrections come from the user_notes card alone; keying on it keeps
  // unrelated state ticks from re-mapping every transcript row.
  const userNotes = ui.state?.cards.user_notes;
  const correct = useMemo(() => correctionText(userNotes), [userNotes]);
  const segments = useMemo(() => correctedSegments(ui.segments, correct), [ui.segments, correct]);

  const participants = useMemo(
    () => (ui.state ? Object.values(ui.state.participants) : []),
    [ui.state],
  );

  // How far the meeting has run, in recording seconds: the ruler's right edge.
  const previewList = useMemo(() => Object.values(ui.speechPreviews), [ui.speechPreviews]);
  const meetingSpan = useMemo(
    () => [...segments, ...previewList].reduce((edge, item) => Math.max(edge, item.end_s || 0), 0),
    [segments, previewList],
  );
  const topicHistory = ui.state?.topic.history;
  const startedAt = ui.meeting?.started_at;
  const chapters = useMemo(
    () => topicChapters(topicHistory ?? [], startedAt, meetingSpan || undefined),
    [topicHistory, startedAt, meetingSpan],
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
    setPlaybackMoment({ start_s: seconds, text: 'Listen from this point in the meeting.' });
    playMoment(audioRef.current, seconds);
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

  // Re-keying the player swaps the <audio> node; the minimap playhead needs
  // the same key so it re-binds to the live element.
  const audioKey = `${ui.state.meeting_id}:${ui.state.status}`;
  const meetingRunning = ['active', 'paused'].includes(ui.state.status);
  // Before anyone has spoken, the centre shows readiness instead of empty placeholders.
  const warmingUp = meetingRunning && transcriptComplete && segments.length === 0
    && previewList.every((preview) => !preview.text.trim());
  const reportMode = ui.state.status === 'ended' || ui.state.finalization?.status === 'completed';
  const capturedCount = ['decisions', 'action_items', 'risks', 'key_points', 'timeline'].reduce(
    (total, key) => total + (ui.state!.cards[key as 'decisions'] ?? []).filter((item) => item.status !== 'removed').length,
    0,
  );
  // Once the final insights are in, live-agent chatter no longer earns space above the report.
  const reportsSettled = ui.state.status === 'ended' && ui.state.finalization?.status === 'completed';

  return (
    <div className="app-shell">
      {!showHistory && <SelectionInsight key={ui.state.meeting_id} onSend={sendOp} live={ui.state.status === 'active'} online={ui.state.cloud_enabled && ui.state.intelligence_online} />}
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
          setPlaybackMoment(null);
          setHistoryFocused(false);
          setShowHistory((v) => !v);
        }}
        showHistory={showHistory}
        historySelectionId={historyMeetingId}
        historyFocused={historyFocused}
        onOpenHistoryMeeting={() => setHistoryFocused(true)}
        onExitHistoryMeeting={() => setHistoryFocused(false)}
        onToggleActivity={() => {
          setShowActivity((v) => !v);
          setHistoryFocused(false);
          setShowHistory(false);
        }}
        showActivity={showActivity}
        transcriptLoadError={transcriptLoad.error ?? null}
        onRetryTranscript={() => setTranscriptAttempt((attempt) => attempt + 1)}
        transcriptComplete={transcriptComplete}
        reportView={reportView}
        onReportViewChange={selectReportView}
        participants={participants}
        onlineIds={ui.onlineIds}
        listening={ui.state.status === 'active' && previewList.length > 0}
        onOpenRoom={() => setRoomOpen(true)}
      />

      {showHistory && isHost ? (
        <div className="app-main full-bleed">
          <HistoryPane
            token={token}
            initialMeetingId={initialHistoryMeetingId}
            selectedId={historyMeetingId}
            onSelectMeeting={setHistoryMeetingId}
            focused={historyFocused}
            onFocusChange={setHistoryFocused}
            onClose={() => {
              setHistoryFocused(false);
              setShowHistory(false);
            }}
          />
        </div>
      ) : (
        <EvidenceProvider segments={segments} participants={participants}>
          <div
            className={`app-main workspace${pocket ? ' pocket' : ''}${pocket && reportMode ? ' ended' : ''}`}
            ref={workspaceRef}
            data-workspace-scroll
            data-pocket-tab={pocket ? pocketTab : undefined}
          >
            <div className="workspace-ruler">
              <HighlightPulseStrip pulses={ui.state.live_highlights ?? []}
                highlightStatus={ui.state.live_highlights_status} meetingStatus={ui.state.status}
                cloudEnabled={ui.state.cloud_enabled} isHost={isHost}
                chapters={chapters} segments={segments} participants={participants}
                durationS={meetingSpan} nowEdge={meetingRunning}
                onSelect={pulse => {
                  setPlaybackMoment(pulse);
                  void handleEvidenceClick(pulse.segment_id);
                  playMoment(audioRef.current, pulse.start_s, api.audioUrl(token, ui.state!.meeting_id, Date.now()));
                }} />
            </div>
            <aside className="workspace-conversation">
              <TranscriptPane
                segments={segments}
                previews={ui.meetingEnded ? [] : Object.values(ui.speechPreviews).map((preview) => ({ ...preview, text: correct(preview.text) }))}
                participants={participants}
                highlightSegmentId={highlightSegmentId}
                onHighlightClear={() => setHighlightSegmentId(null)}
                newestFirst
                onReassignSpeaker={(segmentId, participantId) =>
                  sendOp({ op: 'reassign_segment_speaker', segment_id: segmentId, participant_id: participantId })
                }
                audioRef={audioRef}
                audioKey={audioKey}
                onPlaySegment={(startSeconds) => {
                  // While the meeting is live the player is `preload="none"`, so
                  // there is no metadata to seek against until a source is handed
                  // over; a finished recording is already loaded and just moves.
                  const audio = audioRef.current;
                  playMoment(audio, startSeconds, audio && audio.readyState >= 1
                    ? undefined
                    : api.audioUrl(token, ui.state!.meeting_id, Date.now()));
                }}
                headerExtra={
                  <div className="recording-inline">
                    <RecordingPlayer
                      audioRef={audioRef}
                      key={audioKey}
                      label="Meeting audio recording"
                      preload={ui.state.status === 'active' ? 'none' : 'metadata'}
                      src={api.audioUrl(token, ui.state.meeting_id, ui.state.status)}
                      moment={playbackMoment}
                      onClose={() => setPlaybackMoment(null)}
                      onPopOut={setPlaybackMoment}
                    />
                  </div>
                }
              />
            </aside>

            <div className="workspace-center">
              {isHost && showActivity && !reportsSettled && (
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
                <>
                {isHost && <InsightReview state={ui.state} onSendOp={sendOp} onEvidenceClick={handleEvidenceClick}
                  onRetry={async () => {
                    const result = await api.review(token, ui.state!.meeting_id, {op: 'start'});
                    if (!result.ok) throw new Error(result.error);
                  }} />}
                <ReportTabs
                  state={ui.state}
                  segments={segments}
                  meeting={ui.meeting}
                  onEvidenceClick={handleEvidenceClick}
                  onSeek={seekTo}
                  audioRef={audioRef}
                  audioKey={audioKey}
                  transcriptComplete={transcriptComplete}
                  showDownload={false}
                  showSwitcher={false}
                  activeView={reportView}
                  onViewChange={selectReportView}
                  token={isHost ? token : undefined}
                />
                </>
              ) : (
                <>
                  {warmingUp ? (
                    <PreFlight
                      state={ui.state}
                      meeting={ui.meeting}
                      isHost={isHost}
                      guestUrl={ui.guestUrl}
                      participants={participants}
                      onlineIds={ui.onlineIds}
                      listening={ui.state.status === 'active' && previewList.length > 0}
                      onSendOp={sendOp}
                      onClientError={(message) => dispatch({ type: 'client_error', message })}
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

                  <MeetingBrief
                    intent={ui.state.intent}
                    isHost={isHost}
                    status={ui.state.status}
                    onSendOp={sendOp}
                  />

                  <div className="center-notes">
                  <NotesPane
                    newestFirst
                    onRequestAdjustment={(text) => api.requestNoteAdjustment(token, text)}
                    notes={ui.state.cards.live_notes ?? []}
                    status={ui.state.status}
                    cloudEnabled={ui.state.cloud_enabled}
                    intelligenceOnline={ui.state.intelligence_online}
                    onSendOp={sendOp}
                    onEvidenceClick={handleEvidenceClick}
                    onUndo={isHost ? sendUndo : undefined}
                    lastSeqByTarget={ui.lastSeqByTarget}
                  />
                  </div>
                  </>
                  )}

                  {meetingRunning &&
                    <VoiceCommandHelp guide={ui.voiceCommandGuide} meetingId={ui.state.meeting_id}
                      cloudEnabled={ui.state.cloud_enabled}
                      paused={ui.state.status === 'paused'} isHost={isHost} />}
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
                    highlightTop={3}
                    status={ui.state.status}
                    cloudEnabled={ui.state.cloud_enabled}
                    intelligenceOnline={ui.state.intelligence_online}
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
          {pocket && (
            <>
              <JumpToLive anchorRef={workspaceRef} enabled={pocketTab === 'live' || pocketTab === 'transcript'} />
              <PocketTabs
                tabs={pocketTabs(reportMode)}
                active={pocketTab}
                onSelect={(tab) => {
                  setPocketTab(tab);
                  const scroller = workspaceRef.current?.closest('.app-shell');
                  workspaceRef.current?.scrollTo?.({ top: 0 });
                  scroller?.scrollTo?.({ top: 0 });
                }}
                capturedCount={capturedCount}
              />
            </>
          )}
        </EvidenceProvider>
      )}
      {roomOpen && (
        <RoomDisplay
          state={ui.state}
          segments={segments}
          participants={participants}
          chapters={chapters}
          elapsedS={meetingSpan}
          onExit={closeRoom}
        />
      )}
      {!showHistory && !ui.meetingEnded && ui.state.status === 'active' &&
        <VoiceAssistantBubble feedback={ui.voiceFeedback} />}
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
