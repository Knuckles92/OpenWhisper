import EvidenceChip from './EvidenceChip';

interface TopicHeroProps {
  meetingTitle: string;
  status: string;
  topic: string;
  topicEvidence: string[];
  summary: string;
  summaryEvidence: string[];
  onEvidenceClick: (segmentId: string) => void;
}

function statusLabel(status: string): string {
  if (status === 'active') return 'Live';
  if (status === 'paused') return 'Paused';
  if (status === 'ended') return 'Ended';
  return status.replace(/_/g, ' ');
}

export default function TopicHero({
  meetingTitle,
  status,
  topic,
  topicEvidence,
  summary,
  summaryEvidence,
  onEvidenceClick,
}: TopicHeroProps) {
  const pulseClass =
    status === 'active' ? 'pulse' : status === 'paused' ? 'pulse paused' : 'pulse ended';
  const evidence = [...topicEvidence, ...summaryEvidence];
  const uniqueEvidence = [...new Set(evidence)];

  return (
    <section className="topic-hero">
      <div className="topic-hero-eyebrow">
        <i className={pulseClass} aria-hidden />
        {statusLabel(status)}
        {meetingTitle ? ` · ${meetingTitle}` : ''}
      </div>
      <h1>{topic || 'Waiting for the discussion to begin…'}</h1>
      <p className="summary">
        {summary || 'Insights will appear here when cloud intelligence is enabled.'}
      </p>
      {uniqueEvidence.length > 0 && (
        <div className="evidence-row">
          {uniqueEvidence.map((segmentId) => (
            <EvidenceChip key={segmentId} segmentId={segmentId} onClick={onEvidenceClick} />
          ))}
        </div>
      )}
    </section>
  );
}
