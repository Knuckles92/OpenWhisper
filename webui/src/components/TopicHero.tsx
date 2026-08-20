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
  if (!intelligenceOnline) return 'Cloud intelligence is offline';
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
    return 'Enable cloud insights to generate a live summary.';
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
    <section className="topic-hero">
      <div className="topic-hero-eyebrow">
        <i className={pulseClass} aria-hidden />
        {statusLabel(status)}
        {meetingTitle ? ` · ${meetingTitle}` : ''}
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
