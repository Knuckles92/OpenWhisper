import InsightReview from './InsightReview';
import HighlightPulseStrip, { pulseTime } from './HighlightPulseStrip';
import { playMoment } from '../playback';
import RecordingPlayer, { type PlaybackMoment } from './RecordingPlayer';
import FinalizationDiagnostics from './FinalizationDiagnostics';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../api';
import { topicChapters } from '../chapters';
import { initials, speakerColor } from '../people';
import type { CardItem, ExportFormat, MeetingRow, MeetingStateDoc, Participant, SearchRow } from '../types';
import type { Segment } from '../types';
import ConfirmDialog from './ConfirmDialog';
import TranscriptPane from './TranscriptPane';
import ReportTabs from './report/ReportTabs';
import './history.css';

interface HistoryPaneProps {
  token: string;
  initialMeetingId?: string | null;
  /** Lifted selection, so the header can act on the meeting being viewed. */
  selectedId?: string | null;
  onSelectMeeting?: (meetingId: string | null) => void;
  /** Full view: the list steps aside and the selected meeting fills the pane. */
  focused?: boolean;
  onFocusChange?: (focused: boolean) => void;
  onClose: () => void;
}

type DateRange = 'all' | '7' | '30' | 'year';

const DAY_MS = 24 * 60 * 60 * 1000;

function meetingTitle(m: MeetingRow): string {
  return String(m.display_title || m.title || (m.status === 'failed' ? 'Failed meeting' : 'Untitled meeting'));
}

function startedDate(m: MeetingRow): Date | null {
  if (!m.started_at) return null;
  const date = new Date(String(m.started_at));
  return Number.isNaN(date.getTime()) ? null : date;
}

function startOfDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

/** Monday-based week start, local time. */
function startOfWeek(date: Date): Date {
  const day = startOfDay(date);
  return new Date(day.getTime() - ((day.getDay() + 6) % 7) * DAY_MS);
}

/** "This week", "Last week", then month buckets ("Earlier in September", "August 2025"). */
export function weekGroup(date: Date | null, now = new Date()): string {
  if (!date) return 'Undated';
  const thisWeek = startOfWeek(now);
  if (date >= thisWeek) return 'This week';
  if (date >= new Date(thisWeek.getTime() - 7 * DAY_MS)) return 'Last week';
  const month = date.toLocaleString(undefined, { month: 'long' });
  if (date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth()) {
    return `Earlier in ${month}`;
  }
  return date.getFullYear() === now.getFullYear() ? month : `${month} ${date.getFullYear()}`;
}

/** "Today · 9:02 AM", "Yesterday · …", "Thu Sep 17 · 2:00 PM". */
export function friendlyStart(date: Date | null, now = new Date()): string {
  if (!date) return 'Unknown date';
  const time = date.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
  const days = Math.round((startOfDay(now).getTime() - startOfDay(date).getTime()) / DAY_MS);
  if (days === 0) return `Today · ${time}`;
  if (days === 1) return `Yesterday · ${time}`;
  const day = date.toLocaleDateString(undefined, {
    weekday: 'short', month: 'short', day: 'numeric',
    ...(date.getFullYear() === now.getFullYear() ? {} : { year: 'numeric' }),
  });
  return `${day} · ${time}`;
}

export function shortDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '';
  const minutes = Math.round(seconds / 60);
  if (minutes < 1) return '<1m';
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function liveItems(items: CardItem[] | undefined): CardItem[] {
  return (items ?? []).filter((item) => item.status !== 'removed');
}

/** Small shape glyphs shared with the live dashboard's insight kinds. */
function KindGlyph({ kind }: { kind: 'decision' | 'action' | 'risk' | 'question' }) {
  return (
    <svg className={`shelf-glyph shelf-glyph-${kind}`} width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
      {kind === 'decision' && <path d="M5 0l5 5-5 5-5-5z" fill="currentColor" />}
      {kind === 'action' && <rect x="0.75" y="0.75" width="8.5" height="8.5" rx="2" fill="none" stroke="currentColor" strokeWidth="1.5" />}
      {kind === 'risk' && <path d="M5 0.8l4.3 8.4H0.7z" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />}
      {kind === 'question' && <circle cx="5" cy="5" r="4.2" fill="none" stroke="currentColor" strokeWidth="1.5" />}
    </svg>
  );
}

