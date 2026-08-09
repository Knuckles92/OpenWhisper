// REST client for the meeting server. Every endpoint authenticates with the
// page token (?token=); the server resolves host vs guest role from it.
import type {
  ExportFormat,
  MeetingDetailResponse,
  MeetingRow,
  RerunInsightsResponse,
  SearchRow,
  Segment,
  SessionResponse,
} from './types';

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) {
    let detail = '';
    try {
      detail = await res.text();
    } catch {
      /* body unavailable */
    }
    throw new ApiError(res.status, detail || `HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

function qs(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  return search.toString();
}

/** Normalize list-shaped responses that may arrive bare or wrapped. */
function asArray<T>(data: unknown, ...keys: string[]): T[] {
  if (Array.isArray(data)) return data as T[];
  if (data && typeof data === 'object') {
    for (const key of keys) {
      const value = (data as Record<string, unknown>)[key];
      if (Array.isArray(value)) return value as T[];
    }
  }
  return [];
}

export const api = {
  session(token: string): Promise<SessionResponse> {
    return request<SessionResponse>(`/api/session?${qs({ token })}`);
  },

  async transcript(token: string, afterStartS = -1, limit?: number): Promise<Segment[]> {
    const data = await request<unknown>(
      `/api/transcript?${qs({ token, after_start_s: afterStartS, limit })}`,
    );
    return asArray<Segment>(data, 'items', 'segments');
  },

  async meetings(token: string): Promise<MeetingRow[]> {
    const data = await request<unknown>(`/api/meetings?${qs({ token })}`);
    return asArray<MeetingRow>(data, 'meetings', 'items');
  },

  meeting(token: string, meetingId: string): Promise<MeetingDetailResponse> {
    return request<MeetingDetailResponse>(
      `/api/meetings/${encodeURIComponent(meetingId)}?${qs({ token })}`,
    );
  },

  renameMeeting(token: string, meetingId: string, title: string): Promise<unknown> {
    return request<unknown>(
      `/api/meetings/${encodeURIComponent(meetingId)}/rename?${qs({ token })}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title }),
      },
    );
  },

  /** Re-run the consolidation pass over a past meeting (host only, slow). */
  rerunInsights(token: string, meetingId: string): Promise<RerunInsightsResponse> {
    return request<RerunInsightsResponse>(
      `/api/meetings/${encodeURIComponent(meetingId)}/reinsights?${qs({ token })}`,
      { method: 'POST' },
    );
  },

  deleteMeeting(token: string, meetingId: string): Promise<unknown> {
    return request<unknown>(`/api/meetings/${encodeURIComponent(meetingId)}?${qs({ token })}`, {
      method: 'DELETE',
    });
  },

  async search(token: string, query: string): Promise<SearchRow[]> {
    const data = await request<unknown>(`/api/search?${qs({ token, q: query })}`);
    return asArray<SearchRow>(data, 'results', 'items');
  },

  exportUrl(token: string, fmt: ExportFormat, meetingId: string): string {
    return `/api/export/${fmt}?${qs({ token, meeting_id: meetingId })}`;
  },

  endMeeting(token: string): Promise<unknown> {
    return request<unknown>(`/api/meeting/end?${qs({ token })}`, { method: 'POST' });
  },

  pauseMeeting(token: string): Promise<unknown> {
    return request<unknown>(`/api/meeting/pause?${qs({ token })}`, { method: 'POST' });
  },

  resumeMeeting(token: string): Promise<unknown> {
    return request<unknown>(`/api/meeting/resume?${qs({ token })}`, { method: 'POST' });
  },

  setCloud(token: string, enabled: boolean): Promise<unknown> {
    return request<unknown>(`/api/meeting/cloud?${qs({ token })}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
  },
};
