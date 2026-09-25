import { EvidenceRow } from './EvidenceChip';

interface TopicHeroProps {
  meetingTitle: string;
  status: string;
  topic: string;
  topicEvidence: string[];
  summary: string;
  summaryEvidence: string[];
  cloudEnabled: boolean;
  intelligenceOnline: boolean;
  onEvidenceClick: (segmentId: string) => void;
}

function statusLabel(status: string): string {
  if (status === 'active') return 'Live';
  if (status === 'paused') return 'Paused';
  if (status === 'ending') return 'Ending';
  if (status === 'ended') return 'Ended';
  return status.replace(/_/g, ' ');
}

function topicPlaceholder(
  status: string,
  cloudEnabled: boolean,
  intelligenceOnline: boolean,
): string {
  if (status === 'ending') return 'Wrapping up insights…';
  if (!cloudEnabled) return 'Waiting for the discussion to begin…';
  if (!intelligenceOnline) return 'AI insights are offline';
  return 'Listening for insights…';
}

function summaryPlaceholder(
  status: string,
  cloudEnabled: boolean,
  intelligenceOnline: boolean,
): string {
  if (status === 'ending') {
    return 'Final insights are being generated from the full transcript…';
  }
  if (!cloudEnabled) {
    return 'Turn on AI insights to generate a live summary.';
  }
  if (!intelligenceOnline) {
    return 'Transcript continues; insights resume when intelligence is online.';
  }
  return 'Insights update as the conversation develops.';
}

export default function TopicHero({
  meetingTitle,
  status,
  topic,
  topicEvidence,
  summary,
  summaryEvidence,
  cloudEnabled,
  intelligenceOnline,
  onEvidenceClick,
}: TopicHeroProps) {
  const pulseClass =
    status === 'active'
      ? 'pulse'
      : status === 'paused'
        ? 'pulse paused'
        : status === 'ending'
          ? 'pulse paused'
          : 'pulse ended';
  const evidence = [...topicEvidence, ...summaryEvidence];
  const uniqueEvidence = [...new Set(evidence)];

  return (
    <section className="topic-hero" data-status={status}>
      <svg className="topic-art" viewBox="0 0 120 120" fill="none" aria-hidden="true">
        <circle cx="60" cy="60" r="49" />
        <circle cx="60" cy="60" r="37" />
        <circle cx="60" cy="60" r="25" />
        <path d="M48 60h24M60 48v24" />
        <circle className="topic-art-satellite" cx="99" cy="30" r="7" />
      </svg>
      {/* The title already sits in the command bar; the eyebrow names the moment. */}
      <div className="topic-hero-eyebrow" title={meetingTitle || undefined}>
        <i className={pulseClass} aria-hidden />
        {status === 'active' ? 'Now discussing' : `${statusLabel(status)} · Last topic`}
      </div>
      <h1>
        {topic || topicPlaceholder(status, cloudEnabled, intelligenceOnline)}
      </h1>
      <p className="summary">
        {summary ||
          summaryPlaceholder(status, cloudEnabled, intelligenceOnline)}
      </p>
      <EvidenceRow ids={uniqueEvidence} onClick={onEvidenceClick} />
    </section>
  );
}