function AvatarStack({ people, total }: { people: Participant[]; total: number }) {
  if (!people.length) return <span className="shelf-avatars shelf-avatars-empty" aria-hidden="true" />;
  const extra = Math.max(0, total - people.slice(0, 4).length);
  const names = people.map((p) => p.display_name || 'Unnamed').join(', ');
  return (
    <span className="shelf-avatars" role="img" aria-label={`${total} ${total === 1 ? 'person' : 'people'}: ${names}`}>
      {people.slice(0, 4).map((person) => (
        <span key={person.id} className="shelf-avatar" style={{ background: speakerColor(person.id, people) }}>
          {initials(person.display_name || '?')}
        </span>
      ))}
      {extra > 0 && <span className="shelf-avatar shelf-avatar-more">+{extra}</span>}
    </span>
  );
}

function StatusPill({ meeting }: { meeting: MeetingRow }) {
  if (!meeting.insights_pill) return null;
  return (
    <span className={`shelf-pill${meeting.insights_tone ? ` shelf-pill-${meeting.insights_tone}` : ''}`}>
      {String(meeting.insights_pill)}
    </span>
  );
}

function digestPeople(m: MeetingRow): Participant[] {
  return (m.digest?.participants ?? []).map((p) => ({
    ...p, name_source: 'default', is_provisional: false, updated_at: p.created_at ?? '',
  })) as Participant[];
}

