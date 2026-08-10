import type { FormEvent } from 'react';

interface JoinGateProps {
  meetingTitle: string;
  onJoin: (displayName: string) => void;
}

export default function JoinGate({ meetingTitle, onJoin }: JoinGateProps) {
  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    const name = String(fd.get('name') ?? '').trim();
    if (name) onJoin(name);
  };

  return (
    <div className="join-gate">
      <div className="join-card">
        <p className="brand">
          OpenWhisper <em>Meeting</em>
        </p>
        <h1>Join meeting</h1>
        <p>
          Enter your name to join <strong>{meetingTitle}</strong>.
        </p>
        <form className="join-form" onSubmit={handleSubmit}>
          <label>
            Display name
            <input
              name="name"
              type="text"
              placeholder="Your name"
              maxLength={120}
              autoFocus
              required
            />
          </label>
          <button type="submit" className="primary">
            Join
          </button>
        </form>
      </div>
    </div>
  );
}
