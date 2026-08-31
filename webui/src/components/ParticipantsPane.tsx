import { useState } from 'react';
import type { Participant } from '../types';

interface ParticipantsPaneProps {
  participants: Participant[];
  onlineIds: Set<string>;
  onRename?: (participantId: string, displayName: string) => Promise<boolean>;
  readOnly?: boolean;
}

const KIND_LABELS: Record<Participant['kind'], string> = {
  me: 'Host',
  others_cluster: 'Others',
  guest: 'Guest',
};

export default function ParticipantsPane({
  participants,
  onlineIds,
  onRename,
  readOnly = false,
}: ParticipantsPaneProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState('');
  const [savingId, setSavingId] = useState<string | null>(null);

  const sorted = [...participants].sort((a, b) => {
    const order = { me: 0, guest: 1, others_cluster: 2 };
    const d = order[a.kind] - order[b.kind];
    return d !== 0 ? d : a.display_name.localeCompare(b.display_name);
  });

  const startEdit = (p: Participant) => {
    if (readOnly || !onRename) return;
    setEditingId(p.id);
    setDraft(p.display_name);
  };

  const commitEdit = async (participantId: string) => {
    const trimmed = draft.trim();
    if (!trimmed || !onRename) {
      setEditingId(null);
      return;
    }
    if (savingId === participantId) return;
    setSavingId(participantId);
    try {
      if (await onRename(participantId, trimmed)) setEditingId(null);
    } catch {
      // Preserve the draft for retry.
    } finally {
      setSavingId(null);
    }
  };

  return (
    <section className="panel">
      <div className="panel-header">
        <span>In the room</span>
        <span className="meta">{participants.length}</span>
      </div>
      {sorted.length === 0 ? (
        <p className="empty-state people-empty">No participants yet.</p>
      ) : (
        <div className="people">
          {sorted.map((p) => {
            const online = onlineIds.has(p.id);
            return (
              <span
                key={p.id}
                className={`chip${online ? ' on' : ''}`}
                title={`${KIND_LABELS[p.kind]}${p.is_provisional ? ' · provisional' : ''}`}
              >
                {editingId === p.id ? (
                  <input
                    value={draft}
                    onChange={(e) => setDraft(e.target.value)}
                    onBlur={() => void commitEdit(p.id)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') void commitEdit(p.id);
                      if (e.key === 'Escape') setEditingId(null);
                    }}
                    autoFocus
                    disabled={savingId === p.id}
                    aria-label="Rename participant"
                  />
                ) : (
                  <span
                    onDoubleClick={() => startEdit(p)}
                    title="Double-click to rename"
                    style={{ cursor: 'default' }}
                  >
                    {p.display_name}
                  </span>
                )}
                {!readOnly && (
                  <button
                    type="button"
                    className="ghost no-print"
                    disabled={savingId === p.id}
                    onClick={() => startEdit(p)}
                    aria-label={`Rename ${p.display_name}`}
                  >
                    Rename
                  </button>
                )}
              </span>
            );
          })}
        </div>
      )}
    </section>
  );
}
