import { useState } from 'react';
import {
  evidenceLabel,
  evidenceTitle,
  sortEvidenceIds,
  useEvidenceLookup,
} from '../evidence';

interface EvidenceChipProps {
  segmentId: string;
  label?: string;
  onClick?: (segmentId: string) => void;
}

/** Clickable citation. Face is the meeting clock when the segment is known. */
export default function EvidenceChip({ segmentId, label, onClick }: EvidenceChipProps) {
  const { segs, participants } = useEvidenceLookup();
  const segment = segs.get(segmentId);
  return (
    <button
      type="button"
      className="evidence-chip"
      title={evidenceTitle(segment, participants)}
      onClick={() => onClick?.(segmentId)}
    >
      {evidenceLabel(segment, label)}
    </button>
  );
}

interface EvidenceRowProps {
  ids: string[];
  onClick?: (segmentId: string) => void;
  /** Visible chips before "+N more". 0 shows every citation (print / archive). */
  limit?: number;
}

const DEFAULT_LIMIT = 4;

/** Sorted, collapsed evidence citations for a card, note, or question. */
export function EvidenceRow({ ids, onClick, limit = DEFAULT_LIMIT }: EvidenceRowProps) {
  const { segs } = useEvidenceLookup();
  const [expanded, setExpanded] = useState(false);
  const sorted = sortEvidenceIds(ids, segs);
  if (!sorted.length) return null;

  const cap = limit > 0 && !expanded ? limit : sorted.length;
  const visible = sorted.slice(0, cap);
  const hidden = sorted.length - visible.length;

  return (
    <div className="evidence-row">
      {visible.map((id) => (
        <EvidenceChip key={id} segmentId={id} onClick={onClick} />
      ))}
      {hidden > 0 && (
        <button type="button" className="evidence-more" onClick={() => setExpanded(true)}>
          +{hidden} more
        </button>
      )}
    </div>
  );
}
