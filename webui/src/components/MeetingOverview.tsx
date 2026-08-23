import TopicHero from './TopicHero';

interface MeetingOverviewProps {
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

/** Focus Stage topic hero — current topic and rolling summary. */
export default function MeetingOverview({
  meetingTitle,
  status,
  topic,
  topicEvidence,
  summary,
  summaryEvidence,
  cloudEnabled,
  intelligenceOnline,
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
      cloudEnabled={cloudEnabled}
      intelligenceOnline={intelligenceOnline}
      onEvidenceClick={onEvidenceClick}
    />
  );
}
