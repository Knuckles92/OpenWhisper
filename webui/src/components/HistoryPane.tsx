import InsightReview from './InsightReview';
import HighlightPulseStrip, { pulseTime } from './HighlightPulseStrip';
import { playMoment } from '../playback';
import RecordingPlayer, { type PlaybackMoment } from './RecordingPlayer';
import FinalizationDiagnostics from './FinalizationDiagnostics';
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
  const [searchMode, setSearchMode] = useState<'keyword' | 'semantic'>('semantic');
  const searchGeneration = useRef(0);
  const [searchResults, setSearchResults] = useState<SearchRow[]>([]);
  const [searchStatus, setSearchStatus] = useState('');
  const [searching, setSearching] = useState(false);
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
  const [playbackMoment, setPlaybackMoment] = useState<PlaybackMoment | null>(null);
  const reportRef = useRef<HTMLDivElement | null>(null);
  const selectedIdRef = useRef<string | null>(selectedId);
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
    selectedIdRef.current = selectedId;
  }, [selectedId]);

  const loadCompleteMeeting = useCallback(async (
    meetingId: string,
    onPage?: (state: MeetingStateDoc, segments: Segment[], complete: boolean) => void,
  ): Promise<{ state: MeetingStateDoc; segments: Segment[] }> => {
    const response = await api.meeting(token, meetingId);
    let segments = response.segments;
    let cursor = response.transcript_next_cursor ?? undefined;
    onPage?.(response.state, segments, !cursor);
    while (cursor) {
      const page = await api.meetingTranscriptPage(token, meetingId, cursor);
      const byId = new Map(segments.map((segment) => [segment.id, segment]));
      for (const segment of page.items) byId.set(segment.id, segment);
      segments = [...byId.values()].sort(
        (a, b) => a.start_s - b.start_s || a.id.localeCompare(b.id),
      );
      cursor = page.next_cursor ?? undefined;
      onPage?.(response.state, segments, !cursor);
    }
    return { state: response.state, segments };
  }, [token]);

  useEffect(() => {
    if (!selectedId || detail?.insight_review?.status !== 'running') return;
    let cancelled = false;
    const timer = setInterval(() => {
      api.meeting(token, selectedId).then(response => {
        if (!cancelled) setDetail(response.state);
      }).catch(() => { if (!cancelled) setDetailError('Could not refresh the review. Reopen this meeting to reconnect.'); });
    }, 2000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [token, selectedId, detail?.insight_review?.status]);

  useEffect(() => {
    setDetail(null);
    setPlaybackMoment(null);
    setDetailSegments([]);
    setDetailError(null);
    setRerunNote(null);
    setTranscriptComplete(false);
    if (!selectedId) return;
    let cancelled = false;
    loadCompleteMeeting(selectedId, (state, segments, complete) => {
      if (cancelled) return;
      setDetail(state);
      setDetailSegments(segments);
      setTranscriptComplete(complete);
    })
      .then(() => {
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
  }, [loadCompleteMeeting, selectedId]);

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (pendingDeleteId) {
        setPendingDeleteId(null);
        return;
      }
      onClose();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose, pendingDeleteId]);

  const runSearch = async () => {
    if (searching) return;
    const generation = ++searchGeneration.current;
    const q = query.trim();
    if (!q) {
      setSearchResults([]);
      setSearchStatus('');
      return;
    }
    setSearching(true);
    setSearchStatus('Searching meeting transcripts…');
    try {
      const response = await api.searchHistory(token, q, searchMode);
      if (generation !== searchGeneration.current) return;
      setSearchResults(response.results);
      setError(null);
      setSearchStatus(`${response.results.length} results. ${response.message || ''}`);
    } catch (err) {
      if (generation !== searchGeneration.current) return;
      setError(err instanceof Error ? err.message : 'Search failed');
      setSearchStatus('Meeting transcript search failed.');
    } finally {
      setSearching(false);
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
      if (selectedIdRef.current === meetingId) {
        setDetail(res.state);
        if (res.ok) {
          setRerunNote(
            res.applied === 1 ? '1 update applied.' : `${res.applied} updates applied.`,
          );
        } else {
          setDetailError(res.error ?? 'Re-run failed');
        }
        setTranscriptComplete(false);
      }
      // A retry can include audio re-decoding or speaker relabeling, so replace
      // the entire client transcript rather than leaving its old 500-row page.
      try {
        await loadCompleteMeeting(meetingId, (state, segments, complete) => {
          if (selectedIdRef.current !== meetingId) return;
          setDetail(state);
          setDetailSegments(segments);
          setTranscriptComplete(complete);
        });
      } catch (refreshError) {
        if (selectedIdRef.current === meetingId) {
          setDetailError(
            `The re-run finished, but the updated transcript could not be loaded: ${
              refreshError instanceof Error ? refreshError.message : 'refresh failed'
            }`,
          );
        }
      }
      await loadMeetings();
    } catch (err) {
      if (selectedIdRef.current === meetingId) {
        setDetailError(err instanceof Error ? err.message : 'Re-run failed');
      }
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
      if (selectedIdRef.current === meetingId) {
        setDetail(res.state);
        setTranscriptComplete(false);
        if (res.ok) {
          setRerunNote(
            res.applied === 1
              ? '1 speaker label updated.'
              : `${res.applied} speaker labels updated.`,
          );
        } else {
          setDetailError(res.error ?? 'Speaker re-run failed');
        }
      }
      try {
        await loadCompleteMeeting(meetingId, (state, segments, complete) => {
          if (selectedIdRef.current !== meetingId) return;
          setDetail(state);
          setDetailSegments(segments);
          setTranscriptComplete(complete);
        });
      } catch (refreshError) {
        if (selectedIdRef.current === meetingId) {
          setDetailError(
            `Speaker identification finished, but the updated transcript could not be loaded: ${
              refreshError instanceof Error ? refreshError.message : 'refresh failed'
            }`,
          );
        }
      }
      await loadMeetings();
    } catch (err) {
      if (selectedIdRef.current === meetingId) {
        setDetailError(err instanceof Error ? err.message : 'Speaker re-run failed');
      }
    } finally {
      setRerunningSpeakers(false);
    }
  };

  const exportMeeting = (fmt: ExportFormat, meetingId: string) => {
    window.open(api.exportUrl(token, fmt, meetingId), '_blank');
  };

  const seekTo = useCallback((seconds: number) => {
    setPlaybackMoment({ start_s: seconds, text: 'Listen from this point in the meeting.' });
    playMoment(audioRef.current, seconds);
  }, []);

  const handleEvidenceClick = useCallback((segmentId: string) => {
    setHighlightSegmentId(segmentId);
  }, []);

  return (
    <section
      className="panel history-pane"
      style={{ minHeight: '70vh' }}
      aria-labelledby="meeting-history-heading"
    >
      <div className="panel-header no-print">
        <div className="history-pane-title">
          <span id="meeting-history-heading">Past Meetings</span>
          {!loading && <span className="status-chip">{meetings.length}</span>}
        </div>
        <button type="button" className="ghost" onClick={onClose} aria-label="Close meeting history">
          Close
        </button>
      </div>
      <div className="panel-body">
        {error && (
          <div className="banner warning no-print" role="alert">
            {error}
          </div>
        )}

        <div className="history-detail-grid">
          <div className="history-sidebar-column no-print">
            <label className="search-mode">Search by
              <select value={searchMode} aria-label="Search mode" onChange={e => { ++searchGeneration.current; setSearchStatus(''); setSearchMode(e.target.value as 'keyword' | 'semantic'); }}>
                <option value="semantic">Meaning</option><option value="keyword">Keywords</option>
              </select>
            </label>
            <div className="history-search">
              <input
                type="search"
                placeholder="What did we decide about…?"
                aria-label="Search meeting transcripts"
                value={query}
                onChange={(e) => { ++searchGeneration.current; setSearchStatus(''); setQuery(e.target.value); }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') runSearch();
                }}
              />
              <button type="button" className="primary" disabled={searching} onClick={runSearch}>
                Search
              </button>
            </div>
            <p className="search-status" role="status" aria-live="polite">
              {searchStatus}
            </p>

            {searchResults.length > 0 && (
              <div className="search-results">
                <h3 className="card-section-title">Search results ({searchResults.length})</h3>
                {searchResults.map((row, i) => (
                  <button key={i} type="button" className="search-hit" onClick={() => selectSearchResult(row)}>
                    <strong>{String(row.title || 'Untitled meeting')}</strong>
                    <span>{pulseTime(Number(row.start_s) || 0)} · {String(row.started_at || '').slice(0, 10)}</span>
                    <p>{String(row.snippet || row.text || '')}</p>
                  </button>
                ))}
              </div>
            )}

            {loading ? (
              <p className="empty-state" role="status" aria-live="polite">Loading past meetings…</p>
            ) : meetings.length === 0 ? (
              <p className="empty-state">No past meetings recorded.</p>
            ) : (
              <ul className="history-list">
                {meetings.map((m) => (
                  <li key={m.id}>
                    <button
                      type="button"
                      className={`history-item${selectedId === m.id ? ' active' : ''}`}
                      aria-current={selectedId === m.id ? 'true' : undefined}
                      onClick={() => setSelectedId(m.id)}
                    >
                      <span
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
                      </span>
                      <span className="history-item-meta">
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
                      </span>
                    </button>
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

                {detail && <HighlightPulseStrip pulses={detail.live_highlights ?? []} playbackAvailable={selected.has_audio !== false} onSelect={pulse => {
                  setPlaybackMoment(pulse);
                  setHighlightSegmentId(pulse.segment_id);
                  playMoment(audioRef.current, pulse.start_s);
                }} />}
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

                {detail && ['failed', 'unavailable'].includes(detail.finalization?.status ?? '') && (
                  <div className="banner warning no-print" role="status">
                    {detail.finalization?.message || 'Meeting finalization could not finish.'}
                    <FinalizationDiagnostics finalization={detail.finalization} meetingId={selected.id} />
                  </div>
                )}

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
                  <p className="empty-state" role="status" aria-live="polite">Loading meeting report…</p>
                ) : (
                  <div ref={reportRef} id="history-report">
                    <InsightReview key={detail.meeting_id} state={detail} onEvidenceClick={handleEvidenceClick}
                      onSendOp={async op => {
                        const result = await api.review(token, detail.meeting_id, op);
                        setDetail(result.state);
                        return result.ok;
                      }}
                      onRetry={async () => {
                        const result = await api.review(token, detail.meeting_id, {op: 'start'});
                        setDetail(result.state);
                        if (!result.ok) throw new Error(result.error);
                      }} />
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

                <section
                  className="panel no-print"
                  style={{ marginTop: 20 }}
                  aria-labelledby="history-audio-heading"
                >
                  <div className="panel-header"><span id="history-audio-heading">Audio Recording</span></div>
                  <div className="panel-body">
                    {selected.has_audio === false ? (
                      <p className="empty-state">No audio was captured for this meeting.</p>
                    ) : (
                      <RecordingPlayer
                        key={selected.id}
                        audioRef={audioRef}
                        label={`Audio recording for ${selected.display_title || selected.title || 'meeting'}`}
                        src={api.audioUrl(token, selected.id)}
                        moment={playbackMoment}
                        onClose={() => setPlaybackMoment(null)}
                      />
                    )}
                  </div>
                </section>

                <section
                  className="panel no-print"
                  style={{ marginTop: 20 }}
                  aria-labelledby="history-transcript-heading"
                >
                  <div className="panel-header">
                    <span id="history-transcript-heading">Full Transcript</span>
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
