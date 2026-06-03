'use client';

import { BUCKET_STYLES, normalizeBucket, type LanaUi } from '@/lib/buckets';

type Props = {
  role: 'user' | 'assistant';
  content: string;
  ui?: LanaUi | null;
  time?: string;
};

function formatTime(iso?: string) {
  if (!iso) return '';
  return new Date(iso).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function isMeaningfulPhrase(phrase?: string | null) {
  if (!phrase) return false;
  const t = phrase.trim().toLowerCase();
  return t.length > 0 && t !== 'none' && t !== 'null';
}

export function LanaBubble({ role, content, ui, time }: Props) {
  const isUser = role === 'user';
  const bucket = ui?.bucket ? normalizeBucket(ui.bucket) : null;
  const styles = bucket ? BUCKET_STYLES[bucket] : null;
  const focusPhrase = isMeaningfulPhrase(ui?.focus_phrase) ? ui!.focus_phrase : null;

  return (
    <div className={`bubble-row ${isUser ? 'bubble-row--user' : 'bubble-row--lana'}`}>
      <span className="bubble-speaker">{isUser ? 'User' : 'Lana'}</span>
      <div
        className={`bubble ${isUser ? 'bubble--user' : 'bubble--lana'}`}
        style={
          !isUser && styles
            ? { borderLeft: `4px solid ${styles.border}` }
            : undefined
        }
      >
        {!isUser && bucket && styles && (
          <div className="bucket-pill" style={{ color: styles.pill }}>
            <span className="bucket-dot" style={{ background: styles.pill }} />
            {styles.label}
            {focusPhrase ? ' · reflecting on' : ' · question'}
          </div>
        )}

        {!isUser && focusPhrase && styles && (
          <p className="focus-phrase" style={{ color: styles.pill }}>
            &ldquo;{focusPhrase}&rdquo;
          </p>
        )}

        <p className="bubble-text">{content}</p>

        {!isUser && ui?.highlights && ui.highlights.length > 0 && (
          <div className="highlights">
            {ui.highlights.map((h) => {
              const hb = normalizeBucket(h.bucket);
              const hs = BUCKET_STYLES[hb];
              return (
                <span
                  key={h.text}
                  className="highlight-chip"
                  style={{ background: hs.highlight, borderColor: hs.border }}
                >
                  {h.text}
                </span>
              );
            })}
          </div>
        )}
      </div>
      {time && <span className="bubble-time">{formatTime(time)}</span>}
    </div>
  );
}
