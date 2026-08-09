import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { AuditEvent } from '../types';

interface ActivityPaneProps {
  token: string;
  onUndo: (seq: number) => void;
}

export default function ActivityPane({ token, onUndo }: ActivityPaneProps) {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(true);

  const load = useCallback(async (reset = false) => {
    setLoading(true);
    setError(null);
    try {
      const beforeSeq = reset || events.length === 0
        ? undefined
        : events[events.length - 1].seq;
      const rows = await api.events(token, beforeSeq);
      setEvents((current) => {
        const base = reset ? [] : current;
        const bySeq = new Map(base.map((event) => [event.seq, event]));
        for (const event of rows) bySeq.set(event.seq, event);
        return [...bySeq.values()].sort((a, b) => b.seq - a.seq);
      });
      setHasMore(rows.length === 100);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load activity');
    } finally {
      setLoading(false);
    }
  }, [token, events]);

  useEffect(() => {
    load(true);
    // Loading once on mount is intentional; the Refresh control picks up
    // later events without turning every WebSocket patch into another fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const undo = (seq: number) => {
    onUndo(seq);
    setEvents((current) => current.map((event) => (
      event.seq === seq ? { ...event, undoable: false } : event
    )));
  };

  return (
    <section className="panel">
      <div className="panel-header">
        <span>Activity and undo</span>
        <button type="button" className="ghost" disabled={loading} onClick={() => load(true)}>
          Refresh
        </button>
      </div>
      <div className="panel-body">
        {error && <div className="banner warning">{error}</div>}
        {events.length === 0 ? <p className="empty-state">No activity yet.</p> : (
          <div className="segment-list">
            {events.map((event) => (
              <article className="segment" key={event.seq}>
                <div className="segment-meta">
                  <strong>#{event.seq}</strong>
                  <span>{new Date(event.ts).toLocaleString()}</span>
                  <span>{event.actor_type}</span>
                </div>
                <p className="segment-text">
                  {event.action.replace(/_/g, ' ')}{event.target_id ? ` · ${event.target_id}` : ''}
                </p>
                {event.undoable && (
                  <button type="button" onClick={() => undo(event.seq)}>Undo</button>
                )}
              </article>
            ))}
          </div>
        )}
        {hasMore && events.length > 0 && (
          <button type="button" disabled={loading} onClick={() => load(false)}>
            {loading ? 'Loading…' : 'Load older activity'}
          </button>
        )}
      </div>
    </section>
  );
}
