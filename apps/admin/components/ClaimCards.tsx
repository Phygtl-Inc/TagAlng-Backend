'use client';

import { BUCKET_STYLES, normalizeBucket } from '@/lib/buckets';
import type { ConversationClaim } from '@/lib/supabase';

export function ClaimCards({ claims }: { claims: ConversationClaim[] }) {
  if (!claims.length) return null;

  return (
    <section className="claims-panel">
      <h4>Identity claims ({claims.length})</h4>
      <p className="claims-intro">
        Each row is one stored claim — label, source quote, and synonym matches.
      </p>
      {claims.map((c) => {
        const b = normalizeBucket(c.bucket);
        const s = BUCKET_STYLES[b];
        return (
          <article
            key={c.label + (c.source_quote || '')}
            className="claim-card"
            style={{ borderLeftColor: s.border }}
          >
            <div className="claim-head">
              <strong>{c.label}</strong>
              <span className="match-pct">{Math.round(c.confidence * 100)}% match</span>
            </div>
            {c.source_quote && (
              <p className="claim-from">
                From &ldquo;<em>{c.source_quote}</em>&rdquo;
              </p>
            )}
            {c.synonyms?.length > 0 && (
              <div className="synonyms">
                {c.synonyms.map((syn) => (
                  <span key={syn} className="syn-chip">
                    ≈ {syn}
                  </span>
                ))}
              </div>
            )}
          </article>
        );
      })}
    </section>
  );
}
