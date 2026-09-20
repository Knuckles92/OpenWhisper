import { useEffect, useRef, useState } from 'react';
import { ops, type MeetingIntent, type Op } from '../types';

/** Matches MAX_INTENT_LEN in meeting/state/patches.py. */
const MAX_BRIEF_CHARS = 2000;

interface MeetingBriefProps {
  intent?: MeetingIntent;
  /** Only the host may write the brief, as with the meeting's title. */
  isHost: boolean;
  status: string;
  onSendOp: (op: Op) => Promise<boolean>;
}

function isLive(status: string): boolean {
  return status === 'active' || status === 'paused' || status === 'ending';
}

/**
 * The host's standing brief: what they want out of this meeting's record.
 *
 * Written before the meeting from the desktop app or here at any time, and
 * read by every agent pass, so naming a moment to watch for ("flag anything
 * about the Q3 budget") steers capture as the meeting happens rather than
 * being applied in hindsight.
 */
export default function MeetingBrief({
  intent,
  isHost,
  status,
  onSendOp,
}: MeetingBriefProps) {
  const text = (intent?.text ?? '').trim();
  const editable = isHost && isLive(status);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(text);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // A brief set from the desktop field, or by the host in another tab,
  // arrives as a patch. Adopt it unless this viewer is mid-edit.
  useEffect(() => {
    if (!editing) setDraft(text);
  }, [text, editing]);

  useEffect(() => {
    if (editing) textareaRef.current?.focus();
  }, [editing]);

  if (!text && !editable) return null;

  const save = async () => {
    if (saving) return;
    const next = draft.trim();
    if (next === text) {
      setEditing(false);
      return;
    }
    setSaving(true);
    setError('');
    try {
      const ok = await onSendOp(ops.setMeetingIntent(next));
      if (ok) {
        setEditing(false);
      } else {
        setError('The brief was not saved. Try again.');
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The brief was not saved.');
    } finally {
      setSaving(false);
    }
  };

  const cancel = () => {
    setDraft(text);
    setError('');
    setEditing(false);
  };

  if (editing) {
    return (
      <section className="meeting-brief editing" aria-label="Meeting brief">
        <div className="meeting-brief-heading">
          <strong>Meeting brief</strong>
          <span className="meeting-brief-hint">
            The note taker reads this on every pass.
          </span>
        </div>
        <textarea
          ref={textareaRef}
          aria-label="What do you want out of this meeting?"
          placeholder="What do you want out of these notes? Name anything to watch for, such as “we decide the vendor today — capture who objected and why”."
          value={draft}
          maxLength={MAX_BRIEF_CHARS}
          disabled={saving}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault();
              cancel();
            } else if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
              event.preventDefault();
              void save();
            }
          }}
        />
        <div className="meeting-brief-actions">
          <button type="button" className="ghost" disabled={saving} onClick={cancel}>
            Cancel
          </button>
          <button type="button" className="primary" disabled={saving} onClick={() => void save()}>
            {saving ? 'Saving…' : 'Save brief'}
          </button>
        </div>
        {error && (
          <p className="meeting-brief-error" role="status" aria-live="polite">
            {error}
          </p>
        )}
      </section>
    );
  }

  if (!text) {
    return (
      <section className="meeting-brief empty" aria-label="Meeting brief">
        <div className="meeting-brief-heading">
          <strong>Meeting brief</strong>
          <span className="meeting-brief-hint">
            Tell the note taker what you need from this meeting and it will
            watch for it as the meeting happens.
          </span>
        </div>
        <button type="button" className="ghost" onClick={() => setEditing(true)}>
          Add a brief
        </button>
      </section>
    );
  }

  return (
    <section className="meeting-brief" aria-label="Meeting brief">
      <div className="meeting-brief-heading">
        <strong>Meeting brief</strong>
        {editable && (
          <button type="button" className="ghost" onClick={() => setEditing(true)}>
            Edit
          </button>
        )}
      </div>
      <p className="meeting-brief-text">{text}</p>
    </section>
  );
}
