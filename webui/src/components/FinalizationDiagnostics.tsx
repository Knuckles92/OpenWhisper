import type { FinalizationState } from '../types';

export default function FinalizationDiagnostics({ finalization, meetingId }: {
  finalization: FinalizationState | null | undefined;
  meetingId: string;
}) {
  if (!finalization || !['failed', 'unavailable'].includes(finalization.status)) return null;
  const failures = (finalization.steps ?? []).filter(step => step.status === 'failed');
  const reason = [finalization.message, ...failures.map(step => step.detail)].join(' ').toLowerCase();
  const guidance = /timed out|timeout/.test(reason)
    ? 'The cleanup or insights request did not finish within its time limit. Check your connection and provider availability. If this repeats, try a faster meeting intelligence model in the desktop app.'
    : /401|403|api key|unauthorized|authentication/.test(reason)
      ? 'Check the API key and model access for the configured meeting intelligence provider in the desktop app.'
      : /429|rate limit|quota|credit/.test(reason)
        ? 'Check provider quota, credits, and rate limits before retrying.'
        : 'Check the failed step below and the meeting intelligence settings in the desktop app. Use the log details to investigate before retrying.';
  return (
    <details style={{ marginTop: 8, overflowWrap: 'anywhere' }}>
      <summary>Failure details and troubleshooting</summary>
      <p>{guidance}</p>
      <ul>
        {(finalization.steps ?? []).map(step => (
          <li key={step.id}>
            <strong>{step.name}: {step.status}</strong>{step.detail ? ` - ${step.detail}` : ''}
          </li>
        ))}
      </ul>
      {!failures.length && <p>{finalization.step_details || finalization.message || 'No detailed cause was recorded.'}</p>}
      <p>To retry cleanup, use Retry failed steps on the Meeting tab in the desktop app. In browser History, Re-run insights regenerates insights.</p>
      <p>On the computer running OpenWhisper, open <code>openwhisper.log</code> (or rotated <code>openwhisper.log.1</code>) and search for the meeting ID or request ID shown here. Installed Windows apps store logs in <code>%LOCALAPPDATA%\OpenWhisper</code>; source runs store them in the working folder.</p>
      <p>Meeting ID: <code>{meetingId}</code>. Include the failed step and matching log lines when reporting the issue. Review logs before sharing; they may contain private meeting information.</p>
    </details>
  );
}
