import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import type { ExportFormat, MeetingRow, MeetingStateDoc, SearchRow } from '../types';
import type { Segment } from '../types';
import ConfirmDialog from './ConfirmDialog';
import TranscriptPane from './TranscriptPane';
import ReportTabs from './report/ReportTabs';

interface HistoryPaneProps {
  token: string;
  initialMeetingId?: string | null;
  onClose: () => void;
}

export default function HistoryPane({ token, initialMeetingId, onClose }: HistoryPaneProps) {
  const [meetings, setMeetings] = useState<MeetingRow[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(initialMeetingId ?? null);
  const [query, setQuery] = useState('');
  const [searchResults, setSearchResults] = useState<SearchRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState('');
  const [detail, setDetail] = useState<MeetingStateDoc | null>(null);
  const [detailSegments, setDetailSegments] = useState<Segment[]>([]);
  const [highlightSegmentId, setHighlightSegmentId] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const [rerunningSpeakers, setRerunningSpeakers] = useState(false);
  const [rerunNote, setRerunNote] = useState<string | null>(null);
  const [transcriptComplete, setTranscriptComplete] = useState(false);
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const loadMeetings = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const rows = await api.meetings(token);
      setMeetings(rows);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load meetings');
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    loadMeetings();
  }, [loadMeetings]);

  useEffect(() => {
    setDetail(null);
    setDetailSegments([]);
    setDetailError(null);
    setRerunNote(null);
    setTranscriptComplete(false);
    if (!selectedId) return;
    let cancelled = false;
    api
      .meeting(token, selectedId)
      .then(async (res) => {
        if (cancelled) return;
        setDetail(res.state);
        let segments = res.segments;
        setDetailSegments(segments);
        let cursor = res.transcript_next_cursor ?? undefined;
        while (cursor && !cancelled) {
          const page = await api.meetingTranscriptPage(token, selectedId, cursor);
          const byId = new Map(segments.map((segment) => [segment.id, segment]));
          for (const segment of page.items) byId.set(segment.id, segment);
          segments = [...byId.values()].sort((a, b) => a.start_s - b.start_s);
          setDetailSegments(segments);
          cursor = page.next_cursor ?? undefined;
        }
        if (!cancelled) setTranscriptComplete(true);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setDetailError(err instanceof Error ? err.message : 'Failed to load meeting');
        }
      });
    return () => {
      cancelled = true;
    };
  }, [token, selectedId]);

  const runSearch = async () => {
    const q = query.trim();
    if (!q) {
      setSearchResults([]);
      return;
    }
    try {
      const results = await api.search(token, q);
      setSearchResults(results);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Search failed');
    }
  };

  const selectSearchResult = (row: SearchRow) => {
    const meetingId = String(row.meeting_id ?? '');
    const segmentId = String(row.segment_id ?? '');
    if (!meetingId) return;
    setSelectedId(meetingId);
    setHighlightSegmentId(segmentId || null);
  };

  const selected = meetings.find((m) => m.id === selectedId);

  useEffect(() => {
    if (selected) setRenameDraft(String(selected.title ?? ''));
  }, [selected]);

  const renameMeeting = async () => {
    if (!selectedId) return;
    const title = renameDraft.trim();
    if (!title) return;
    try {
      await api.renameMeeting(token, selectedId, title);
      await loadMeetings();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Rename failed');
    }
  };

  const deleteMeeting = async (meetingId: string) => {
    setDeleting(true);
    try {
      await api.deleteMeeting(token, meetingId);
      if (selectedId === meetingId) setSelectedId(null);
      setPendingDeleteId(null);
      await loadMeetings();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Delete failed');
    } finally {
      setDeleting(false);
    }
  };

  // The pass runs the whole transcript through the agent; it can take a minute.
  const rerunInsights = async (meetingId: string) => {
    setRerunning(true);
    setDetailError(null);
    setRerunNote(null);
    try {
      const res = await api.rerunInsights(token, meetingId);
      setDetail(res.state);
      if (res.ok) {
        setRerunNote(
          res.applied === 1 ? '1 update applied.' : `${res.applied} updates applied.`,
        );
      } else {
        setDetailError(res.error ?? 'Re-run failed');
      }
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : 'Re-run failed');
    } finally {
      setRerunning(false);
    }
  };

  const rerunSpeakers = async (meetingId: string) => {
    setRerunningSpeakers(true);
    setDetailError(null);
    setRerunNote(null);
    try {
      const res = await api.rerunSpeakers(token, meetingId);
      setDetail(res.state);
      const page = await api.meeting(token, meetingId);
      setDetail(page.state);
      setDetailSegments(page.segments);
      if (res.ok) {
        setRerunNote(
          res.applied === 1
            ? '1 speaker label updated.'
            : `${res.applied} speaker labels updated.`,
        );
      } else {
        setDetailError(res.error ?? 'Speaker re-run failed');
      }
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : 'Speaker re-run failed');
    } finally {
      setRerunningSpeakers(false);
    }
  };

  const exportMeeting = (fmt: ExportFormat, meetingId: string) => {
    window.open(api.exportUrl(token, fmt, meetingId), '_blank');
  };

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

  const handleEvidenceClick = useCallback((segmentId: string) => {
    setHighlightSegmentId(segmentId);
  }, []);

  return (
    <section className="panel" style={{ minHeight: '70vh' }}>
      <div className="panel-header no-print">
        <span>Meeting history</span>
        <button type="button" className="ghost" onClick={onClose}>
          Back to live
        </button>
      </div>
      <div className="panel-body">
        {error && (
          <div className="banner warning no-print" role="alert">
            {error}
          </div>
        )}

        <div className="history-search no-print" style={{ display: 'flex', gap: 8, marginBottom: 16 }}>
          <input
            type="search"
            placeholder="Search transcripts…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') runSearch();
            }}
          />
          <button type="button" className="primary" onClick={runSearch}>
            Search
          </button>
        </div>

        {searchResults.length > 0 && (
          <div className="search-results no-print">
            <h3 className="card-section-title">Search results</h3>
            {searchResults.map((row, i) => (
              <button key={i} type="button" className="search-hit" onClick={() => selectSearchResult(row)}>
                {Object.entries(row).map(([k, v]) => (
                  <div key={k}>
                    <strong>{k}:</strong> {String(v)}
                  </div>
                ))}
              </button>
            ))}
          </div>
        )}

        {loading ? (
          <p className="empty-state">Loading meetings…</p>
        ) : meetings.length === 0 ? (
          <p className="empty-state">No past meetings.</p>
        ) : (
          <div className="history-detail-grid" style={{ display: 'grid', gridTemplateColumns: '280px 1fr', gap: 16 }}>
            <ul className="history-list no-print">
              {meetings.map((m) => (
                <li
                  key={m.id}
                  className={`history-item${selectedId === m.id ? ' active' : ''}`}
                  onClick={() => setSelectedId(m.id)}
                >
                  <div
                    className="history-item-title"
                    title={String(
                      m.display_title
                      || m.title
                      || (m.status === 'failed' ? 'Failed meeting' : 'Untitled meeting'),
                    )}
                  >
                    {String(
                      m.display_title
                      || m.title
                      || (m.status === 'failed' ? 'Failed meeting' : 'Untitled meeting'),
                    )}
                  </div>
                  <div className="history-item-meta">
                    {m.started_at ? new Date(String(m.started_at)).toLocaleString() : '—'}
                  </div>
                  {m.insights_pill && (
                    <span
                      className={`status-chip insights-pill${
                        m.insights_tone ? ` insights-${m.insights_tone}` : ''
                      }`}
                    >
                      {String(m.insights_pill)}
                    </span>
                  )}
                </li>
              ))}
            </ul>

            {selected && (
              <div>
                <h3 className="no-print" style={{ marginTop: 0 }}>Details</h3>
                {selected.content_summary?.is_empty && (
                  <div className="banner warning no-print" role="status">
                    No audio or transcript was captured for this meeting.
                  </div>
                )}
                <div className="no-print" style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
                  <input
                    value={renameDraft}
                    onChange={(e) => setRenameDraft(e.target.value)}
                    aria-label="Meeting title"
                  />
                  <button type="button" onClick={renameMeeting}>
                    Rename
                  </button>
                </div>
                <div className="history-actions no-print" style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
                  <button type="button" onClick={() => exportMeeting('md', selected.id)}>
                    Export Markdown
                  </button>
                  <button type="button" onClick={() => exportMeeting('json', selected.id)}>
                    Export JSON
                  </button>
                  <button type="button" onClick={() => exportMeeting('txt', selected.id)}>
                    Export transcript
                  </button>
                  <button
                    type="button"
                    onClick={() => rerunInsights(selected.id)}
                    disabled={rerunning || rerunningSpeakers}
                  >
                    {rerunning ? 'Re-running insights…' : 'Re-run insights'}
                  </button>
                  <button
                    type="button"
                    onClick={() => rerunSpeakers(selected.id)}
                    disabled={
                      rerunning
                      || rerunningSpeakers
                      || selected.can_rerun_speakers === false
                    }
                    title={
                      selected.can_rerun_speakers === false
                        ? 'No system-audio recording is available for speaker identification'
                        : undefined
                    }
                  >
                    {rerunningSpeakers
                      ? 'Re-running speakers…'
                      : 'Re-run speaker identification'}
                  </button>
                  <button type="button" className="danger" onClick={() => setPendingDeleteId(selected.id)}>
                    Delete
                  </button>
                </div>

                {(rerunning || rerunningSpeakers) && (
                  <p className="no-print" style={{ color: 'var(--text-muted)', fontSize: 13, margin: '0 0 12px' }}>
                    {rerunningSpeakers
                      ? 'Relabeling speakers — this can take a minute.'
                      : 'Re-analyzing the transcript — this can take a minute.'}
                  </p>
                )}
                {rerunNote && (
                  <p className="no-print" style={{ color: 'var(--text-muted)', fontSize: 13, margin: '0 0 12px' }}>
                    {rerunNote}
                  </p>
                )}
                {detailError && (
                  <p className="no-print" style={{ color: 'var(--danger)', fontSize: 13, margin: '0 0 12px' }}>
                    {detailError}
                  </p>
                )}

                {!detail ? (
                  <p style={{ color: 'var(--text-muted)', fontSize: 13, margin: '0 0 16px' }}>
                    Loading…
                  </p>
                ) : (
                  <ReportTabs
                    state={detail}
                    segments={detailSegments}
                    meeting={selected}
                    onEvidenceClick={handleEvidenceClick}
                    onSeek={seekTo}
                    transcriptComplete={transcriptComplete}
                  />
                )}

                <section className="panel no-print" style={{ marginTop: 16 }}>
                  <div className="panel-header"><span>Recording</span></div>
                  <div className="panel-body">
                    {selected.has_audio === false ? (
                      <p className="empty-state">No audio was captured.</p>
                    ) : (
                      <audio
                        ref={audioRef}
                        controls
                        preload="metadata"
                        src={api.audioUrl(token, selected.id)}
                      />
                    )}
                  </div>
                </section>

                <div className="no-print">
                  <TranscriptPane
                    segments={detailSegments}
                    participants={detail ? Object.values(detail.participants) : []}
                    highlightSegmentId={highlightSegmentId}
                    onHighlightClear={() => setHighlightSegmentId(null)}
                    onReassignSpeaker={() => undefined}
                    readOnly
                  />
                </div>

                <p className="no-print" style={{ color: 'var(--text-muted)', fontSize: 13 }}>
                  ID: {selected.id}
                </p>
              </div>
            )}
          </div>
        )}
      </div>

      <ConfirmDialog
        open={pendingDeleteId !== null}
        title="Delete this meeting?"
        message="This permanently removes the meeting and all of its recordings, transcripts, and notes."
        confirmLabel="Delete meeting"
        cancelLabel="Keep meeting"
        danger
        busy={deleting}
        onCancel={() => {
          if (!deleting) setPendingDeleteId(null);
        }}
        onConfirm={() => {
          if (pendingDeleteId) void deleteMeeting(pendingDeleteId);
        }}
      />
    </section>
  );
}
