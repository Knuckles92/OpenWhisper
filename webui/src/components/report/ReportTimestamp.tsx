import { clock } from '../../report';
import type { Segment } from '../../types';

interface ReportTimestampProps {
  evidence: string[];
  segs: Map<string, Segment>;
  limit?: number;
  onEvidenceClick?: (segmentId: string) => void;
  onSeek?: (seconds: number) => void;
}

/** Quiet, clickable timestamps resolved from evidence segment ids. */
export default function ReportTimestamp({
  evidence,
  segs,
  limit = 4,
  onEvidenceClick,
  onSeek,
}: ReportTimestampProps) {
  const ids = (evidence || []).filter((id) => segs.has(id)).slice(0, limit);
  if (!ids.length) return null;
  return (
    <span className="t-group">
      {ids.map((id) => {
        const segment = segs.get(id);
        if (!segment) return null;
        return (
          <button
            key={id}
            type="button"
            className="t"
            onClick={() => {
              onSeek?.(segment.start_s);
              onEvidenceClick?.(id);
            }}
          >
            {clock(segment.start_s)}
          </button>
        );
      })}
    </span>
  );
}
