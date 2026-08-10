import TopicHero from './TopicHero';

interface MeetingOverviewProps {
  meetingTitle: string;
  status: string;
  topic: string;
  topicEvidence: string[];
  summary: string;
  summaryEvidence: string[];
  onEvidenceClick: (segmentId: string) => void;
}

/** Focus Stage topic hero — current topic and rolling summary. */
export default function MeetingOverview({
  meetingTitle,
  status,
  topic,
  topicEvidence,
  summary,
  summaryEvidence,
  onEvidenceClick,
}: MeetingOverviewProps) {
  return (
    <TopicHero
      meetingTitle={meetingTitle}
      status={status}
      topic={topic}
      topicEvidence={topicEvidence}
      summary={summary}
      summaryEvidence={summaryEvidence}
      onEvidenceClick={onEvidenceClick}
    />
  );
}
