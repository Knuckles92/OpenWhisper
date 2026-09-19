import { useEffect, useState } from 'react';
import type { VoiceFeedbackMsg } from '../types';
import './VoiceAssistantBubble.css';

export default function VoiceAssistantBubble({ feedback }: { feedback: VoiceFeedbackMsg | null }) {
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    setVisible(Boolean(feedback));
    if (!feedback) return;
    const pending = ['heard', 'recognized', 'working'].includes(feedback.phase);
    const timer = window.setTimeout(() => setVisible(false), pending ? 12000 : 6000);
    return () => window.clearTimeout(timer);
  }, [feedback]);
  if (!visible || !feedback) return null;
  return <div className="voice-assistant" data-phase={feedback.phase}>
    <div className="voice-assistant-orb" aria-hidden="true">
      {feedback.phase === 'saved' ? <span className="voice-assistant-check">✓</span> :
        <div className="voice-assistant-wave">{[0, 1, 2, 3, 4].map(i => <i key={i} style={{ animationDelay: `${i * -0.17}s` }} />)}</div>}
    </div>
    <div className="voice-assistant-copy" role="status" aria-live="polite" aria-atomic="true">
      <span>Meeting assistant</span><strong>{feedback.message}</strong>
    </div>
    <button type="button" aria-label="Dismiss assistant feedback" onClick={() => setVisible(false)}>×</button>
  </div>;
}
