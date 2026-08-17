import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { AuditEvent } from '../types';

interface ActivityPaneProps {
  token: string;
  onUndo: (seq: number) => void;
  onHide: () => void;
  refreshKey: number;
  cloudEnabled: boolean;
  intelligenceOnline: boolean;
  meetingStatus: string;
  finalizationStatus?: string | null;
  finalizationMessage?: string | null;
}

const CARD_NAMES: Record<string, string> = {
  key_points: 'key point',
  decisions: 'decision',
  action_items: 'action item',
  risks: 'risk or disagreement',
  timeline: 'timeline moment',
  user_notes: 'note',
};

function textValue(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function eventDetail(event: AuditEvent): string | null {
  const payload = event.payload ?? {};
  const set = payload.set && typeof payload.set === 'object'
    ? payload.set as Record<string, unknown>
    : {};
  return textValue(payload.text)
    ?? textValue(set.text)
    ?? textValue(payload.answer_text)
    ?? textValue(payload.display_name);
}

function eventDescription(event: AuditEvent): string {
  const payload = event.payload ?? {};
  const card = CARD_NAMES[String(payload.card ?? '')] ?? 'insight';
  const descriptions: Record<string, string> = {
    add_item: `Captured a ${card}`,
    update_item: `Refined a ${card}`,
    remove_item: 'Removed an outdated insight',
    set_topic: 'Updated the conversation topic',
    set_rolling_summary: 'Refreshed the meeting summary',
    upsert_participant: 'Identified a speaker',
    suggest_participant_name: 'Suggested a speaker name',
    ask_question: 'Raised a question to revisit',
    resolve_question: 'Resolved a question from the conversation',
    revise_segment_text: 'Polished a transcript passage',
    pin_item: 'Pinned an insight',
    unpin_item: 'Unpinned an insight',
    confirm_item: 'Confirmed an insight',
    answer_question: 'Answered a question',
    dismiss_question: 'Dismissed a question',
    reopen_question: 'Reopened a question',
    rename_participant: 'Renamed a participant',
    reassign_segment_speaker: 'Corrected a speaker',
    set_title: 'Renamed the meeting',
    set_cloud_enabled: 'Changed cloud intelligence',
  };
  if (event.action.startsWith('undo:')) return 'Undid an earlier change';
  return descriptions[event.action] ?? event.action.replace(/_/g, ' ');
}

function actorLabel(event: AuditEvent): string {
  if (event.actor_type === 'agent') return 'Pi';
  if (event.actor_type === 'system') return 'System';
  if (event.actor_type === 'host') return 'Host';
  return 'Participant';
}

function relativeTime(value: string): string {
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return '';
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  if (seconds < 45) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return new Date(value).toLocaleDateString();
}

export default function ActivityPane({
  token,
  onUndo,
  onHide,
  refreshKey,
  cloudEnabled,
  intelligenceOnline,
  meetingStatus,
  finalizationStatus = null,
  finalizationMessage = null,
}: ActivityPaneProps) {
  const [expanded, setExpanded] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(true);

  const mergeEvents = useCallback((rows: AuditEvent[], reset: boolean) => {
    setEvents((current) => {
      const bySeq = new Map((reset ? [] : current).map((event) => [event.seq, event]));
      for (const event of rows) bySeq.set(event.seq, event);
      return [...bySeq.values()].sort((a, b) => b.seq - a.seq);
    });
    setHasMore(rows.length === 100);
  }, []);

  const loadLatest = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    setError(null);
    try {
      mergeEvents(await api.events(token), true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load agent activity');
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [mergeEvents, token]);

  const loadOlder = async () => {
    const beforeSeq = events[events.length - 1]?.seq;
    if (beforeSeq == null) return;
    setLoading(true);
    setError(null);
    try {
      mergeEvents(await api.events(token, beforeSeq), false);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load older activity');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadLatest();
  }, [loadLatest]);

  useEffect(() => {
    if (refreshKey <= 0) return undefined;
    const timer = window.setTimeout(() => void loadLatest(true), 450);
    return () => window.clearTimeout(timer);
  }, [loadLatest, refreshKey]);

  const agentEvents = useMemo(
    () => events.filter((event) => event.actor_type === 'agent'),
    [events],
  );
  const visibleEvents = showAll ? events : agentEvents;
  const latestAgentEvent = agentEvents[0];

  let statusText = 'Watching the conversation';
  let statusTone = 'online';
  const finalMsg = (finalizationMessage || '').trim();
  // Never infer review completion solely from meetingStatus === 'ended'.
  if (finalizationStatus === 'running') {
    statusText = finalMsg || 'Wrapping up final insights.';
    statusTone = 'online';
  } else if (finalizationStatus === 'completed') {
    statusText = finalMsg || 'Session review complete.';
    statusTone = 'complete';
  } else if (finalizationStatus === 'disabled') {
    statusText = finalMsg || 'Cloud insights were off for this meeting.';
    statusTone = 'paused';
  } else if (finalizationStatus === 'unavailable' || finalizationStatus === 'failed') {
    statusText =
      finalMsg ||
      (finalizationStatus === 'failed'
        ? 'Final insights failed.'
        : 'Final insights unavailable.');
    statusTone = 'offline';
  } else if (meetingStatus === 'ending') {
    statusText = 'Finishing transcription…';
  } else if (meetingStatus === 'ended' || meetingStatus === 'needs_recovery') {
    statusText = cloudEnabled
      ? 'Meeting ended — waiting for finalization status.'
      : 'Meeting ended.';
    statusTone = 'complete';
  } else if (!cloudEnabled) {
    statusText = 'Paused — cloud insights are off';
    statusTone = 'paused';
  } else if (!intelligenceOnline) {
    statusText = 'Offline — the transcript is still running';
    statusTone = 'offline';
  }

  return (
    <section className={`agent-activity no-print ${expanded ? 'expanded' : 'collapsed'}`} aria-label="Pi agent activity">
      <div className="agent-activity-summary">
        <button
          type="button"
          className="agent-activity-toggle"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <span className={`agent-status-dot ${statusTone}`} aria-hidden="true" />
          <span className="agent-activity-copy">
            <span className="agent-activity-title">
              Pi activity
              <span className="agent-activity-count">
                {agentEvents.length ? `${agentEvents.length} recent` : 'No updates yet'}
              </span>
            </span>
            <span className="agent-activity-status">{statusText}</span>
            {!expanded && latestAgentEvent && (
              <span className="agent-activity-latest">
                Latest: {eventDescription(latestAgentEvent)}
              </span>
            )}
          </span>
          <span className="agent-activity-chevron" aria-hidden="true">⌄</span>
        </button>
        <button
          type="button"
          className="agent-activity-hide"
          aria-label="Hide Pi activity"
          title="Hide Pi activity"
          onClick={onHide}
        >
          ×
        </button>
      </div>

      {expanded && (
        <div className="agent-activity-body">
          <div className="agent-activity-toolbar">
            <div className="activity-filter" role="group" aria-label="Activity filter">
              <button
                type="button"
                className={!showAll ? 'selected' : ''}
                onClick={() => setShowAll(false)}
              >
                Pi only
              </button>
              <button
                type="button"
                className={showAll ? 'selected' : ''}
                onClick={() => setShowAll(true)}
              >
                All changes
              </button>
            </div>
            <button type="button" className="activity-refresh" disabled={loading} onClick={() => void loadLatest()}>
              {loading ? 'Refreshing…' : 'Refresh'}
            </button>
          </div>

          {error && <div className="activity-error" role="status">{error}</div>}
          {visibleEvents.length === 0 ? (
            <p className="agent-activity-empty">
              {cloudEnabled
                ? 'Pi has not changed the meeting insights yet.'
                : 'Turn on cloud insights when you want Pi to follow the conversation.'}
            </p>
          ) : (
            <ol className="activity-list">
              {visibleEvents.map((event) => {
                const detail = eventDetail(event);
                return (
                  <li className="activity-entry" key={event.seq}>
                    <span className={`activity-entry-mark ${event.actor_type}`} aria-hidden="true" />
                    <div className="activity-entry-content">
                      <div className="activity-entry-heading">
                        <strong>{eventDescription(event)}</strong>
                        <span title={new Date(event.ts).toLocaleString()}>{relativeTime(event.ts)}</span>
                      </div>
                      {detail && <p>{detail}</p>}
                      <div className="activity-entry-meta">
                        <span>{actorLabel(event)}</span>
                        {event.undoable && (
                          <button type="button" onClick={() => {
                            onUndo(event.seq);
                            setEvents((current) => current.map((item) => (
                              item.seq === event.seq ? { ...item, undoable: false } : item
                            )));
                          }}>
                            Undo
                          </button>
                        )}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ol>
          )}

          {showAll && hasMore && events.length > 0 && (
            <button type="button" className="activity-load-more" disabled={loading} onClick={() => void loadOlder()}>
              {loading ? 'Loading…' : 'Load older activity'}
            </button>
          )}
        </div>
      )}
    </section>
  );
}
