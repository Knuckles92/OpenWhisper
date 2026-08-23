/** Browser print scopes for the post-meeting visual download. */
export type PrintScope = 'summary' | 'full';

/** Strip characters that break Save-as-PDF filenames. */
export function printDocumentTitle(meetingTitle: string, scope: PrintScope, viewLabel?: string): string {
  const title = meetingTitle.replace(/[\\/:*?"<>|]+/g, ' ').replace(/\s+/g, ' ').trim() || 'Meeting';
  if (scope === 'summary' && viewLabel) return `${title} — ${viewLabel}`;
  return `${title} — Meeting`;
}

/**
 * Print the dashboard with a scope class so CSS can show the report sheet
 * and, for full, the complete print-only meeting document.
 */
export function printMeeting(scope: PrintScope, title: string): void {
  const previousTitle = document.title;
  const previousScope = document.body.dataset.printScope;
  document.body.dataset.printScope = scope;
  document.title = title;

  let cleaned = false;
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    if (previousScope === undefined) delete document.body.dataset.printScope;
    else document.body.dataset.printScope = previousScope;
    document.title = previousTitle;
    window.removeEventListener('afterprint', cleanup);
    media.removeEventListener('change', onMedia);
  };

  const media = window.matchMedia('print');
  const onMedia = (event: MediaQueryListEvent) => {
    if (!event.matches) cleanup();
  };
  media.addEventListener('change', onMedia);
  window.addEventListener('afterprint', cleanup);
  window.print();
}