export default function HistoryPane({
  token,
  initialMeetingId,
  selectedId: controlledSelectedId,
  onSelectMeeting,
  focused: controlledFocused,
  onFocusChange,
  onClose,
}: HistoryPaneProps) {
  const [meetings, setMeetings] = useState<MeetingRow[]>([]);
  const [ownSelectedId, setOwnSelectedId] = useState<string | null>(initialMeetingId ?? null);
  const [ownFocused, setOwnFocused] = useState(false);
  // Controlled when the dashboard drives it, self-contained when mounted alone.
  const selectedId = controlledSelectedId !== undefined ? controlledSelectedId : ownSelectedId;
  const focused = controlledFocused !== undefined ? controlledFocused : ownFocused;
  const selectMeeting = useCallback((meetingId: string | null) => {
    setOwnSelectedId(meetingId);
    onSelectMeeting?.(meetingId);
  }, [onSelectMeeting]);
  const setFocused = useCallback((next: boolean) => {
    setOwnFocused(next);
    onFocusChange?.(next);
  }, [onFocusChange]);
  const [query, setQuery] = useState('');
  const [searchMode, setSearchMode] = useState<'keyword' | 'semantic'>('semantic');
  const searchGeneration = useRef(0);
  const [searchResults, setSearchResults] = useState<SearchRow[]>([]);
  const [searchStatus, setSearchStatus] = useState('');
  const [searching, setSearching] = useState(false);
  const [dateRange, setDateRange] = useState<DateRange>('all');
  const [onlyDecisions, setOnlyDecisions] = useState(false);
  const [onlyActions, setOnlyActions] = useState(false);
  const [onlyAttention, setOnlyAttention] = useState(false);
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
  const playOnOpen = useRef(false);
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

  // A deep link straight to a report opens the full view, where the report lives.
  useEffect(() => {
    if (focusReport && initialMeetingId) setFocused(true);
    // Mount-only: later selections should not re-open the full view.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  // History has no live socket, so anything running server-side is followed
  // by polling. A boolean dep keeps the interval stable across refreshes.
  const workRunning = detail?.insight_review?.status === 'running'
    || (detail?.custom_reports ?? []).some((report) => report.status === 'running');

  useEffect(() => {
    if (!selectedId || !workRunning) return;
    let cancelled = false;
    const timer = setInterval(() => {
      api.meeting(token, selectedId).then(response => {
        if (!cancelled) setDetail(response.state);
      }).catch(() => { if (!cancelled) setDetailError('Could not refresh this meeting. Reopen it to reconnect.'); });
    }, 2000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [token, selectedId, workRunning]);

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
      // Escape steps back out of the full view before it leaves history.
      if (focused) {
        setFocused(false);
        return;
      }
      onClose();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [onClose, pendingDeleteId, focused, setFocused]);

  useEffect(() => {
    if (!focused) return;
    const scroller = document.querySelector('.app-main');
    if (scroller) scroller.scrollTop = 0;
  }, [focused, selectedId]);

  // "Play audio" from the preview opens the full view, whose player it needs.
  useEffect(() => {
    if (!focused || !playOnOpen.current || !audioRef.current) return;
    playOnOpen.current = false;
    setPlaybackMoment({ start_s: 0, text: 'Playing from the start of the meeting.' });
    playMoment(audioRef.current, 0);
  }, [focused, detail]);

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

  const changeSearchMode = (mode: 'keyword' | 'semantic') => {
    ++searchGeneration.current;
    setSearchStatus('');
    setSearchMode(mode);
  };

  const selectSearchResult = (row: SearchRow) => {
    const meetingId = String(row.meeting_id ?? '');
    const segmentId = String(row.segment_id ?? '');
    if (!meetingId) return;
    selectMeeting(meetingId);
    setHighlightSegmentId(segmentId || null);
  };

  const selected = meetings.find((m) => m.id === selectedId);

  useEffect(() => {
    if (selected) setRenameDraft(String(selected.title ?? ''));
  }, [selected]);

  useEffect(() => {
    if (!focusReport || !detail || !reportRef.current) return;
    reportRef.current.scrollIntoView({ block: 'start' });
  }, [focusReport, detail, selectedId, focused]);

  const filtered = useMemo(() => {
    const now = new Date();
    const cutoff = dateRange === '7' ? new Date(now.getTime() - 7 * DAY_MS)
      : dateRange === '30' ? new Date(now.getTime() - 30 * DAY_MS)
        : dateRange === 'year' ? new Date(now.getFullYear(), 0, 1)
          : null;
    return meetings.filter((m) => {
      const date = startedDate(m);
      if (cutoff && (!date || date < cutoff)) return false;
      if (onlyDecisions && !(m.digest?.decisions)) return false;
      if (onlyActions && !(m.digest?.action_items)) return false;
      if (onlyAttention && m.insights_tone !== 'warning') return false;
      return true;
    });
  }, [meetings, dateRange, onlyDecisions, onlyActions, onlyAttention]);

  const groups = useMemo(() => {
    const now = new Date();
    const out: Array<{ label: string; rows: MeetingRow[] }> = [];
    for (const m of filtered) {
      const label = weekGroup(startedDate(m), now);
      const last = out[out.length - 1];
      if (last && last.label === label) last.rows.push(m);
      else out.push({ label, rows: [m] });
    }
    return out;
  }, [filtered]);

  const longest = useMemo(
    () => Math.max(1, ...filtered.map((m) => Number(m.duration_s) || 0)),
    [filtered],
  );
  const filtersActive = dateRange !== 'all' || onlyDecisions || onlyActions || onlyAttention;

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
      if (selectedId === meetingId) {
        selectMeeting(null);
        setFocused(false);
      }
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

  const detailPeople = useMemo(
    () => (detail ? Object.values(detail.participants ?? {}) : []),
    [detail],
  );
  const chapters = useMemo(
    () => (detail && selected
      ? topicChapters(detail.topic?.history ?? [], selected.started_at, selected.duration_s ?? undefined)
      : []),
    [detail, selected],
  );

  const selectedTitle = selected ? meetingTitle(selected) : '';
  const selectedStart = selected ? startedDate(selected) : null;
  const peopleCount = detail ? detailPeople.length : selected?.digest?.participant_count ?? 0;
  const metaParts = selected ? [
    friendlyStart(selectedStart),
    shortDuration(selected.duration_s),
    peopleCount ? `${peopleCount} ${peopleCount === 1 ? 'person' : 'people'}` : '',
  ].filter(Boolean) : [];
  const personName = (id: unknown) =>
    detailPeople.find((person) => person.id === id)?.display_name || '';

  const renderPreview = () => {
    if (!selected) {
      return (
        <div className="shelf-preview-empty">
          <h3>Select a meeting</h3>
          <p>Pick a meeting to see its summary, decisions and open actions.</p>
        </div>
      );
    }
    const decisions = liveItems(detail?.cards?.decisions);
    const actions = liveItems(detail?.cards?.action_items);
    return (
      <>
        <div className="shelf-preview-head">
          <p className="shelf-preview-meta">{metaParts.join(' · ')}</p>
          <h3 className="shelf-preview-title">{selectedTitle}</h3>
          <StatusPill meeting={selected} />
        </div>
        {detailError && <div className="banner warning" role="alert">{detailError}</div>}
        {!detail ? (
          !detailError && <p className="shelf-muted" role="status" aria-live="polite">Loading meeting…</p>
        ) : (
          <>
            {selected.content_summary?.is_empty ? (
              <p className="shelf-muted">No audio or transcript was captured for this meeting.</p>
            ) : detail.rolling_summary ? (
              <p className="shelf-preview-summary">{detail.rolling_summary}</p>
            ) : (
              <p className="shelf-muted">No summary was saved for this meeting.</p>
            )}
            {decisions.length > 0 && (
              <section className="shelf-preview-section" aria-label="Decisions">
                <h4 className="shelf-kind shelf-kind-decision">
                  <KindGlyph kind="decision" />{decisions.length === 1 ? 'Decision' : 'Decisions'}
                  <span className="shelf-count">{decisions.length}</span>
                </h4>
                <ul>
                  {decisions.slice(0, 3).map((item) => <li key={item.id}>{item.text}</li>)}
                </ul>
                {decisions.length > 3 && <p className="shelf-more">+{decisions.length - 3} more in the report</p>}
              </section>
            )}
            {actions.length > 0 && (
              <section className="shelf-preview-section" aria-label="Open actions">
                <h4 className="shelf-kind shelf-kind-action">
                  <KindGlyph kind="action" />Open actions<span className="shelf-count">{actions.length}</span>
                </h4>
                <ul>
                  {actions.slice(0, 4).map((item) => {
                    const owner = personName(item.data?.owner_participant_id);
                    const due = String(item.data?.deadline || item.data?.due_date || '');
                    return (
                      <li key={item.id} className="shelf-action">
                        <span>{item.text}</span>
                        {(owner || due) && (
                          <span className="shelf-action-meta">{[owner, due].filter(Boolean).join(' · ')}</span>
                        )}
                      </li>
                    );
                  })}
                </ul>
                {actions.length > 4 && <p className="shelf-more">+{actions.length - 4} more in the report</p>}
              </section>
            )}
          </>
        )}
        <div className="shelf-preview-actions">
          <button type="button" className="primary" onClick={() => setFocused(true)}>
            Open meeting
          </button>
          {selected.has_audio !== false && (
            <button type="button" onClick={() => { playOnOpen.current = true; setFocused(true); }}>
              <svg width="10" height="10" viewBox="0 0 12 12" aria-hidden="true"><path d="M3 2l7 4-7 4z" fill="currentColor" /></svg>
              Play audio
            </button>
          )}
        </div>
      </>
    );
  };

  return (
    <section className="history-pane" aria-labelledby="meeting-history-heading">
      <div className="shelf-header no-print">
        <div className="history-pane-title">
          {focused ? (
            <>
              <button
                type="button"
                className="ghost shelf-back"
                onClick={() => setFocused(false)}
                aria-label="Back to the meeting list"
              >
                ← All meetings
              </button>
              <span id="meeting-history-heading" className="shelf-heading-focused">
                {selectedTitle || 'Meeting'}
              </span>
            </>
          ) : (
            <>
              <h2 id="meeting-history-heading" className="shelf-heading">Past meetings</h2>
              {!loading && (
                <span className="shelf-heading-count">
                  {filtersActive ? `${filtered.length} of ${meetings.length}` : meetings.length}{' '}
                  {meetings.length === 1 ? 'meeting' : 'meetings'}
                </span>
              )}
            </>
          )}
        </div>
        <button type="button" className="ghost" onClick={onClose} aria-label="Close meeting history">
          Close
        </button>
      </div>

      {error && (
        <div className="banner warning no-print" role="alert">
          {error}
        </div>
      )}

      <div className={`history-detail-grid${focused ? ' focused' : ''}`}>
        {!focused && (
        <div className="history-sidebar-column no-print">
          <aside className="shelf-filters" aria-label="Find meetings">
            <div className="shelf-field">
              <label className="shelf-label" htmlFor="shelf-search">Search</label>
              <div className="history-search">
                <input
                  id="shelf-search"
                  type="search"
                  placeholder="Ask or search…"
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
              <div className="shelf-segmented" role="group" aria-label="Search mode">
                <button type="button" aria-pressed={searchMode === 'semantic'} onClick={() => changeSearchMode('semantic')}>
                  Meaning
                </button>
                <button type="button" aria-pressed={searchMode === 'keyword'} onClick={() => changeSearchMode('keyword')}>
                  Keywords
                </button>
              </div>
              <p className="search-status" role="status" aria-live="polite">
                {searchStatus}
              </p>
            </div>

            {searchResults.length > 0 && (
              <div className="search-results">
                <h3 className="shelf-label">Search results ({searchResults.length})</h3>
                {searchResults.map((row, i) => (
                  <button key={i} type="button" className="search-hit" onClick={() => selectSearchResult(row)}>
                    <strong>{String(row.title || 'Untitled meeting')}</strong>
                    <span>{pulseTime(Number(row.start_s) || 0)} · {String(row.started_at || '').slice(0, 10)}</span>
                    <p>{String(row.snippet || row.text || '')}</p>
                  </button>
                ))}
              </div>
            )}

            <div className="shelf-field">
              <label className="shelf-label" htmlFor="shelf-range">Date range</label>
              <select id="shelf-range" value={dateRange} onChange={(e) => setDateRange(e.target.value as DateRange)}>
                <option value="all">All time</option>
                <option value="7">Last 7 days</option>
                <option value="30">Last 30 days</option>
                <option value="year">This year</option>
              </select>
            </div>

            <fieldset className="shelf-field shelf-checks">
              <legend className="shelf-label">Show only</legend>
              <label><input type="checkbox" checked={onlyDecisions} onChange={(e) => setOnlyDecisions(e.target.checked)} />Has decisions</label>
              <label><input type="checkbox" checked={onlyActions} onChange={(e) => setOnlyActions(e.target.checked)} />Has action items</label>
              <label><input type="checkbox" checked={onlyAttention} onChange={(e) => setOnlyAttention(e.target.checked)} />Needs attention</label>
            </fieldset>

            <p className="shelf-footnote">Recordings and transcripts stay on this PC.</p>
          </aside>

          <div className="shelf-list-column">
            {loading ? (
              <p className="empty-state" role="status" aria-live="polite">Loading past meetings…</p>
            ) : meetings.length === 0 ? (
              <p className="empty-state">No past meetings recorded.</p>
            ) : filtered.length === 0 ? (
              <p className="empty-state">No meetings match these filters.</p>
            ) : groups.map((group) => (
              <section key={group.label} className="shelf-group" aria-label={group.label}>
                <h3 className="shelf-label shelf-group-label">{group.label}</h3>
                <ul className="history-list">
                  {group.rows.map((m) => {
                    const digest = m.digest;
                    const duration = Number(m.duration_s) || 0;
                    return (
                    <li key={m.id}>
                      <button
                        type="button"
                        className={`history-item${selectedId === m.id ? ' active' : ''}`}
                        aria-current={selectedId === m.id ? 'true' : undefined}
                        onClick={() => selectMeeting(m.id)}
                      >
                        <span className="shelf-row-main">
                          <span className="history-item-title" title={meetingTitle(m)}>{meetingTitle(m)}</span>
                          <span className="shelf-row-date">{friendlyStart(startedDate(m))}</span>
                        </span>
                        <span className="shelf-row-duration">
                          {duration > 0 && (
                            <>
                              <span className="shelf-bar" aria-hidden="true">
                                <span style={{ width: `${Math.max(6, (duration / longest) * 100)}%` }} />
                              </span>
                              <span className="shelf-mono">{shortDuration(duration)}</span>
                            </>
                          )}
                        </span>
                        <AvatarStack people={digestPeople(m)} total={digest?.participant_count ?? 0} />
                        <span className="shelf-counts">
                          {!!digest?.decisions && <span title="Decisions"><KindGlyph kind="decision" />{digest.decisions}<span className="sr-only"> decisions</span></span>}
                          {!!digest?.action_items && <span title="Action items"><KindGlyph kind="action" />{digest.action_items}<span className="sr-only"> action items</span></span>}
                          {!!digest?.risks && <span title="Risks"><KindGlyph kind="risk" />{digest.risks}<span className="sr-only"> risks</span></span>}
                          {!!digest?.open_questions && <span title="Open questions"><KindGlyph kind="question" />{digest.open_questions}<span className="sr-only"> open questions</span></span>}
                        </span>
                        <span className="shelf-row-status"><StatusPill meeting={m} /></span>
                      </button>
                    </li>
                    );
                  })}
                </ul>
              </section>
            ))}
          </div>
        </div>
        )}

        {!focused ? (
          <aside className="shelf-preview no-print" aria-label="Selected meeting preview">
            {renderPreview()}
          </aside>
        ) : (
          <div className="history-content-column">
            {!selected ? (
              <div className="shelf-preview-empty no-print">
                <h3>Select a meeting</h3>
                <p>Choose a meeting from the list to view its report, audio recording, and transcript.</p>
              </div>
            ) : (
              <div className="shelf-detail">
                <div className="shelf-detail-head no-print">
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
                  <div className="shelf-detail-meta">
                    <span>{metaParts.join(' · ') || 'Unknown date'}</span>
                    <StatusPill meeting={selected} />
                    <span className="shelf-detail-id">ID {selected.id}</span>
                  </div>
                </div>

                <div className="shelf-toolbar no-print" role="toolbar" aria-label="Meeting tools">
                  <div className="shelf-tool-group">
                    <span className="shelf-label">Export</span>
                    <div className="shelf-tool-buttons">
                      <button type="button" onClick={() => exportMeeting('md', selected.id)}>Markdown</button>
                      <button type="button" onClick={() => exportMeeting('json', selected.id)}>JSON</button>
                      <button type="button" onClick={() => exportMeeting('txt', selected.id)}>Transcript</button>
                    </div>
                  </div>
                  <div className="shelf-tool-group">
                    <span className="shelf-label">Intelligence</span>
                    <div className="shelf-tool-buttons">
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
                        aria-describedby={selected.can_rerun_speakers === false ? 'shelf-speakers-why' : undefined}
                      >
                        {rerunningSpeakers
                          ? 'Re-running speakers…'
                          : 'Re-run speaker identification'}
                      </button>
                    </div>
                    {selected.can_rerun_speakers === false && (
                      <p id="shelf-speakers-why" className="shelf-tool-note">
                        Speaker identification needs a system-audio recording, and this meeting has none.
                      </p>
                    )}
                  </div>
                  <div className="shelf-tool-group shelf-tool-danger">
                    <button type="button" className="danger" onClick={() => setPendingDeleteId(selected.id)}>
                      Delete meeting
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

                {detail && <HighlightPulseStrip pulses={detail.live_highlights ?? []}
                  playbackAvailable={selected.has_audio !== false}
                  chapters={chapters}
                  segments={detailSegments}
                  participants={detailPeople}
                  durationS={selected.duration_s ?? undefined}
                  onSelect={pulse => {
                    setPlaybackMoment(pulse);
                    setHighlightSegmentId(pulse.segment_id);
                    playMoment(audioRef.current, pulse.start_s);
                  }} />}

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
                      audioRef={selected.has_audio === false ? undefined : audioRef}
                      audioKey={selected.id}
                      transcriptComplete={transcriptComplete}
                      token={token}
                      onState={setDetail}
                    />
                  </div>
                )}

                <section className="shelf-section no-print" aria-labelledby="history-audio-heading">
                  <h3 id="history-audio-heading" className="shelf-label">Audio recording</h3>
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
                      onPopOut={setPlaybackMoment}
                    />
                  )}
                </section>

                <section className="shelf-section no-print" aria-labelledby="history-transcript-heading">
                  <div className="shelf-section-head">
                    <h3 id="history-transcript-heading" className="shelf-label">Full transcript</h3>
                    <span className="shelf-muted">{detailSegments.length} segments</span>
                  </div>
                  <TranscriptPane
                    segments={detailSegments}
                    participants={detailPeople}
                    highlightSegmentId={highlightSegmentId}
                    onHighlightClear={() => setHighlightSegmentId(null)}
                    onReassignSpeaker={() => undefined}
                    audioRef={selected.has_audio === false ? undefined : audioRef}
                    audioKey={selected.id}
                    onPlaySegment={selected.has_audio === false ? undefined : (startSeconds) => {
                      const audio = audioRef.current;
                      playMoment(audio, startSeconds, audio && audio.readyState >= 1
                        ? undefined
                        : api.audioUrl(token, selected.id));
                    }}
                    readOnly
                  />
                </section>
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
