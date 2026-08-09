import { useState } from 'react';
import type { Participant } from '../types';

interface ParticipantsPaneProps {
  participants: Participant[];
  onlineIds: Set<string>;
  onRename: (participantId: string, displayName: string) => void;
}

const KIND_LABELS: Record<Participant['kind'], string> = {
  me: 'Host',
  others_cluster: 'Others',
  guest: 'Guest',
};

export default function ParticipantsPane({ participants, onlineIds, onRename }: ParticipantsPaneProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');

  const sorted = [...participants].sort((a, b) => {
    const order = { me: 0, guest: 1, others_cluster: 2 };
    const d = order[a.kind] - order[b.kind];
    return d !== 0 ? d : a.display_name.localeCompare(b.display_name);
  });

  const startEdit = (p: Participant) => {
    setEditingId(p.id);
    setDraft(p.display_name);
  };

  const commitEdit = (participantId: string) => {
    const trimmed = draft.trim();
    if (trimmed) onRename(participantId, trimmed);
    setEditingId(null);
  };

  return (
    <section className="panel">
      <div className="panel-header">
        <span>Participants</span>
        <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>{participants.length}</span>
      </div>
      <div className="panel-body">
        {sorted.length === 0 ? (
          <p className="empty-state">No participants yet.</p>
        ) : (
          sorted.map((p) => (
            <div key={p.id} className="participant-row">
              {onlineIds.has(p.id) && (
                <span className="participant-online" title="Online" aria-hidden />
              )}
              <div className="participant-name">
                {editingId === p.id ? (
                  <input
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onBlur={() => commitEdit(p.id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitEdit(p.id);
                      if (e.key === 'Escape') setEditingId(null);
                    }}
                    autoFocus
                  />
                ) : (
                  <span onDoubleClick={() => startEdit(p)} title="Double-click to rename">
                    {p.display_name}
                    {p.is_provisional && (
                      <span style={{ color: 'var(--text-muted)', fontSize: 11, marginLeft: 4 }}>
                        (provisional)
                      </span>
                    )}
                  </span>
                )}
              </div>
              <span className="participant-kind">{KIND_LABELS[p.kind]}</span>
              <button type="button" className="ghost" style={{ padding: '2px 8px', fontSize: 12 }} onClick={() => startEdit(p)}>
                Rename
              </button>
            </div>
          ))
        )}
      </div>
    </section>
  );
}
