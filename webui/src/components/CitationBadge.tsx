import type { CardItem } from '../types';

const labels: Record<string, string> = {
  supported: 'Citation supports claim', contradicted: 'Citation conflicts with claim',
  unsupported: 'Check citation', missing: 'Missing citation', uncertain: 'Citation uncertain',
  unavailable: 'Citation check unavailable', stale: 'Citation needs recheck',
};

export default function CitationBadge({ item }: {item: CardItem}) {
  const check = item.citation_check;
  if (!check || check.revision !== item.revision || !labels[check.status]) return null;
  return <span className={`citation-badge citation-${check.status}`}
    title="Advisory check against cited transcript excerpts. This does not certify accuracy or change the insight.">
    {labels[check.status]}
  </span>;
}
