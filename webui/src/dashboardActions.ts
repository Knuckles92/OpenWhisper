import type { ActionResultItem, Segment, TranscriptPage } from './types';

export type TranscriptLoadState =
  | { status: 'loading' | 'complete'; error?: never }
  | { status: 'failed'; error: string };

export async function hydrateTranscript(
  fetchPage: (cursor?: string) => Promise<TranscriptPage>,
  onSegments: (segments: Segment[]) => void,
  onState: (state: TranscriptLoadState) => void,
  cancelled: () => boolean,
): Promise<void> {
  onState({ status: 'loading' });
  try {
    let cursor: string | undefined;
    do {
      const page = await fetchPage(cursor);
      if (cancelled()) return;
      onSegments(page.items);
      cursor = page.next_cursor ?? undefined;
    } while (cursor);
    onState({ status: 'complete' });
  } catch {
    if (!cancelled()) {
      onState({
        status: 'failed',
        error: 'The full transcript could not be loaded. Some earlier speech may be missing.',
      });
    }
  }
}

export async function sendDashboardAction(
  send: (() => Promise<ActionResultItem[]>) | null,
  action: 'change' | 'undo',
  onError: (message: string) => void,
): Promise<boolean> {
  const messages = action === 'undo'
    ? {
        offline: 'You are offline. Undo was not sent.',
        unacknowledged: 'The server did not acknowledge undo.',
        rejected: 'Undo was rejected',
        failed: 'Undo could not be sent.',
      }
    : {
        offline: 'You are offline. Your change was not sent.',
        unacknowledged: 'The server did not acknowledge the change.',
        rejected: 'Action was rejected',
        failed: 'Your change could not be sent.',
      };
  if (!send) {
    onError(messages.offline);
    return false;
  }
  try {
    const results = await send();
    if (!results.length) {
      onError(messages.unacknowledged);
      return false;
    }
    const rejected = results.find((result) => !result.ok);
    if (rejected) {
      onError((rejected.reason || messages.rejected).replace(/_/g, ' '));
      return false;
    }
    return true;
  } catch (err) {
    onError(err instanceof Error ? err.message : messages.failed);
    return false;
  }
}
