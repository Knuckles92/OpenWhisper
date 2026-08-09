interface EvidenceChipProps {
  segmentId: string;
  label?: string;
  onClick?: (segmentId: string) => void;
}

export default function EvidenceChip({ segmentId, label, onClick }: EvidenceChipProps) {
  const short = segmentId.length > 10 ? `${segmentId.slice(0, 8)}…` : segmentId;
  return (
    <button
      type="button"
      className="evidence-chip"
      title={`Jump to segment ${segmentId}`}
      onClick={() => onClick?.(segmentId)}
    >
      {label ?? short}
    </button>
  );
}
