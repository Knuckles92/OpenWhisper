import { useRef } from 'react';
import { REPORT_VIEW_META, type ReportViewId } from '../../report';

interface ReportViewSelectProps {
  views: ReportViewId[];
  active: ReportViewId;
  onSelect: (view: ReportViewId) => void;
}

export default function ReportViewSelect({ views, active, onSelect }: ReportViewSelectProps) {
  const detailsRef = useRef<HTMLDetailsElement>(null);

  const pick = (view: ReportViewId) => {
    detailsRef.current?.removeAttribute('open');
    onSelect(view);
  };

  return (
    <details ref={detailsRef} className="report-view-select">
      <summary>
        <span>{REPORT_VIEW_META[active]?.label ?? 'View'}</span>
        <span className="report-view-caret" aria-hidden="true" />
      </summary>
      <div className="report-view-menu" role="listbox" aria-label="Report view">
        {views.map((view) => (
          <button
            key={view}
            type="button"
            role="option"
            aria-selected={view === active}
            className={view === active ? 'active' : undefined}
            onClick={() => pick(view)}
          >
            <strong>{REPORT_VIEW_META[view].label}</strong>
            <span>{REPORT_VIEW_META[view].hint}</span>
          </button>
        ))}
      </div>
    </details>
  );
}
