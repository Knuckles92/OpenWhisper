import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
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
  const reportRef = useRef<HTMLDivElement | null>(null);
  const focusReport = useMemo(
    () => new URLSearchParams(location.search).get('view') === 'report',
    [],
  );

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

  useEffect(() => {
    if (!focusReport || !detail || !reportRef.current) return;
    reportRef.current.scrollIntoView({ block: 'start' });
  }, [focusReport, detail, selectedId]);

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
    <section className="panel history-pane" style={{ minHeight: '70vh' }}>
      <div className="panel-header no-print">
        <div className="history-pane-title">
          <span>Past Meetings</span>
          {!loading && <span className="status-chip">{meetings.length}</span>}
        </div>
      </div>
      <div className="panel-body">
        {error && (
          <div className="banner warning no-print" role="alert">
            {error}
          </div>
        )}

        <div className="history-detail-grid" style={{ display: 'grid', gridTemplateColumns: '300px 1fr', gap: 20 }}>
          <div className="history-sidebar-column no-print">
            <div className="history-search" style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
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
              <div className="search-results">
                <h3 className="card-section-title">Search results ({searchResults.length})</h3>
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
              <p className="empty-state">Loading past meetings…</p>
            ) : meetings.length === 0 ? (
              <p className="empty-state">No past meetings recorded.</p>
            ) : (
              <ul className="history-list">
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
                      <span>{m.started_at ? new Date(String(m.started_at)).toLocaleString() : '—'}</span>
                      {m.insights_pill && (
                        <span
                          className={`status-chip insights-pill${
                            m.insights_tone ? ` insights-${m.insights_tone}` : ''
                          }`}
                        >
                          {String(m.insights_pill)}
                        </span>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="history-content-column">
            {!selected ? (
              <div className="history-empty-detail no-print">
                <h3>Select a meeting</h3>
                <p>Choose a meeting from the list on the left to view its report, audio recording, and transcript.</p>
              </div>
            ) : (
              <div>
                <div className="history-header-box no-print">
                  <div className="history-title-row">
                    <input
                      className="history-title-input"
                      value={renameDraft}
                      onChange={(e) => setRenameDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') renameMeeting();
                      }}
                      placeholder="Meeting title"
                      aria-label="Meeting title"
                    />
                    <button
                      type="button"
                      className={renameDraft !== (selected.title || '') ? 'primary' : 'ghost'}
                      onClick={renameMeeting}
                      disabled={!renameDraft.trim() || renameDraft === (selected.title || '')}
                    >
                      Save title
                    </button>
                  </div>
                  <div className="history-meta-row">
                    <span>
                      📅 {selected.started_at ? new Date(String(selected.started_at)).toLocaleString() : 'Unknown date'}
                    </span>
                    {selected.insights_pill && (
                      <span
                        className={`status-chip insights-pill${
                          selected.insights_tone ? ` insights-${selected.insights_tone}` : ''
                        }`}
                      >
                        {String(selected.insights_pill)}
                      </span>
                    )}
                    <span className="history-meta-id">ID: {selected.id}</span>
                  </div>
                </div>

                <div className="history-actions-bar no-print">
                  <div className="history-action-group">
                    <span className="history-action-group-label">Export:</span>
                    <button type="button" onClick={() => exportMeeting('md', selected.id)}>
                      Markdown
                    </button>
                    <button type="button" onClick={() => exportMeeting('json', selected.id)}>
                      JSON
                    </button>
                    <button type="button" onClick={() => exportMeeting('txt', selected.id)}>
                      Transcript
                    </button>
                  </div>

                  <div className="history-action-group">
                    <span className="history-action-group-label">Intelligence:</span>
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
                  </div>

                  <div className="history-action-group">
                    <button type="button" className="danger" onClick={() => setPendingDeleteId(selected.id)}>
                      Delete
                    </button>
                  </div>
                </div>

                {selected.content_summary?.is_empty && (
                  <div className="banner warning no-print" role="status">
                    No audio or transcript was captured for this meeting.
                  </div>
                )}
                {(rerunning || rerunningSpeakers) && (
                  <div className="banner info no-print" role="status">
                    {rerunningSpeakers
                      ? 'Relabeling speakers — this can take a minute.'
                      : 'Re-analyzing the transcript with cloud intelligence — this can take a minute.'}
                  </div>
                )}
                {rerunNote && (
                  <div className="banner info no-print" role="status">
                    {rerunNote}
                  </div>
                )}
                {detailError && (
                  <div className="banner warning no-print" role="alert">
                    {detailError}
                  </div>
                )}

                {!detail ? (
                  <p className="empty-state">Loading meeting report…</p>
                ) : (
                  <div ref={reportRef} id="history-report">
                    <ReportTabs
                      state={detail}
                      segments={detailSegments}
                      meeting={selected}
                      onEvidenceClick={handleEvidenceClick}
                      onSeek={seekTo}
                      transcriptComplete={transcriptComplete}
                    />
                  </div>
                )}

                <section className="panel no-print" style={{ marginTop: 20 }}>
                  <div className="panel-header"><span>Audio Recording</span></div>
                  <div className="panel-body">
                    {selected.has_audio === false ? (
                      <p className="empty-state">No audio was captured for this meeting.</p>
                    ) : (
                      <audio
                        ref={audioRef}
                        controls
                        preload="metadata"
                        src={api.audioUrl(token, selected.id)}
                        style={{ width: '100%' }}
                      />
                    )}
                  </div>
                </section>

                <section className="panel no-print" style={{ marginTop: 20 }}>
                  <div className="panel-header">
                    <span>Full Transcript</span>
                    <span className="status-chip">{detailSegments.length} segments</span>
                  </div>
                  <div className="panel-body">
                    <TranscriptPane
                      segments={detailSegments}
                      participants={detail ? Object.values(detail.participants) : []}
                      highlightSegmentId={highlightSegmentId}
                      onHighlightClear={() => setHighlightSegmentId(null)}
                      onReassignSpeaker={() => undefined}
                      readOnly
                    />
                  </div>
                </section>
              </div>
            )}
          </div>
        </div>
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
