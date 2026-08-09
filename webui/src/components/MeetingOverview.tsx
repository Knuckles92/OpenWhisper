import EvidenceChip from './EvidenceChip';

interface MeetingOverviewProps {
  topic: string;
  topicEvidence: string[];
  summary: string;
  summaryEvidence: string[];
  onEvidenceClick: (segmentId: string) => void;
}

export default function MeetingOverview({
  topic,
  topicEvidence,
  summary,
  summaryEvidence,
  onEvidenceClick,
}: MeetingOverviewProps) {
  return (
    <section className="panel">
      <div className="panel-header"><span>Meeting overview</span></div>
      <div className="panel-body">
        <h3 className="card-section-title">Current topic</h3>
        <p style={{ marginTop: 0 }}>{topic || 'Waiting for the discussion to begin…'}</p>
        {topicEvidence.length > 0 && (
          <div className="evidence-row">
            {topicEvidence.map((segmentId) => (
              <EvidenceChip
                key={segmentId}
                segmentId={segmentId}
                onClick={onEvidenceClick}
              />
            ))}
          </div>
        )}
        <h3 className="card-section-title">Rolling summary</h3>
        <p style={{ marginTop: 0, whiteSpace: 'pre-wrap' }}>
          {summary || 'Insights will appear here when cloud intelligence is enabled.'}
        </p>
        {summaryEvidence.length > 0 && (
          <div className="evidence-row">
            {summaryEvidence.map((segmentId) => (
              <EvidenceChip
                key={segmentId}
                segmentId={segmentId}
                onClick={onEvidenceClick}
              />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}
