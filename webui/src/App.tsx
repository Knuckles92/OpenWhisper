import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { api } from './api';
import CardsPane from './components/CardsPane';
import HeaderBar from './components/HeaderBar';
import HistoryPane from './components/HistoryPane';
import JoinGate from './components/JoinGate';
import ParticipantsPane from './components/ParticipantsPane';
import QuestionInbox from './components/QuestionInbox';
import TranscriptPane from './components/TranscriptPane';
import { initialUiState, meetingReducer } from './state';
import type { Op, Role, SessionResponse } from './types';
import { MeetingSocket } from './ws';

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
  const [showHistory, setShowHistory] = useState(false);
  const [highlightSegmentId, setHighlightSegmentId] = useState<string | null>(null);

  const isHost = role === 'host';

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

  const sendOp = useCallback((op: Op) => {
    socketRef.current?.sendAction(op);
  }, []);

  const sendUndo = useCallback((seq: number) => {
    socketRef.current?.sendUndo(seq);
  }, []);

  const participants = useMemo(
    () => (ui.state ? Object.values(ui.state.participants) : []),
    [ui.state],
  );

  const handleEvidenceClick = useCallback((segmentId: string) => {
    setHighlightSegmentId(segmentId);
    setShowHistory(false);
    requestAnimationFrame(() => {
      document.getElementById(`seg-${segmentId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
  }, []);

  if (!ui.state) {
    return <div className="loading-screen">Connecting…</div>;
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
        onClearError={() => dispatch({ type: 'clear_error' })}
        onToggleHistory={() => setShowHistory((v) => !v)}
        showHistory={showHistory}
      />

      {showHistory && isHost ? (
        <div className="app-main" style={{ gridTemplateColumns: '1fr' }}>
          <HistoryPane token={token} onClose={() => setShowHistory(false)} />
        </div>
      ) : (
        <div className="app-main">
          <TranscriptPane
            segments={ui.segments}
            participants={participants}
            highlightSegmentId={highlightSegmentId}
            onHighlightClear={() => setHighlightSegmentId(null)}
            onReassignSpeaker={(segmentId, participantId) =>
              sendOp({ op: 'reassign_segment_speaker', segment_id: segmentId, participant_id: participantId })
            }
          />

          <div className="center-stack">
            <CardsPane
              cards={ui.state.cards}
              onSendOp={sendOp}
              onEvidenceClick={handleEvidenceClick}
              onUndo={isHost ? sendUndo : undefined}
              lastSeqByTarget={ui.lastSeqByTarget}
            />
            <QuestionInbox
              questions={ui.state.questions}
              onSendOp={sendOp}
              onEvidenceClick={handleEvidenceClick}
            />
          </div>

          <ParticipantsPane
            participants={participants}
            onlineIds={ui.onlineIds}
            onRename={(participantId, displayName) =>
              sendOp({ op: 'rename_participant', participant_id: participantId, display_name: displayName })
            }
          />
        </div>
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
      <div className="error-screen">
        <p>Missing meeting token.</p>
        <p>Open the link shared by the host.</p>
      </div>
    );
  }

  if (loadError) {
    return (
      <div className="error-screen">
        <p>Unable to join meeting</p>
        <p>{loadError}</p>
      </div>
    );
  }

  if (!session) {
    return <div className="loading-screen">Loading meeting…</div>;
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
